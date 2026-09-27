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
from django.http import Http404, HttpResponse
from django.shortcuts import get_object_or_404, redirect, render
from django.urls import reverse
from django.utils import timezone
from django.views.decorators.http import require_http_methods

from portal import ratelimit

from . import services
from .models import Event, Invite

# ?ok=<key> after a PRG redirect -> a human confirmation banner on the detail page.
_NOTICES = {"created": "Event created.", "track": "Track added.",
            "team": "Team added.", "state": "Event state updated.",
            "invite": "Invitation created."}


def _login_redirect(request):
    return redirect_to_login(request.get_full_path(), settings.LOGIN_URL)


def _invite_rows(event):
    """View-model for the invitations table: each row carries its shareable redeem path
    (route + ?sig=) and human-readable status, computed server-side (templates can't call
    is_expired(now) with an argument)."""
    now = timezone.now()
    rows = []
    for inv in event.invites.order_by("-id"):
        rows.append({
            "ext_id": inv.ext_id, "role": inv.role,
            "redeemed": inv.is_redeemed, "redeemed_at": inv.redeemed_at,
            "expires_at": inv.expires_at, "expired": inv.is_expired(now),
            "path": "%s?sig=%s" % (reverse("events:redeem", args=[inv.ext_id]), inv.signature),
        })
    return rows


def _detail_context(event, **extra):
    ctx = {"event": event,
           "tracks": event.tracks.order_by("id"),
           "teams": event.teams.order_by("id"),
           "states": Event.STATES,
           "invites": _invite_rows(event),
           "invite_roles": Invite.ROLES,
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


@require_http_methods(["POST"])
def create_invite(request, ext_id):
    """Organizer mints a signed, single-use invite for this event. Optional expiry reuses the
    same datetime parser as the submission window. PRG back to the detail page, where the new
    invite's shareable link is listed."""
    event, resp = _organizer_event_or_response(request, ext_id)
    if resp is not None:
        return resp
    expires_raw = (request.POST.get("expires_at") or "").strip()
    try:
        expires_at = services.parse_close(expires_raw) if expires_raw else None
        services.create_invite(request.user, event,
                               role=request.POST.get("role", ""), expires_at=expires_at)
    except ValidationError as e:
        return render(request, "events/detail.html",
                      _detail_context(event, error=" ".join(e.messages)), status=400)
    return redirect("%s?ok=invite" % reverse("events:detail", args=[event.ext_id]))


def _redeem_page(request, invite, *, state, status=200, error=""):
    """Render the invitee-facing redeem page. `state` drives the body: confirm | joined |
    redeemed | expired | invalid. The signature travels in the URL (GET) / a hidden field (POST),
    never in a link the invitee could tamper with undetected -- verification is server-side."""
    return render(request, "events/redeem.html", {
        "invite": invite, "event": invite.event, "state": state,
        "sig": request.GET.get("sig", "") if request.method == "GET" else request.POST.get("sig", ""),
        "error": error,
    }, status=status)


@require_http_methods(["GET", "POST"])
def redeem(request, ext_id):
    """Invitee lands here from a shared link (GET shows a confirm page), then joins (POST).

    Not organizer-gated -- any authenticated user may redeem a link addressed to them; anonymous
    visitors are sent to log in first. The POST is rate-limited under the `invite_redeem` policy
    (the DEMO shim is exempt, as everywhere) and all of signature / expiry / single-use live in
    services.redeem_invite, so this view only maps outcomes to responses.
    """
    if not request.user.is_authenticated:
        return _login_redirect(request)
    invite = get_object_or_404(Invite.objects.select_related("event"), ext_id=ext_id)

    if request.method == "GET":
        if invite.is_redeemed:
            joined = (request.GET.get("joined") == "1" and invite.redeemed_by_id == request.user.pk)
            return _redeem_page(request, invite, state="joined" if joined else "redeemed")
        if invite.is_expired(timezone.now()):
            return _redeem_page(request, invite, state="expired")
        return _redeem_page(request, invite, state="confirm")

    if not getattr(request, "demo_shim", False):
        allowed, retry = ratelimit.hit(
            "invite_redeem:%s" % request.user.pk,
            settings.DOGFOOD_RATE_LIMITS["invite_redeem"])
        if not allowed:
            resp = HttpResponse("rate limited", status=429, content_type="text/plain")
            resp["Retry-After"] = str(retry)
            return resp

    try:
        services.redeem_invite(request.user, ext_id, request.POST.get("sig", ""))
    except services.InviteNotFound:
        raise Http404("no such invitation")
    except services.InviteAlreadyRedeemed:
        return _redeem_page(request, invite, state="redeemed", status=409)
    except services.InviteInvalid as e:
        return _redeem_page(request, invite, state="invalid", status=400, error=str(e))
    return redirect("%s?joined=1" % reverse("events:redeem", args=[ext_id]))
