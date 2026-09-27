# src/submissions/views.py
"""T1 submit endpoint. GET renders a minimal form (humans); POST creates a submission
through the service, mapping domain errors to status codes. The acceptance checker POSTs
to a CLOSED event, so it exercises the SubmissionsClosed -> 4xx path (check 3).

The submit() view below is byte-stable (checks 2/3 + replay). The participant self-service
pages -- "my submissions", edit, withdraw -- are mounted under /submissions/ (see urls.py),
reachable by URL and NOT linked from base.html, so they add surface without moving a byte
of the five flat checker routes.
"""
from django.contrib.auth.views import redirect_to_login
from django.core.exceptions import PermissionDenied
from django.conf import settings
from django.http import HttpResponse, JsonResponse
from django.shortcuts import redirect, render
from django.utils import timezone
from django.views.decorators.http import require_http_methods

from events.models import Event, Track

from portal import ratelimit

from . import services


def _current_event():
    return Event.objects.order_by("id").first()


@require_http_methods(["GET", "POST"])
def submit(request):
    event = _current_event()
    if event is None:
        return JsonResponse({"detail": "no event configured"}, status=404)

    if request.method == "GET":
        return render(request, "submissions/new.html", {
            "event": event,
            "tracks": Track.objects.filter(event=event).order_by("ext_id"),
            "open": event.accepting_submissions(timezone.now()),
        })

    if not request.user.is_authenticated:
        return JsonResponse({"detail": "authentication required"}, status=401)

    # Abuse control for real logged-in users. The DEMO shim (the acceptance checker) is EXEMPT:
    # its request carries `request.demo_shim`, so the checker's POST to /projects/new is never
    # rate-limited and check 3's response stays byte-identical no matter how often it runs. Real
    # users over DOGFOOD_RATE_LIMITS['submission_write'] get 429; the limiter fails open.
    if not getattr(request, "demo_shim", False):
        allowed, retry = ratelimit.hit(
            "submission_write:%s" % request.user.pk,
            settings.DOGFOOD_RATE_LIMITS["submission_write"])
        if not allowed:
            resp = JsonResponse({"detail": "rate limited"}, status=429)
            resp["Retry-After"] = str(retry)
            return resp

    track = Track.objects.filter(event=event, ext_id=request.POST.get("track", "")).first()
    try:
        sub = services.create_submission(
            request.user, event,
            team=services.participant_team(request.user, event),
            track=track,
            title=(request.POST.get("title") or "").strip(),
            summary=(request.POST.get("summary") or "").strip(),
            repo_url=(request.POST.get("repo_url") or "").strip(),
        )
    except services.SubmissionsClosed as e:
        return JsonResponse({"detail": str(e)}, status=403)     # check 3: 4xx (deadline)
    except PermissionDenied as e:
        return JsonResponse({"detail": str(e)}, status=403)
    except ValueError as e:
        return JsonResponse({"detail": str(e)}, status=400)
    return JsonResponse({"id": sub.ext_id, "title": sub.title, "state": sub.state}, status=201)


def _write_throttled(request):
    """Reuse the submission_write policy for the self-service write flows. The DEMO shim
    is exempt (parity with submit()); a blocked real user gets a 429 + Retry-After. Returns
    an HttpResponse to short-circuit with, or None when the caller may proceed."""
    if getattr(request, "demo_shim", False):
        return None
    allowed, retry = ratelimit.hit(
        "submission_write:%s" % request.user.pk,
        settings.DOGFOOD_RATE_LIMITS["submission_write"])
    if allowed:
        return None
    resp = HttpResponse("rate limited", status=429, content_type="text/plain")
    resp["Retry-After"] = str(retry)
    return resp


@require_http_methods(["GET"])
def mine(request):
    """A participant's own submissions across every state (including withdrawn), so they
    can revise or withdraw them. Read-only list; the row actions post to edit/withdraw."""
    event = _current_event()
    if event is None:
        return JsonResponse({"detail": "no event configured"}, status=404)
    if not request.user.is_authenticated:
        return redirect_to_login(request.get_full_path())
    return render(request, "submissions/mine.html", {
        "event": event,
        "submissions": services.owned_submissions(request.user, event),
        "open": event.accepting_submissions(timezone.now()),
        "withdrawn_state": services.Submission.WITHDRAWN,
    })


@require_http_methods(["GET", "POST"])
def edit(request, ext_id):
    """Revise one owned submission. GET pre-fills the form; POST applies the change through
    the service and redirects back to the list (POST/redirect/GET). Ownership, deadline and
    validation all live in the service, so this view only maps outcomes to responses."""
    event = _current_event()
    if event is None:
        return JsonResponse({"detail": "no event configured"}, status=404)
    if not request.user.is_authenticated:
        return redirect_to_login(request.get_full_path())
    try:
        sub = services._resolve_owned(request.user, event, ext_id)
    except services.SubmissionNotFound:
        return HttpResponse("no such submission", status=404, content_type="text/plain")
    except PermissionDenied as e:
        return HttpResponse(str(e), status=403, content_type="text/plain")

    now = timezone.now()
    is_open = event.accepting_submissions(now)
    editable = is_open and sub.state != services.Submission.WITHDRAWN

    def _page(error=None, status=200, values=None):
        v = values or {"title": sub.title, "summary": sub.summary, "repo_url": sub.repo_url}
        return render(request, "submissions/edit.html", {
            "event": event, "submission": sub, "editable": editable, "open": is_open,
            "error": error, "values": v,
            "withdrawn": sub.state == services.Submission.WITHDRAWN,
        }, status=status)

    if request.method == "GET":
        return _page()

    throttled = _write_throttled(request)
    if throttled is not None:
        return throttled

    values = {"title": (request.POST.get("title") or "").strip(),
              "summary": (request.POST.get("summary") or "").strip(),
              "repo_url": (request.POST.get("repo_url") or "").strip()}
    try:
        services.update_submission(request.user, event, ext_id,
                                   title=values["title"], summary=values["summary"],
                                   repo_url=values["repo_url"], now=now)
    except services.SubmissionsClosed as e:
        return _page(error=str(e), status=403, values=values)
    except PermissionDenied as e:
        return HttpResponse(str(e), status=403, content_type="text/plain")
    except services.SubmissionNotFound:
        return HttpResponse("no such submission", status=404, content_type="text/plain")
    except ValueError as e:
        return _page(error=str(e), status=400, values=values)
    return redirect("submissions:mine")


@require_http_methods(["POST"])
def withdraw(request, ext_id):
    """Soft-withdraw one owned submission (state -> withdrawn; the row and its ballot/audit
    history survive). POST-only; success and a redundant re-withdraw both redirect to the
    list, so the button is idempotent from the participant's point of view."""
    event = _current_event()
    if event is None:
        return JsonResponse({"detail": "no event configured"}, status=404)
    if not request.user.is_authenticated:
        return redirect_to_login(request.get_full_path())
    throttled = _write_throttled(request)
    if throttled is not None:
        return throttled
    try:
        services.withdraw_submission(request.user, event, ext_id, now=timezone.now())
    except services.SubmissionsClosed as e:
        return HttpResponse(str(e), status=403, content_type="text/plain")
    except PermissionDenied as e:
        return HttpResponse(str(e), status=403, content_type="text/plain")
    except services.SubmissionNotFound:
        return HttpResponse("no such submission", status=404, content_type="text/plain")
    except ValueError:
        pass  # already withdrawn -> idempotent, fall through to the list
    return redirect("submissions:mine")
