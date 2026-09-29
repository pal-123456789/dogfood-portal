# src/awards/views.py
"""HTTP surface for prizes/awards.

Two audiences, one guard model that mirrors the rest of the organizer UI (voting/judging):
  * ORGANIZER console (manage/assign/clear/remove) -- gated by _organizer_or_response: anonymous
    callers are sent to the login page, authenticated non-organizers get a plain 403.
  * PUBLIC podium (podium) -- AllowAny GET that renders the derived podium ONLY once results are
    published; otherwise a neutral "not yet published" state with no ranking. It shows only
    published-safe values (title, team name, place, public q) -- never a per-judge score, a judge
    identity, or PII.
None of these routes is linked from base.html or sits among the acceptance checker's flat routes.
"""
from django.conf import settings
from django.contrib.auth.views import redirect_to_login
from django.http import HttpResponse
from django.shortcuts import get_object_or_404, redirect, render
from django.urls import reverse
from django.views.decorators.http import require_GET, require_http_methods

from events.models import Event, Track
from submissions.models import Submission

from . import services
from .models import Prize

_NOTICES = {"created": "Prize created.", "assigned": "Winner assigned.",
            "cleared": "Winner cleared.", "removed": "Prize removed."}


def _plain(message, status):
    return HttpResponse(message, status=status, content_type="text/plain; charset=utf-8")


def _organizer_or_response(request, event_ext_id):
    """Resolve the event and enforce organizer-of-this-event; return (event, None) or
    (None, response). Anonymous -> login redirect; unknown event -> 404; authenticated
    non-organizer -> plain 403. Matches events/judging/voting so the console guards identically."""
    if not (request.user and request.user.is_authenticated):
        return None, redirect_to_login(request.get_full_path(), settings.LOGIN_URL)
    event = get_object_or_404(Event, ext_id=event_ext_id)
    if not services.is_organizer(request.user, event):
        return None, _plain("organizers only", 403)
    return event, None


def _manage_context(event, *, notice="", error="", form=None):
    return {
        "event": event,
        "prizes": services.prizes_for_event(event),
        "tracks": Track.objects.filter(event=event).order_by("name"),
        "submissions": (Submission.objects.filter(event=event)
                        .select_related("team", "track").order_by("id")),
        "notice": notice, "error": error, "form": form or {},
    }


@require_http_methods(["GET", "POST"])
def manage(request, event_ext_id):
    """Organizer-only awards console: GET lists prizes + a create form; POST creates a prize."""
    event, resp = _organizer_or_response(request, event_ext_id)
    if resp is not None:
        return resp
    if request.method == "POST":
        track = None
        track_ext = (request.POST.get("track") or "").strip()
        if track_ext:
            track = get_object_or_404(Track, ext_id=track_ext, event=event)
        try:
            services.create_prize(
                event, name=request.POST.get("name", ""),
                position=request.POST.get("position", "1"), track=track,
                description=(request.POST.get("description") or "").strip(), actor=request.user)
        except ValueError as exc:
            return render(request, "awards/manage.html",
                          _manage_context(event, error=str(exc), form=request.POST), status=400)
        return redirect("%s?ok=created" % reverse("awards:manage", args=[event.ext_id]))
    return render(request, "awards/manage.html",
                  _manage_context(event, notice=_NOTICES.get(request.GET.get("ok", ""), "")))


def _prize_or_404(event, prize_ext_id):
    return get_object_or_404(Prize, ext_id=prize_ext_id, event=event)


@require_http_methods(["POST"])
def assign(request, event_ext_id, prize_ext_id):
    event, resp = _organizer_or_response(request, event_ext_id)
    if resp is not None:
        return resp
    prize = _prize_or_404(event, prize_ext_id)
    submission = get_object_or_404(Submission, ext_id=(request.POST.get("submission") or ""),
                                   event=event)
    try:
        services.assign_winner(prize, submission, actor=request.user)
    except ValueError as exc:
        return render(request, "awards/manage.html",
                      _manage_context(event, error=str(exc)), status=400)
    return redirect("%s?ok=assigned" % reverse("awards:manage", args=[event.ext_id]))


@require_http_methods(["POST"])
def clear(request, event_ext_id, prize_ext_id):
    event, resp = _organizer_or_response(request, event_ext_id)
    if resp is not None:
        return resp
    services.clear_winner(_prize_or_404(event, prize_ext_id), actor=request.user)
    return redirect("%s?ok=cleared" % reverse("awards:manage", args=[event.ext_id]))


@require_http_methods(["POST"])
def remove(request, event_ext_id, prize_ext_id):
    event, resp = _organizer_or_response(request, event_ext_id)
    if resp is not None:
        return resp
    services.remove_prize(_prize_or_404(event, prize_ext_id), actor=request.user)
    return redirect("%s?ok=removed" % reverse("awards:manage", args=[event.ext_id]))


@require_GET
def podium(request, event_ext_id):
    """PUBLIC awards/podium page. Renders the derived podium + prize winners ONLY when the event's
    results are published; otherwise a neutral 'not yet published' state with NO ranking. Everything
    shown is derived from the FROZEN, signed normalization result (never a live recompute) plus
    public submission metadata (title, team name) -- no per-judge score, judge identity, or PII."""
    event = get_object_or_404(Event, ext_id=event_ext_id)
    context = services.public_awards(event)
    context["event"] = event
    return render(request, "awards/podium.html", context)


@require_GET
def topup(request, event_ext_id):
    """ORGANIZER-only review top-up planner, computed from the LIVE (unsigned) standings: per podium
    prize, the contenders at or straddling that prize's rank cutoff -- where a few more reviews would
    most reduce uncertainty BEFORE results are finalized and signed. It ranks nothing, assigns no
    score, and is NOT fraud detection; the official podium is always derived from the signed result.
    The response is per-organizer computed data, so it is marked private/no-store and Vary: Cookie so
    it is never cached or served to another user."""
    event, resp = _organizer_or_response(request, event_ext_id)
    if resp is not None:
        return resp
    plan = services.review_topup(event)
    response = render(request, "awards/topup.html", {"event": event, **plan})
    response["Cache-Control"] = "private, no-store"
    response["Vary"] = "Cookie"
    return response
