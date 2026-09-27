# src/events/services.py
"""Organizer-side write rules for the event core graph.

The organizer UI (src/events/views.py) never writes these models directly; it calls the helpers
here so every create/state change is atomic, appends a tamper-evident audit event, and mints an
opaque ext_id in the SAME shape the submission writer uses ("<prefix>_<uuid16>"). Seed rows keep
their fixture ids (evt_01, trk_01, tm_01); UI rows never collide with them, and because a new
event always gets a higher pk it never displaces _current_event() (the checker's target).

Authorization ("who may touch this event") lives in the view; the invariant that a config change
and its audit row commit together lives here -- the same contract create_submission/record_ballot
follow.
"""
import uuid

from django.core.exceptions import ValidationError
from django.db import transaction
from django.utils import timezone
from django.utils.dateparse import parse_datetime

from audit import service as audit_service

from .models import Event, EventMembership, Team, Track


def _ext_id(prefix):
    """An opaque id in the submission writer's shape; disjoint from fixture ids (evt_01, ...)."""
    return "%s_%s" % (prefix, uuid.uuid4().hex[:16])


def is_organizer(user, event):
    """True iff user holds an organizer membership for THIS event (event-scoped, not a global flag)."""
    return bool(user and user.is_authenticated and EventMembership.objects.filter(
        user=user, event=event, role=EventMembership.ORGANIZER).exists())


def organized_events(user):
    """Events this user organizes, newest first (dashboard listing)."""
    if not (user and user.is_authenticated):
        return Event.objects.none()
    return (Event.objects
            .filter(memberships__user=user, memberships__role=EventMembership.ORGANIZER)
            .order_by("-id").distinct())


def parse_close(raw):
    """Parse a submissions-close datetime from form input; return an aware datetime.

    Accepts ISO 8601 and the HTML datetime-local shape (YYYY-MM-DDTHH:MM). A naive value is read
    in the server timezone so accepting_submissions() compares like-for-like.
    """
    dt = parse_datetime((raw or "").strip())
    if dt is None:
        raise ValidationError("Enter a valid submissions-close date and time.")
    if timezone.is_naive(dt):
        dt = timezone.make_aware(dt, timezone.get_current_timezone())
    return dt


def create_event(actor, *, name, submissions_close, now=None):
    """Create an event in SETUP and make the creator its organizer, atomically + audited.

    Any authenticated user may create an event: there is no self-registration, so the caller set
    is already the deployment's provisioned users, and a fresh deployment has no memberships yet --
    requiring "organizer of some event" first would deadlock the bootstrap. Creating one makes the
    caller its organizer, which is what every later management action checks.
    """
    now = now or timezone.now()
    name = (name or "").strip()
    if not name:
        raise ValidationError("Event name is required.")
    close = parse_close(submissions_close)
    with transaction.atomic():
        event = Event.objects.create(
            ext_id=_ext_id("evt"), name=name, state=Event.SETUP,
            submissions_close=close, results_published=False)
        membership = EventMembership.objects.create(
            user=actor, event=event, role=EventMembership.ORGANIZER, ext_id=_ext_id("org"))
        audit_service.record_event(
            event_type="event.created", object_type="event", object_id=event.ext_id,
            actor_user_id=actor.pk, actor_membership_id=membership.ext_id,
            occurred_at=now.isoformat(),
            payload={"name": name, "state": event.state,
                     "submissions_close": close.isoformat(), "organizer": membership.ext_id})
        return event


def create_track(actor, event, *, name, now=None):
    """Append a track to an event (atomic + audited)."""
    now = now or timezone.now()
    name = (name or "").strip()
    if not name:
        raise ValidationError("Track name is required.")
    with transaction.atomic():
        track = Track.objects.create(ext_id=_ext_id("trk"), event=event, name=name)
        audit_service.record_event(
            event_type="track.created", object_type="track", object_id=track.ext_id,
            actor_user_id=actor.pk, occurred_at=now.isoformat(),
            payload={"event": event.ext_id, "name": name})
        return track


def create_team(actor, event, *, name, now=None):
    """Append a team to an event (atomic + audited)."""
    now = now or timezone.now()
    name = (name or "").strip()
    if not name:
        raise ValidationError("Team name is required.")
    with transaction.atomic():
        team = Team.objects.create(ext_id=_ext_id("tm"), event=event, name=name)
        audit_service.record_event(
            event_type="team.created", object_type="team", object_id=team.ext_id,
            actor_user_id=actor.pk, occurred_at=now.isoformat(),
            payload={"event": event.ext_id, "name": name})
        return team


def set_state(actor, event, *, state, now=None):
    """Move an event between setup/open/closed (atomic + audited).

    Opening is what actually starts accepting submissions -- seeded events ship CLOSED -- so this
    is the operability hinge a self-hoster needs to run a real round. A no-op (target == current)
    returns without writing an audit row.
    """
    now = now or timezone.now()
    if state not in {Event.SETUP, Event.OPEN, Event.CLOSED}:
        raise ValidationError("Unknown state %r." % (state,))
    previous = event.state
    if state == previous:
        return event
    with transaction.atomic():
        event.state = state
        event.save(update_fields=["state"])
        audit_service.record_event(
            event_type="event.state_changed", object_type="event", object_id=event.ext_id,
            actor_user_id=actor.pk, occurred_at=now.isoformat(),
            payload={"from": previous, "to": state})
        return event
