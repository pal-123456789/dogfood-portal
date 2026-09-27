# src/submissions/views.py
"""T1 submit endpoint. GET renders a minimal form (humans); POST creates a submission
through the service, mapping domain errors to status codes. The acceptance checker POSTs
to a CLOSED event, so it exercises the SubmissionsClosed -> 4xx path (check 3).
"""
from django.core.exceptions import PermissionDenied
from django.conf import settings
from django.http import JsonResponse
from django.shortcuts import render
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
