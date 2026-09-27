# src/events/views.py
"""Organizer UI for the event core graph: create an event (and become its organizer), then
manage its tracks, teams and lifecycle state.

None of this is on the acceptance checker's five flat routes -- it lives under /events/, never
touches base.html, and never calls _current_event() (the checker's target is always the oldest
event; UI-created events get higher ids and cannot displace it). Writes go through events.services
so each one is atomic and audited, exactly like the submission and ballot writers. Anonymous
callers are sent to the login page (this is a browser tool for people); an authenticated
non-organizer gets a plain 403.
"""
from django.conf import settings
from django.contrib.auth.views import redirect_to_login
from django.core.exceptions import ValidationError
from django.http import HttpResponse
from django.shortcuts import get_object_or_404, redirect, render
from django.urls import reverse
from django.utils import timezone
from django.views.decorators.http import require_http_methods

from . import services
from .models import Event

# ?ok=<key> after a PRG redirect -> a human confirmation banner on the detail page.
_NOTICES = {"created": "Event created.", "track": "Track added.",
            "team": "Team added.", "state": "Event state updated."}


def _login_redirect(request):
    return redirect_to_login(request.get_full_path(), settings.LOGIN_URL)


def _detail_context(event, **extra):
    ctx = {"event": event,
           "tracks": event.tracks.order_by("id"),
           "teams": event.teams.order_by("id"),
           "states": Event.STATES,
           "accepting": event.accepting_submissions(timezone.now()),
           "notice": "", "error": ""}
    ctx.update(extra)
    return ctx


def _organizer_event_or_response(request, ext_id):
    """Resolve the event and enforce organizer-of-this-event; return (event, None) or
    (None, response) where response is the login redirect / 403 to return as-is."""
    if not request.user.is_authenticated:
        return None, _login_redirect(request)
    event = get_object_or_404(Event, ext_id=ext_id)
    if not services.is_organizer(request.user, event):
        return None, HttpResponse("organizers only", status=403)
    return event, None


@require_http_methods(["GET"])
def dashboard(request):
    """List the caller's organized events and offer the create-event form."""
    if not request.user.is_authenticated:
        return _login_redirect(request)
    return render(request, "events/dashboard.html",
                  {"events": services.organized_events(request.user), "error": "", "form": {}})


@require_http_methods(["POST"])
def create_event(request):
    if not request.user.is_authenticated:
        return _login_redirect(request)
    try:
        event = services.create_event(
            request.user,
            name=request.POST.get("name", ""),
            submissions_close=request.POST.get("submissions_close", ""))
    except ValidationError as e:
        return render(request, "events/dashboard.html",
                      {"events": services.organized_events(request.user),
                       "error": " ".join(e.messages),
                       "form": {"name": request.POST.get("name", ""),
                                "submissions_close": request.POST.get("submissions_close", "")}},
                      status=400)
    return redirect("%s?ok=created" % reverse("events:detail", args=[event.ext_id]))


@require_http_methods(["GET"])
def detail(request, ext_id):
    event, resp = _organizer_event_or_response(request, ext_id)
    if resp is not None:
        return resp
    return render(request, "events/detail.html",
                  _detail_context(event, notice=_NOTICES.get(request.GET.get("ok", ""), "")))


@require_http_methods(["POST"])
def create_track(request, ext_id):
    event, resp = _organizer_event_or_response(request, ext_id)
    if resp is not None:
        return resp
    try:
        services.create_track(request.user, event, name=request.POST.get("name", ""))
    except ValidationError as e:
        return render(request, "events/detail.html",
                      _detail_context(event, error=" ".join(e.messages)), status=400)
    return redirect("%s?ok=track" % reverse("events:detail", args=[event.ext_id]))


@require_http_methods(["POST"])
def create_team(request, ext_id):
    event, resp = _organizer_event_or_response(request, ext_id)
    if resp is not None:
        return resp
    try:
        services.create_team(request.user, event, name=request.POST.get("name", ""))
    except ValidationError as e:
        return render(request, "events/detail.html",
                      _detail_context(event, error=" ".join(e.messages)), status=400)
    return redirect("%s?ok=team" % reverse("events:detail", args=[event.ext_id]))


@require_http_methods(["POST"])
def set_state(request, ext_id):
    event, resp = _organizer_event_or_response(request, ext_id)
    if resp is not None:
        return resp
    try:
        services.set_state(request.user, event, state=request.POST.get("state", ""))
    except ValidationError as e:
        return render(request, "events/detail.html",
                      _detail_context(event, error=" ".join(e.messages)), status=400)
    return redirect("%s?ok=state" % reverse("events:detail", args=[event.ext_id]))
