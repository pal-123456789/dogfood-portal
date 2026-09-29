# src/awards/services.py
"""Organizer-side write rules and read helpers for prizes/awards.

Every mutation here follows the repo's contract: wrap the business write in `transaction.atomic()`
and co-commit a tamper-evident audit event (audit.service.record_event) inside the SAME block, so
the change and its audit row commit or roll back together. Prizes are organizer-owned configuration,
so ext_ids are minted as "prz_" + secrets.token_hex(8) (the same secrets-based shape webhooks use).

The public read path (`public_awards`) DERIVES its podium from the frozen, signed normalization
result (normalize.results.current_results -- never a live recompute) plus public submission metadata
(title, team name). It reads no per-judge score, no judge identity, and no PII. The same-event
invariant between a prize and its track/awarded_submission is enforced here (there is no cross-event
DB constraint).
"""
import secrets

from django.db import transaction
from django.utils import timezone

from audit import service as audit_service
from events.models import EventMembership
from normalize import results as results_service
from submissions.models import Submission

from . import podium as podium_mod
from .models import Prize


def _ext_id(prefix):
    """An opaque id in the webhooks writer's shape (prefix + 8 random bytes as hex)."""
    return "%s_%s" % (prefix, secrets.token_hex(8))


def is_organizer(user, event):
    """True iff `user` holds an organizer membership for THIS event (event-scoped, not a global
    flag). Local mirror of events.services.is_organizer so awards depends on no sibling feature."""
    return bool(user and user.is_authenticated and EventMembership.objects.filter(
        user=user, event=event, role=EventMembership.ORGANIZER).exists())


def prizes_for_event(event):
    """Every prize for `event`, in Prize.Meta order (track, then targeted position). Read-only."""
    return list(Prize.objects.filter(event=event)
                .select_related("track", "awarded_submission", "awarded_submission__team"))

def create_prize(event, *, name, position, track=None, description="", actor=None, now=None):
    """Create a prize for `event` (atomic + audited). Returns the Prize.

    `position` is the podium place it targets (1, 2, 3, ...); 0 marks a special, non-podium award.
    A `track` must belong to `event` (else ValueError). `actor`/`now` are optional so a view can
    thread the acting user + timestamp into the audit trail; both default sensibly for scripts.
    """
    now = now or timezone.now()
    name = (name or "").strip()
    if not name:
        raise ValueError("Prize name is required.")
    try:
        position = int(position)
    except (TypeError, ValueError):
        raise ValueError("Prize position must be a whole number (0 for a special award).")
    if position < 0:
        raise ValueError("Prize position cannot be negative.")
    if track is not None and track.event_id != event.id:
        raise ValueError("The chosen track does not belong to this event.")
    with transaction.atomic():
        prize = Prize.objects.create(
            ext_id=_ext_id("prz"), event=event, track=track, name=name,
            description=(description or ""), position=position)
        audit_service.record_event(
            event_type="prize.created", object_type="prize", object_id=prize.ext_id,
            actor_user_id=getattr(actor, "pk", "") or "", occurred_at=now.isoformat(),
            payload={"event": event.ext_id, "name": name, "position": position,
                     "track": track.ext_id if track is not None else ""})
    return prize


def assign_winner(prize, submission, *, actor=None, now=None):
    """Point `prize` at an organizer-chosen winning `submission` (atomic + audited).

    Refuses a cross-event assignment: a submission from another event can never be attached to this
    prize (ValueError). The award is a curatorial pointer -- it is NOT part of the signed
    normalization result and adds no property to it.
    """
    now = now or timezone.now()
    if submission.event_id != prize.event_id:
        raise ValueError("A prize can only be awarded to a submission from the same event.")
    with transaction.atomic():
        prize.awarded_submission = submission
        prize.save(update_fields=["awarded_submission"])
        audit_service.record_event(
            event_type="prize.winner_assigned", object_type="prize", object_id=prize.ext_id,
            actor_user_id=getattr(actor, "pk", "") or "", occurred_at=now.isoformat(),
            payload={"event": prize.event.ext_id, "submission": submission.ext_id})
    return prize

def clear_winner(prize, *, actor=None, now=None):
    """Remove any organizer-assigned winner from `prize` (atomic + audited). Safe when none set."""
    now = now or timezone.now()
    with transaction.atomic():
        prior = prize.awarded_submission
        prize.awarded_submission = None
        prize.save(update_fields=["awarded_submission"])
        audit_service.record_event(
            event_type="prize.winner_cleared", object_type="prize", object_id=prize.ext_id,
            actor_user_id=getattr(actor, "pk", "") or "", occurred_at=now.isoformat(),
            payload={"event": prize.event.ext_id,
                     "submission": prior.ext_id if prior is not None else ""})
    return prize


def remove_prize(prize, *, actor=None, now=None):
    """Delete a prize (atomic + audited). Returns its ext_id.

    Prizes are organizer-owned config, not integrity records: unlike a ballot or a signed run they
    carry no append-only history, so a real delete is fine -- the audit event still records the
    removal, keeping the change traceable.
    """
    now = now or timezone.now()
    ext_id, event_ext_id = prize.ext_id, prize.event.ext_id
    with transaction.atomic():
        audit_service.record_event(
            event_type="prize.removed", object_type="prize", object_id=ext_id,
            actor_user_id=getattr(actor, "pk", "") or "", occurred_at=now.isoformat(),
            payload={"event": event_ext_id, "name": prize.name, "position": prize.position})
        prize.delete()
    return ext_id


# --- Public read path: derive the podium from the frozen, signed result -----------------------

def published_result(event):
    """The event's official published result dict (normalize.results.current_results), or None if
    results are not yet published. Never recomputes -- it returns the FROZEN, signed run verbatim."""
    data = results_service.current_results(event)
    return data if data.get("published") else None


def _submission_display(event):
    """submission ext_id -> {title, team, track, track_name} for enriching podium rows.

    The frozen result carries title + track ext_id but not the team name, so we join it from the DB.
    All of this is public-safe: title and team name are already exposed by the public gallery/API.
    """
    display = {}
    for sub in Submission.objects.filter(event=event).select_related("team", "track"):
        display[sub.ext_id] = {
            "title": sub.title,
            "team": sub.team.name if sub.team_id else "",
            "track": sub.track.ext_id if sub.track_id else "",
            "track_name": sub.track.name if sub.track_id else "",
        }
    return display


def _track_submission_ext_ids(event, track):
    """The set of submission ext_ids in one track of `event` -- the allowed set for a track podium."""
    return set(Submission.objects.filter(event=event, track=track)
               .values_list("ext_id", flat=True))


def _enrich(entry, display):
    """Add the team name (and human track name) to a podium entry from public submission metadata."""
    meta = display.get(entry["submission"], {})
    out = dict(entry)
    out["team"] = meta.get("team", "")
    out["title"] = entry.get("title") or meta.get("title", "")
    out["track_name"] = meta.get("track_name", "")
    return out

def public_awards(event, *, default_top_n=3, max_top_n=25):
    """Assemble the PUBLIC awards view for `event`, derived entirely from the frozen signed result.

    Returns {"published": False} when results are not yet published (the view then renders a neutral
    state with NO ranking). When published, returns:
      * "podium": the overall top-N places (N = max(default_top_n, deepest targeted prize position),
        clamped to max_top_n), each row {place, submission, title, team, track, track_name, q,
        source_rank};
      * "prizes": every prize with its resolved winner. A winner resolves in this order -- an
        organizer-assigned submission if present; else, for a podium-targeting prize (position >= 1),
        the submission at that place read from the frozen podium (within the prize's track when the
        prize is track-scoped, otherwise event-wide). A special award (position 0) shows a winner
        only when the organizer assigned one.
    Every value is copied from the frozen, signed result or from public submission metadata; no
    per-judge score, judge identity, or PII is read.
    """
    data = published_result(event)
    if data is None:
        return {"published": False}
    rows = (data.get("result") or {}).get("rows") or []
    prizes = prizes_for_event(event)
    display = _submission_display(event)

    needed = default_top_n
    for prize in prizes:
        if prize.position and prize.position > needed:
            needed = prize.position
    needed = max(0, min(int(needed), max_top_n))

    overall = [_enrich(entry, display) for entry in podium_mod.compute_podium(rows, top_n=needed)]

    track_podiums = {}   # track_id -> enriched podium within that track (computed once per track)
    for prize in prizes:
        if prize.track_id is not None and prize.track_id not in track_podiums:
            allowed = _track_submission_ext_ids(event, prize.track)
            scoped = podium_mod.compute_podium(rows, top_n=needed, allowed_ext_ids=allowed)
            track_podiums[prize.track_id] = [_enrich(entry, display) for entry in scoped]

    prize_views = []
    for prize in prizes:
        winner, source = None, ""
        if prize.awarded_submission_id is not None:
            sub = prize.awarded_submission
            winner = {"submission": sub.ext_id, "title": sub.title,
                      "team": sub.team.name if sub.team_id else "", "place": None,
                      "q": None, "source_rank": None}
            source = "assigned"
        elif prize.position and prize.position >= 1:
            pod = (track_podiums.get(prize.track_id) if prize.track_id is not None else overall)
            entry = podium_mod.place_at(pod, prize.position) if pod else None
            if entry is not None:
                winner, source = entry, "derived"
        prize_views.append({
            "ext_id": prize.ext_id, "name": prize.name, "description": prize.description,
            "position": prize.position, "track": prize.track.name if prize.track_id else "",
            "winner": winner, "winner_source": source})

    return {
        "published": True,
        "version": data.get("version"),
        "status": data.get("status"),
        "result_hash": data.get("result_hash"),
        "podium": overall,
        "prizes": prize_views,
    }


# --- Organizer aid: prize-line review top-up from the LIVE (unsigned) standings ----------------

def prize_lines(event):
    """The event's podium prizes (position >= 1) as plain planner descriptors:
    {position, track (track ext_id or ""), track_name, name, ext_id}. Special awards (position 0)
    imply no rank cutoff and are omitted. Read-only; order follows prizes_for_event."""
    lines = []
    for prize in prizes_for_event(event):
        if prize.position and prize.position >= 1:
            lines.append({
                "position": prize.position,
                "track": prize.track.ext_id if prize.track_id else "",
                "track_name": prize.track.name if prize.track_id else "",
                "name": prize.name,
                "ext_id": prize.ext_id,
            })
    return lines


def review_topup(event, *, min_ballots=3):
    """Organizer aid computed from the LIVE (unsigned) standings: for each podium prize, surface the
    contenders at or straddling its implied rank cutoff -- the places where a few more reviews would
    most reduce uncertainty BEFORE results are finalized and signed. Returns
    {"n_ballots", "min_ballots", "lines"}, where `lines` is the review_topup_plan output (one entry
    per podium prize). This is a pre-finalization review-planning aid: it ranks nothing, assigns no
    score, and is NOT fraud detection; the official podium is always derived from the signed result
    (see public_awards).

    normalize (which pulls in numpy via normalize.engine) and the pure planner are imported LAZILY
    so importing awards.services -- e.g. from the admin or the write path -- never hard-requires
    numpy or triggers a live recompute."""
    from normalize import services as normalize_services
    from . import topup as topup_mod
    board = normalize_services.leaderboard(event)
    rows = board.get("rows") or []
    lines = topup_mod.review_topup_plan(rows, prize_lines(event), min_ballots=min_ballots)
    return {"n_ballots": board.get("n_ballots", 0), "min_ballots": min_ballots, "lines": lines}



