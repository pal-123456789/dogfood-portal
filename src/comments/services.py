# src/comments/services.py
"""Write/read rules for project comments + organizer moderation.

Same contract the submission and event writers follow: every state change runs inside one
`transaction.atomic()` that co-commits a tamper-evident audit event, so a comment can never
exist without its `comment.posted` row and a hide can never happen without its `comment.hidden`
row. Authorization for moderation (organizer OF the comment's event) is decided from the data --
an EventMembership -- and enforced by the view, exactly like events.services / judging.services.
Hiding is a SOFT state (never a delete), consistent with the append-only stance elsewhere.
"""
import uuid

from django.core.exceptions import PermissionDenied
from django.db import transaction
from django.utils import timezone

from events.models import EventMembership

from audit import service as audit_service
from submissions.models import Submission

from .models import Comment

BODY_MAX = 2000   # application-level cap; the column itself is an unbounded TextField


class CommentNotFound(Exception):
    """No comment with that ext_id exists (the view maps this to 404)."""


def _ext_id():
    """An opaque comment id in the project's `<prefix>_<uuid16>` shape."""
    return "%s_%s" % ("cmt", uuid.uuid4().hex[:16])


def get_submission(ext_id):
    """Resolve a submission by ext_id (its event pre-fetched), or None -> the view returns 404.
    ext_ids are already public on the gallery, so a 404 here leaks nothing."""
    if not ext_id:
        return None
    return Submission.objects.filter(ext_id=ext_id).select_related("event").first()


def get_comment(ext_id):
    """Resolve a comment (with its submission + event) by ext_id, or raise CommentNotFound."""
    comment = (Comment.objects.filter(ext_id=ext_id)
               .select_related("author", "submission", "submission__event").first())
    if comment is None:
        raise CommentNotFound(ext_id)
    return comment


def visible_comments(submission):
    """VISIBLE (hidden=False) comments for a submission, oldest-first. Hidden rows are excluded
    at the data layer, so a moderated comment is gone from every public read, not just one view."""
    return (Comment.objects.filter(submission=submission, hidden=False)
            .select_related("author").order_by("id"))


def is_event_organizer(user, event):
    """True iff `user` holds an ORGANIZER membership for THIS event (event-scoped, not a global
    flag) -- the same rule events.services.is_organizer applies."""
    return bool(user and user.is_authenticated and EventMembership.objects.filter(
        user=user, event=event, role=EventMembership.ORGANIZER).exists())


def post_comment(author, submission, *, body, now=None):
    """Create a visible comment on a submission (atomic + audited `comment.posted`).

    Gate order: an authenticated author is required (the view rejects anonymous with 401 before
    reaching here); `body` is stripped and must be non-empty and <= BODY_MAX chars. The create
    and its audit row share one transaction, so a comment can never exist without its audit event.
    """
    now = now or timezone.now()
    if not (author and author.is_authenticated):
        raise PermissionDenied("authentication required")
    body = (body or "").strip()
    if not body:
        raise ValueError("comment body is required")
    if len(body) > BODY_MAX:
        raise ValueError("comment is too long (max %d characters)" % BODY_MAX)
    with transaction.atomic():
        comment = Comment.objects.create(
            ext_id=_ext_id(), submission=submission, author=author, body=body)
        audit_service.record_event(
            event_type="comment.posted", object_type="comment", object_id=comment.ext_id,
            actor_user_id=author.pk, occurred_at=now.isoformat(),
            payload={"submission": submission.ext_id, "event": submission.event.ext_id,
                     "length": len(body)})
        return comment


def hide_comment(actor, comment, *, now=None):
    """Soft-hide a comment: flip `hidden` to True, never delete the row (atomic + audited
    `comment.hidden`). Authorization (organizer of comment.submission.event) is enforced by the
    caller (view), matching events.services. Idempotent: re-hiding an already-hidden comment is a
    no-op that writes no second audit event."""
    now = now or timezone.now()
    if comment.hidden:
        return comment
    with transaction.atomic():
        comment.hidden = True
        comment.save(update_fields=["hidden"])
        audit_service.record_event(
            event_type="comment.hidden", object_type="comment", object_id=comment.ext_id,
            actor_user_id=getattr(actor, "pk", ""), occurred_at=now.isoformat(),
            payload={"submission": comment.submission.ext_id,
                     "event": comment.submission.event.ext_id})
        return comment
