# src/records/services.py
"""Build and verify signed participation records over the live event graph (T4).

A record is computed on the fly (there is NO records model and NO migration): the builders read the
event graph, assemble exactly the fields the record displays, and attach one Ed25519 signature over
them via records.signing. Signing reuses the ONE operator /state key (audit.keys.ensure_private_key
-> the same key that signs audit checkpoints, normalization runs and invites); this module adds no
new key and no new signature scheme, only a new frozen domain tag (records.signing._RECORD_TAG).

Honest scope: a record attests only that the operator's key signed the listed participation facts
(event, role, subject, items, issued-at). It is NOT a measure of merit and NOT fraud detection, and
-- because the operator holds the private key -- it is decisive to a third party only if they pinned
the public key + fingerprint before judging (docs/THREAT-MODEL.md). Records carry NO scores/ballots.
"""
from __future__ import annotations

from django.utils import timezone

from audit import keys
from events.models import EventMembership, TeamMember

from . import signing

RECORD_KIND = "dogfood.participation-record.v1"

# One honest attestation string, itself a signed field, so a reader cannot doctor the framing (e.g.
# to claim it proves merit) without failing verification.
ATTESTATION = (
    "Attests only that the operator's Ed25519 key signed the participation facts listed in this "
    "record (the named event, the subject's role, and the listed items). It is a signed statement "
    "of participation -- not a measure of merit and not fraud detection. Because the operator holds "
    "the private key, it is decisive to a third party only if the public key and its fingerprint "
    "were pinned before judging."
)


def _body(*, role, event, subject, items):
    """Assemble exactly the RECORD_FIELDS the record displays (no scores, no ballots ever)."""
    return {
        "kind": RECORD_KIND,
        "role": role,
        "event_ext_id": event.ext_id,
        "event_name": event.name,
        "subject": subject,
        "items": items,
        "issued_at": timezone.now().isoformat(),  # aware (settings.USE_TZ)
        "attestation": ATTESTATION,
    }


def _sign(body):
    """Attach the signature block to a record body using the SAME /state operator key.

    The signature covers exactly `body` (records.signing.RECORD_FIELDS). signer_fingerprint names
    the public key a verifier must pin; verify_record_json re-derives it from our key, so a doctored
    fingerprint is rejected even though the Ed25519 check binds our key regardless of the claim.
    """
    key, _created = keys.ensure_private_key()
    pub = key.public_key()
    record = dict(body)
    record["signature"] = {
        "algorithm": "ed25519",
        "value": signing.sign_record(key, **body),
        "signer_fingerprint": signing.public_fingerprint(pub),
    }
    return record


def judge_record(event, membership):
    """The judge's own signed record: the submissions ASSIGNED to them (ext_id + title only).

    No scores, no ballots -- only which submissions the judge was assigned in this event.
    """
    items = [{"ext_id": a.submission.ext_id, "title": a.submission.title}
             for a in (membership.assignments
                       .select_related("submission").order_by("submission_id"))]
    return _sign(_body(role=EventMembership.JUDGE, event=event,
                       subject={"membership_ext_id": membership.ext_id}, items=items))


def participant_team(user, event):
    """The user's team in this event (first by id), or None -- mirrors submissions.services."""
    tm = (TeamMember.objects.filter(user=user, team__event=event)
          .select_related("team").order_by("id").first())
    return tm.team if tm else None


def participant_record(event, user):
    """The participant's own signed record: their team name + their team's project titles.

    No scores, no ballots -- only the team and the projects it entered in this event.
    """
    team = participant_team(user, event)
    items = ([{"ext_id": s.ext_id, "title": s.title}
              for s in team.submissions.order_by("id")] if team else [])
    subject = {"team_ext_id": team.ext_id if team else "",
               "team_name": team.name if team else ""}
    return _sign(_body(role=EventMembership.PARTICIPANT, event=event,
                       subject=subject, items=items))


def verify_record_json(record) -> bool:
    """Recompute the signed material from a record's OWN fields and verify it against OUR key.

    Stateless: reads nothing but the deployment key and writes nothing. Returns False (never raises)
    for a non-dict, a missing/for-a-different-key signature, a doctored field, or a malformed sig.
    """
    if not isinstance(record, dict):
        return False
    sig = record.get("signature")
    if not isinstance(sig, dict) or not isinstance(sig.get("value"), str):
        return False
    try:
        key, _created = keys.ensure_private_key()
    except Exception:
        return False
    pub = key.public_key()
    claimed_fp = sig.get("signer_fingerprint")
    if claimed_fp is not None and claimed_fp != signing.public_fingerprint(pub):
        return False
    return signing.verify_record(pub, signature=sig["value"],
                                 **{f: record.get(f) for f in signing.RECORD_FIELDS})
