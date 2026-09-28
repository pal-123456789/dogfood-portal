# src/comments/views.py
"""Project-comments API: public read, authenticated post, organizer soft-moderation.

JSON-first, api-style endpoints mounted under /comments/ -- OFF the five flat acceptance-checker
routes and not linked from base.html, so the checker surface stays byte-for-byte identical.
Anonymous callers may READ the visible comments on a project (200) but must authenticate to POST
(401); hiding a comment is organizer-only (a non-organizer gets 403) and is a soft state, never a
delete. Author identity is surfaced as a display/short name only -- never the login email (no PII
leak). Every write goes through comments.services, so it is atomic and audited.
"""
from django.conf import settings
from django.http import JsonResponse
from django.views.decorators.http import require_http_methods

from portal import ratelimit

from . import services

_DELETED_AUTHOR = "unknown"   # shown when a comment's author account was later deleted (SET_NULL)


def _comment_json(c):
    """Public JSON shape for one comment. Author is the display/short name (never the email)."""
    return {
        "ext_id": c.ext_id,
        "author": c.author.get_short_name() if c.author_id else _DELETED_AUTHOR,
        "body": c.body,
        "created_at": c.created_at.isoformat(),   # timezone-aware ISO 8601
    }


@require_http_methods(["GET", "POST"])
def project_comments(request, ext_id):
    """GET: list a project's VISIBLE comments as JSON (public, anonymous allowed, 200).
    POST: post a comment (401 if anonymous, 400 if empty, 201 with the created comment)."""
    submission = services.get_submission(ext_id)
    if submission is None:
        return JsonResponse({"detail": "no such project"}, status=404)

    if request.method == "GET":
        rows = [_comment_json(c) for c in services.visible_comments(submission)]
        return JsonResponse({"comments": rows})

    # POST -- authenticated callers only.
    if not request.user.is_authenticated:
        return JsonResponse({"detail": "authentication required"}, status=401)

    # Abuse control for real logged-in users. The DEMO shim (the acceptance checker) is EXEMPT,
    # exactly like submit()/redeem(): its request carries `request.demo_shim`, so the checker is
    # never rate-limited. Over DOGFOOD_RATE_LIMITS['comment_write'] -> 429 + Retry-After; the
    # limiter fails open (a cache outage never becomes a denial).
    if not getattr(request, "demo_shim", False):
        allowed, retry = ratelimit.hit(
            "comment_write:%s" % request.user.pk,
            settings.DOGFOOD_RATE_LIMITS["comment_write"])
        if not allowed:
            resp = JsonResponse({"detail": "rate limited"}, status=429)
            resp["Retry-After"] = str(retry)
            return resp

    try:
        comment = services.post_comment(
            request.user, submission, body=request.POST.get("body", ""))
    except ValueError as e:
        return JsonResponse({"detail": str(e)}, status=400)
    return JsonResponse(_comment_json(comment), status=201)


@require_http_methods(["POST"])
def moderate(request, ext_id):
    """Organizer soft-hides a comment. Anonymous -> 401; an authenticated non-organizer of the
    comment's event -> 403; unknown comment -> 404. `action=hide` (the default) sets hidden=True
    (atomic + audited). Idempotent, and never deletes the row. Returns 200 JSON."""
    if not request.user.is_authenticated:
        return JsonResponse({"detail": "authentication required"}, status=401)
    try:
        comment = services.get_comment(ext_id)
    except services.CommentNotFound:
        return JsonResponse({"detail": "no such comment"}, status=404)
    if not services.is_event_organizer(request.user, comment.submission.event):
        return JsonResponse({"detail": "organizers only"}, status=403)
    action = request.POST.get("action", "hide")
    if action != "hide":
        return JsonResponse({"detail": "unknown moderation action"}, status=400)
    services.hide_comment(request.user, comment)
    return JsonResponse({"ext_id": comment.ext_id, "hidden": True}, status=200)
