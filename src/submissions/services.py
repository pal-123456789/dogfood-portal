# src/submissions/services.py
"""Write-side rules for submissions.

The deadline is enforced HERE -- one canonical rule, on server time -- not in the view or
the template, so every path that creates a submission (the form POST, a future API, a
management action) passes through the same gate. This is what makes check 3 a property of
the domain, not of one handler.
"""
import uuid

from django.core.exceptions import PermissionDenied
from django.db import transaction
from django.utils import timezone

from events.models import EventMembership, TeamMember

from audit import service as audit_service

from .models import Submission


class SubmissionsClosed(Exception):
    """A submission write arrived after the event stopped accepting them (check 3)."""


class SubmissionNotFound(Exception):
    """No submission with that ext_id exists in this event (view maps this to 404)."""


def is_participant(user, event):
    return bool(user and user.is_authenticated and EventMembership.objects.filter(
        user=user, event=event, role=EventMembership.PARTICIPANT).exists())


def participant_team(user, event):
    """The team this user submits under, or None. First membership is deterministic
    because TeamMember ordering falls back to pk."""
    tm = (TeamMember.objects.filter(user=user, team__event=event)
          .select_related("team").order_by("id").first())
    return tm.team if tm else None


def owned_submissions(actor, event):
    """Every submission (any state, including withdrawn) belonging to a team `actor`
    is on within `event`, oldest-first. Powers the participant's "my submissions" page.
    An anonymous or team-less actor gets an empty queryset, never an error."""
    if not (actor and actor.is_authenticated):
        return Submission.objects.none()
    team_ids = list(TeamMember.objects.filter(user=actor, team__event=event)
                    .values_list("team_id", flat=True))
    return (Submission.objects.filter(event=event, team_id__in=team_ids)
            .select_related("team", "track").order_by("id"))


def _resolve_owned(actor, event, ext_id):
    """Resolve a submission by ext_id in `event` that `actor` is allowed to modify.

    Ownership is team-membership: the actor must be on the submission's team. Raises
    SubmissionNotFound for an unknown id (-> 404) and PermissionDenied for a real
    submission the actor doesn't own (-> 403), so an outsider can't probe which ext_ids
    exist by watching the status code -- both an unknown id and someone else's id are
    indistinguishable only if we returned the same code, but here 404 means "no such row
    in this event" and is safe to reveal (ext_ids are already public on the gallery)."""
    sub = (Submission.objects.filter(event=event, ext_id=ext_id)
           .select_related("team", "track").first())
    if sub is None:
        raise SubmissionNotFound(ext_id)
    if not (actor and actor.is_authenticated
            and TeamMember.objects.filter(user=actor, team=sub.team).exists()):
        raise PermissionDenied("only the owning team may modify this submission")
    return sub


def create_submission(actor, event, *, team, track, title, summary="", repo_url="", now=None):
    """Create a SUBMITTED submission after checking auth -> role -> deadline, in that order.

    The deadline check precedes any field validation on purpose: a late POST is rejected
    for being late even if it is otherwise well-formed.
    """
    now = now or timezone.now()
    if not (actor and actor.is_authenticated):
        raise PermissionDenied("authentication required")
    if not is_participant(actor, event):
        raise PermissionDenied("only participants may submit")
    if not event.accepting_submissions(now):
        raise SubmissionsClosed("submissions are closed for this event")
    if not title:
        raise ValueError("title is required")
    if team is None or track is None:
        raise ValueError("team and track are required")
    # The create and its audit event share one transaction: a submission can never exist
    # without its tamper-evident audit row. Note this is reached ONLY after the deadline gate
    # above passes, so check 3's late POST is rejected before anything is written or audited.
    with transaction.atomic():
        sub = Submission.objects.create(
            ext_id="sub_%s" % uuid.uuid4().hex[:16],
            event=event, team=team, track=track,
            title=title, summary=summary, repo_url=repo_url,
            state=Submission.SUBMITTED, submitted_at=now,
        )
        audit_service.record_event(
            event_type="submission.created", object_type="submission", object_id=sub.ext_id,
            actor_user_id=actor.pk, occurred_at=now.isoformat(),
            payload={"title": title, "track": track.ext_id, "team": team.ext_id,
                     "event": event.ext_id, "state": sub.state})
        return sub


def update_submission(actor, event, ext_id, *, title, summary="", repo_url="", now=None):
    """Revise an owned submission's editable fields while the event is still accepting
    writes. Same gate order as create -- auth -> participant -> ownership -> deadline ->
    validation -- so a late edit is rejected for being late exactly like a late create
    (check 3 parity). A withdrawn submission is frozen: edit it back only by not being
    withdrawn. The update and its `submission.revised` audit row share one transaction."""
    now = now or timezone.now()
    if not (actor and actor.is_authenticated):
        raise PermissionDenied("authentication required")
    if not is_participant(actor, event):
        raise PermissionDenied("only participants may edit submissions")
    sub = _resolve_owned(actor, event, ext_id)
    if sub.state == Submission.WITHDRAWN:
        raise ValueError("a withdrawn submission cannot be edited")
    if not event.accepting_submissions(now):
        raise SubmissionsClosed("submissions are closed for this event")
    if not title:
        raise ValueError("title is required")
    changed = [f for f, new in (("title", title), ("summary", summary), ("repo_url", repo_url))
               if getattr(sub, f) != new]
    with transaction.atomic():
        sub.title, sub.summary, sub.repo_url = title, summary, repo_url
        sub.save(update_fields=["title", "summary", "repo_url"])
        audit_service.record_event(
            event_type="submission.revised", object_type="submission", object_id=sub.ext_id,
            actor_user_id=actor.pk, occurred_at=now.isoformat(),
            payload={"title": title, "changed": changed, "event": event.ext_id,
                     "team": sub.team.ext_id, "track": sub.track.ext_id})
        return sub


def withdraw_submission(actor, event, ext_id, *, now=None):
    """Soft-withdraw an owned submission: flip state to WITHDRAWN, never delete the row.

    The project drops out of the public gallery but its JudgeAssignment -> Ballot ->
    BallotRevision history and audit trail stay intact -- the same non-destructive stance
    the admin cascade guards enforce. Gated on the deadline like edit, so a team can only
    withdraw while the event is still OPEN (after close the record is locked, and any
    scoring that has begun is never silently hidden). Records `submission.withdrawn`."""
    now = now or timezone.now()
    if not (actor and actor.is_authenticated):
        raise PermissionDenied("authentication required")
    if not is_participant(actor, event):
        raise PermissionDenied("only participants may withdraw submissions")
    sub = _resolve_owned(actor, event, ext_id)
    if sub.state == Submission.WITHDRAWN:
        raise ValueError("submission is already withdrawn")
    if not event.accepting_submissions(now):
        raise SubmissionsClosed("submissions are closed for this event")
    with transaction.atomic():
        sub.state = Submission.WITHDRAWN
        sub.save(update_fields=["state"])
        audit_service.record_event(
            event_type="submission.withdrawn", object_type="submission", object_id=sub.ext_id,
            actor_user_id=actor.pk, occurred_at=now.isoformat(),
            payload={"title": sub.title, "event": event.ext_id, "team": sub.team.ext_id,
                     "track": sub.track.ext_id})
        return sub
