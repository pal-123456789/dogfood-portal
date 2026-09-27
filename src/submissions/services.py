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


def is_participant(user, event):
    return bool(user and user.is_authenticated and EventMembership.objects.filter(
        user=user, event=event, role=EventMembership.PARTICIPANT).exists())


def participant_team(user, event):
    """The team this user submits under, or None. First membership is deterministic
    because TeamMember ordering falls back to pk."""
    tm = (TeamMember.objects.filter(user=user, team__event=event)
          .select_related("team").order_by("id").first())
    return tm.team if tm else None


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
