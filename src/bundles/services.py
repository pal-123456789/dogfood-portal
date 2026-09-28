# src/bundles/services.py
"""Portable, signed event export/import (T4) -- computed on the fly over the live event graph.

There is NO bundles model and NO migration: a bundle is BUILT from the event graph at request time,
signed with the one operator Ed25519 key on /state (audit.keys.ensure_private_key -> the same key
that signs audit checkpoints, normalization runs, records and invites), and each export/import
operation co-commits a tamper-evident audit event. This mirrors the model-free records/embed apps;
this module adds no new key and no new signature scheme, only the frozen domain tag in
bundles.signing (dogfood.bundle.v1).

Scope of the export (site-administrator-only, for backup / migration between deployments):
  * a faithful STRUCTURAL snapshot -- event config, tracks, teams (with their members), submissions,
    rubric weights, and the community-voting campaign window if one exists;
  * because it is a site-admin-only backup, team members carry the participant's email +
    display_name + their event role. That PII is in-scope for an operator backing up their own data.
  * it deliberately carries NO ballots, NO per-judge scores, and NO audit chain. Judges/organizers
    are re-provisioned on the target; they are not part of the structural team graph.

Honest scope of the signature (bundles.signing): it attests only that the operator's key signed this
exported structure at export time. It is NOT proof of results integrity and NOT a measure of merit.

Import never mints accounts: memberships and team memberships link ONLY AppUser rows that already
exist on the target (matched by email, case-insensitive); an unknown email is skipped and reported.
An imported event's results_published flag is reset to False, because a bundle does not carry the
signed normalization runs / publications that would back a live published result.
"""
from __future__ import annotations

import csv
import io
import uuid

from django.contrib.auth import get_user_model
from django.db import transaction
from django.utils import timezone
from django.utils.dateparse import parse_datetime

from audit import keys
from audit import service as audit_service
from events.models import Event, EventMembership, Team, TeamMember, Track
from submissions.models import Submission

from . import signing

BUNDLE_KIND = "dogfood.event-bundle.v1"

# Membership ext_id prefixes for import-created rows, echoing the fixture / events.services shape.
_ROLE_PREFIX = {EventMembership.JUDGE: "jdg", EventMembership.PARTICIPANT: "prt",
                EventMembership.ORGANIZER: "org"}


# --- domain errors (views map these to status codes) -----------------------------------------
class BundleInvalid(Exception):
    """The signed bundle's signature is missing, malformed, or does not verify (-> 400)."""


class BundleConflict(Exception):
    """An Event with the bundle's ext_id already exists; import refuses to overwrite (-> 409)."""


# --- small helpers ----------------------------------------------------------------------------
def _ext_id(prefix):
    """Opaque id in the project-wide shape ("<prefix>_<uuid16>"); disjoint from fixture ids."""
    return "%s_%s" % (prefix, uuid.uuid4().hex[:16])


def _parse_dt(raw):
    """Parse an ISO-8601 string into an aware datetime (naive read in the server tz), or None."""
    if not raw:
        return None
    dt = parse_datetime(raw)
    if dt is not None and timezone.is_naive(dt):
        dt = timezone.make_aware(dt, timezone.get_current_timezone())
    return dt


def _find_user(email):
    """The existing AppUser for `email` (case-insensitive), or None. NEVER creates a user."""
    email = (email or "").strip()
    if not email:
        return None
    return get_user_model().objects.filter(email__iexact=email).first()


def _role_map(event):
    """{user_id: role} for the event's memberships. A participant membership wins when a user holds
    more than one role, else the first role by id -- so a team member's exported role is stable."""
    m = {}
    for uid, role in (EventMembership.objects.filter(event=event)
                      .order_by("id").values_list("user_id", "role")):
        if role == EventMembership.PARTICIPANT:
            m[uid] = role
        elif uid not in m:
            m[uid] = role
    return m


def _rubric_weights(event):
    """Exported rubric weights [{criterion, weight}], or None if the model is unavailable.

    RubricWeight lives in the sibling judging app; imported lazily so bundles stays decoupled and
    the key is present only "if that model exists". An event with no configured weights exports [].
    """
    try:
        from judging.models import RubricWeight
    except Exception:
        return None
    return [{"criterion": w.criterion, "weight": w.weight}
            for w in RubricWeight.objects.filter(event=event).order_by("id")]


def _voting_campaign(event):
    """The event's community-voting window {mode, credit_budget, opens_at, closes_at}, or None.

    voting is a sibling app imported lazily inside the function so bundles never hard-depends on it.
    """
    try:
        from voting.models import VotingCampaign
    except Exception:
        return None
    c = VotingCampaign.objects.filter(event=event).first()
    if c is None:
        return None
    return {"mode": c.mode, "credit_budget": c.credit_budget,
            "opens_at": c.opens_at.isoformat() if c.opens_at else None,
            "closes_at": c.closes_at.isoformat() if c.closes_at else None}

# --- build + sign the export ------------------------------------------------------------------
def build_bundle(event) -> dict:
    """A faithful STRUCTURAL export of the event graph (no ballots, no scores, no audit chain).

    `event` carries ext_id + name + state + the aware submissions_close datetime (the only
    timezone-bearing field on Event) + results_published, so the target can reconstruct config.
    Team members carry email + display_name + their event role (site-admin backup, PII in-scope).
    """
    roles = _role_map(event)
    tracks = [{"ext_id": t.ext_id, "name": t.name}
              for t in event.tracks.order_by("id")]
    teams = []
    for team in event.teams.order_by("id"):
        members = []
        for tm in team.members.select_related("user").order_by("id"):
            u = tm.user
            members.append({"email": u.email, "display_name": u.display_name,
                            "role": roles.get(u.id, EventMembership.PARTICIPANT)})
        teams.append({"ext_id": team.ext_id, "name": team.name, "members": members})
    submissions = [{"ext_id": s.ext_id, "title": s.title, "summary": s.summary,
                    "repo_url": s.repo_url, "state": s.state,
                    "track_ext_id": s.track.ext_id, "team_ext_id": s.team.ext_id}
                   for s in event.submissions.select_related("track", "team").order_by("id")]
    bundle = {
        "kind": BUNDLE_KIND,
        "event": {"ext_id": event.ext_id, "name": event.name, "state": event.state,
                  "submissions_close": (event.submissions_close.isoformat()
                                        if event.submissions_close else None),
                  "results_published": event.results_published},
        "tracks": tracks,
        "teams": teams,
        "submissions": submissions,
        "exported_at": timezone.now().isoformat(),
    }
    rubric = _rubric_weights(event)
    if rubric is not None:
        bundle["rubric_weights"] = rubric
    campaign = _voting_campaign(event)
    if campaign is not None:
        bundle["voting_campaign"] = campaign
    return bundle


def _bundle_counts(bundle) -> dict:
    """Structural counts of a bundle -- the audit payload for an export and the shape import echoes."""
    teams = bundle.get("teams", [])
    return {"tracks": len(bundle.get("tracks", [])), "teams": len(teams),
            "team_members": sum(len(t.get("members", [])) for t in teams),
            "submissions": len(bundle.get("submissions", [])),
            "rubric_weights": len(bundle.get("rubric_weights", [])),
            "voting_campaign": 1 if bundle.get("voting_campaign") else 0}


def sign_export(event, *, actor=None) -> dict:
    """Build the bundle, attach one Ed25519 signature over it, and audit the export.

    The signature covers exactly `bundle` (bundles.signing over its canonical bytes).
    signer_fingerprint names the public key a verifier must pin; verify_signed_bundle re-derives it
    from our key, so a doctored fingerprint is rejected even though the Ed25519 check binds our key.
    """
    bundle = build_bundle(event)
    key, _created = keys.ensure_private_key()
    pub = key.public_key()
    doc = {"bundle": bundle,
           "signature": {"algorithm": "ed25519",
                         "value": signing.sign_bundle(key, bundle),
                         "signer_fingerprint": signing.public_fingerprint(pub)}}
    now = timezone.now()
    with transaction.atomic():
        audit_service.record_event(
            event_type="bundle.exported", object_type="event", object_id=event.ext_id,
            actor_user_id=getattr(actor, "pk", "") or "", occurred_at=now.isoformat(),
            payload=_bundle_counts(bundle))
    return doc


def verify_signed_bundle(doc) -> bool:
    """Recompute the signed material from a doc's OWN bundle and verify it against OUR key.

    Stateless: reads nothing but the deployment key and writes nothing. Returns False (never raises)
    for a non-dict, a missing/malformed/for-a-different-key signature, or a doctored bundle field.
    """
    if not isinstance(doc, dict):
        return False
    bundle = doc.get("bundle")
    sig = doc.get("signature")
    if not isinstance(bundle, dict) or not isinstance(sig, dict):
        return False
    value = sig.get("value")
    if not isinstance(value, str):
        return False
    try:
        key, _created = keys.ensure_private_key()
    except Exception:
        return False
    pub = key.public_key()
    claimed_fp = sig.get("signer_fingerprint")
    if claimed_fp is not None and claimed_fp != signing.public_fingerprint(pub):
        return False
    return signing.verify_bundle_sig(pub, signature=value, bundle_dict=bundle)

# --- import -----------------------------------------------------------------------------------
def _import_rubric(event, weights) -> int:
    """Recreate rubric weights under `event`; returns the count created (0 if none/unavailable)."""
    if not weights:
        return 0
    try:
        from judging.models import RubricWeight
    except Exception:
        return 0
    n = 0
    for w in weights:
        RubricWeight.objects.create(event=event, criterion=w.get("criterion", ""),
                                    weight=w.get("weight", 0.0))
        n += 1
    return n


def _import_campaign(event, campaign) -> bool:
    """Recreate the voting campaign under `event` with a FRESH ext_id; True if one was created.

    A campaign missing its window is skipped rather than crashing the import (degrade, don't raise).
    """
    if not campaign:
        return False
    try:
        from voting.models import VotingCampaign
    except Exception:
        return False
    opens = _parse_dt(campaign.get("opens_at"))
    closes = _parse_dt(campaign.get("closes_at"))
    if opens is None or closes is None:
        return False
    VotingCampaign.objects.create(
        ext_id=_ext_id("vcmp"), event=event,
        mode=campaign.get("mode", VotingCampaign.AUTHENTICATED),
        credit_budget=campaign.get("credit_budget", 25) or 25,
        opens_at=opens, closes_at=closes)
    return True


def import_bundle(actor, doc):
    """Verify a signed bundle, then reconstruct the event graph on THIS deployment (atomic+audited).

    Order matters: the signature is verified FIRST (BundleInvalid -> 400 on a bad/missing/tampered
    sig), then -- inside one transaction -- a pre-existing Event with the same ext_id is a
    BundleConflict (-> 409). Memberships and team memberships link ONLY existing AppUser rows
    (matched by email, case-insensitive); an unknown email is skipped and returned, never minted.
    results_published is reset to False: a bundle carries no signed publication to back it.
    """
    if not verify_signed_bundle(doc):
        raise BundleInvalid("bundle signature is missing, malformed, or does not verify")
    bundle = doc["bundle"]
    ev = bundle.get("event") or {}
    event_ext_id = (ev.get("ext_id") or "").strip()
    if not event_ext_id:
        raise BundleInvalid("bundle is missing event.ext_id")
    now = timezone.now()
    skipped = []
    counts = {"tracks": 0, "teams": 0, "team_members": 0, "memberships": 0,
              "submissions": 0, "rubric_weights": 0, "voting_campaign": 0}
    with transaction.atomic():
        if Event.objects.filter(ext_id=event_ext_id).exists():
            raise BundleConflict(event_ext_id)
        event = Event.objects.create(
            ext_id=event_ext_id, name=ev.get("name", ""),
            state=ev.get("state", Event.SETUP),
            submissions_close=_parse_dt(ev.get("submissions_close")) or now,
            results_published=False)
        track_by_ext = {}
        for t in bundle.get("tracks", []):
            track_by_ext[t["ext_id"]] = Track.objects.create(
                ext_id=t["ext_id"], event=event, name=t.get("name", ""))
            counts["tracks"] += 1
        team_by_ext = {}
        for tdata in bundle.get("teams", []):
            team = Team.objects.create(ext_id=tdata["ext_id"], event=event,
                                       name=tdata.get("name", ""))
            team_by_ext[tdata["ext_id"]] = team
            counts["teams"] += 1
            for m in tdata.get("members", []):
                user = _find_user(m.get("email"))
                if user is None:
                    email = (m.get("email") or "").strip()
                    if email:
                        skipped.append(email)
                    continue
                role = m.get("role") or EventMembership.PARTICIPANT
                _, made_m = EventMembership.objects.get_or_create(
                    user=user, event=event, role=role,
                    defaults={"ext_id": _ext_id(_ROLE_PREFIX.get(role, "mem"))})
                if made_m:
                    counts["memberships"] += 1
                _, made_tm = TeamMember.objects.get_or_create(team=team, user=user)
                if made_tm:
                    counts["team_members"] += 1
        for s in bundle.get("submissions", []):
            track = track_by_ext.get(s.get("track_ext_id"))
            team = team_by_ext.get(s.get("team_ext_id"))
            if track is None or team is None:
                raise BundleInvalid(
                    "submission %r references an unknown track/team" % s.get("ext_id"))
            Submission.objects.create(
                ext_id=s["ext_id"], event=event, team=team, track=track,
                title=s.get("title", ""), summary=s.get("summary", ""),
                repo_url=s.get("repo_url", ""), state=s.get("state", Submission.DRAFT))
            counts["submissions"] += 1
        counts["rubric_weights"] += _import_rubric(event, bundle.get("rubric_weights"))
        if _import_campaign(event, bundle.get("voting_campaign")):
            counts["voting_campaign"] = 1
        audit_service.record_event(
            event_type="bundle.imported", object_type="event", object_id=event.ext_id,
            actor_user_id=getattr(actor, "pk", "") or "", occurred_at=now.isoformat(),
            payload={**counts, "skipped_users": len(skipped)})
    return {"event_ext_id": event.ext_id, "counts": counts, "skipped_users": skipped}

# --- all-stages CSV ---------------------------------------------------------------------------
def _frozen_results(event):
    """{submission_ext_id: (rank, score)} from the event's PUBLISHED frozen run, or None.

    Read VERBATIM from the signed run's stored result via normalize.results.current_results -- never
    recomputed, so the CSV agrees with the published leaderboard and its offline verifier. Returns
    None when results were never published (the CSV then omits the rank/score columns). normalize is
    imported lazily so bundles does not hard-depend on it.
    """
    try:
        from normalize import results as normalize_results
    except Exception:
        return None
    data = normalize_results.current_results(event)
    if not data.get("published"):
        return None
    out = {}
    for row in (data.get("result") or {}).get("rows", []):
        out[row.get("submission")] = (row.get("rank"), row.get("q"))
    return out


def all_stage_csv(event) -> str:
    """A submissions summary CSV. Columns: submission_ext_id,title,team,track,state. IF the event has
    a PUBLISHED frozen normalization run, official rank,score columns are appended, read verbatim
    from the frozen result (never recomputed); if unpublished, those columns are omitted entirely.
    """
    frozen = _frozen_results(event)
    out = io.StringIO()
    writer = csv.writer(out)
    header = ["submission_ext_id", "title", "team", "track", "state"]
    if frozen is not None:
        header += ["rank", "score"]
    writer.writerow(header)
    for s in event.submissions.select_related("team", "track").order_by("id"):
        row = [s.ext_id, s.title, s.team.name, s.track.name, s.state]
        if frozen is not None:
            rank, score = frozen.get(s.ext_id, ("", ""))
            row += ["" if rank is None else rank, "" if score is None else score]
        writer.writerow(row)
    return out.getvalue()




