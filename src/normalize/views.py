# src/normalize/views.py
"""Organizer-gated, READ-ONLY normalization leaderboard (HTML + JSON).

These routes live under /normalize/ and are NOT among the five the acceptance checker hits,
so nothing here can move 7/7. Access mirrors the CSV export: an unauthenticated caller gets
401, a non-organizer 403; only an organizer of the current event may see normalized results,
since severity-adjustment can reshuffle the podium (they are results-in-waiting, not public).
"""
from django.http import HttpResponse, JsonResponse
from django.shortcuts import render

from events.models import EventMembership

from . import services


def _gate(request):
    """Return (event, error): error is None for an organizer, else a (message, status) pair."""
    event = services.current_event()
    if event is None:
        return None, ("no event configured", 404)
    if not (request.user and request.user.is_authenticated):
        return event, ("authentication required", 401)
    is_org = EventMembership.objects.filter(
        user=request.user, event=event, role=EventMembership.ORGANIZER).exists()
    if not is_org:
        return event, ("organizer only", 403)
    return event, None


def leaderboard(request):
    event, err = _gate(request)
    if err:
        msg, status = err
        return HttpResponse(msg, status=status, content_type="text/plain")
    return render(request, "normalize/leaderboard.html",
                  {"event": event, "data": services.leaderboard(event)})


def leaderboard_json(request):
    event, err = _gate(request)
    if err:
        msg, status = err
        return JsonResponse({"detail": msg}, status=status)
    return JsonResponse(services.leaderboard(event))
