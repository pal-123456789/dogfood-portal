# src/gallery/views.py
"""Public project gallery (checks 1 & 2) and the /debug/whoami helper (§4e).

The gallery lists EVERY submission with no pagination, so a seeded title is always present
in the response body (check 2). whoami is the manual proof that each `session=` cookie
resolves through the DEMO auth shim to the intended user and memberships.
"""
from django.http import JsonResponse
from django.shortcuts import render

from events.models import Event, EventMembership, Track
from submissions.models import Submission


def _current_event():
    return Event.objects.order_by("id").first()


def projects(request):
    event = _current_event()
    submissions = (Submission.objects
                   .select_related("team", "track")
                   .order_by("id"))                       # oldest-first: check-2 titles on top
    active_track = request.GET.get("track") or ""
    if active_track:
        submissions = submissions.filter(track__ext_id=active_track)
    tracks = Track.objects.filter(event=event).order_by("ext_id") if event else Track.objects.none()
    return render(request, "gallery/projects.html", {
        "event": event,
        "submissions": submissions,
        "tracks": tracks,
        "active_track": active_track,
    })


def whoami(request):
    user = request.user
    if not (user and user.is_authenticated):
        return JsonResponse({"authenticated": False})
    memberships = list(EventMembership.objects
                       .filter(user=user)
                       .values("event__ext_id", "role", "ext_id"))
    return JsonResponse({
        "authenticated": True,
        "email": user.email,
        "display_name": user.get_short_name(),
        "memberships": memberships,
    })
