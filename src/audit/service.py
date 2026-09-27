# src/audit/service.py
"""The atomic append -- the one function every audited mutation calls.

CONTRACT: call record_event() from INSIDE the same transaction.atomic() block that performs
the business write. It locks the singleton AuditHead FOR UPDATE, so concurrent writers get
distinct, contiguous seqs and a correctly linked prev_hash; and because the audit row and the
business row share the transaction, they commit or roll back TOGETHER. If the append raises
(or the business write does), neither is persisted -- there is no "business change without an
audit trail" window. The killer test monkeypatches the append to raise and asserts the
business row is absent.

Reads are cheap and DB-free downstream: chain_link is pure (audit/hashchain.py).
"""
from __future__ import annotations

from django.db import transaction
from django.utils import timezone

from .hashchain import SCHEMA_VERSION, chain_link
from .models import AuditEvent, AuditHead


def _locked_head() -> AuditHead:
    """The singleton head, locked FOR UPDATE. Seeded by migration 0001, so it always exists;
    a missing head is a real misconfiguration and should fail loudly rather than be papered over."""
    return AuditHead.objects.select_for_update().get(singleton=True)


@transaction.atomic
def record_event(*, event_type: str, object_type: str, object_id: str,
                 payload: dict, actor_user_id: str = "", actor_membership_id: str = "",
                 occurred_at: str | None = None) -> AuditEvent:
    """Append one hash-chained event and advance the head. Returns the created AuditEvent.

    Decorated with @transaction.atomic so it is safe to call standalone, but the intent is to
    call it WITHIN a caller's atomic block wrapping the business mutation -- nested atomic()
    becomes a savepoint, so a raise here still rolls the caller's write back too.
    """
    occurred_at = occurred_at or timezone.now().isoformat()
    head = _locked_head()
    seq = head.seq + 1
    header = {"schema_version": SCHEMA_VERSION,
              "instance_id": head.instance_id, "seq": seq,
              "event_type": event_type, "object_type": object_type, "object_id": str(object_id),
              "actor_user_id": str(actor_user_id), "actor_membership_id": str(actor_membership_id),
              "occurred_at": occurred_at}
    payload_digest, row_hash = chain_link(header=header, payload=payload, prev_hash=head.row_hash)
    event = AuditEvent.objects.create(
        instance_id=head.instance_id, seq=seq, event_type=event_type, object_type=object_type,
        object_id=str(object_id), actor_user_id=str(actor_user_id),
        actor_membership_id=str(actor_membership_id), occurred_at=occurred_at, payload=payload,
        payload_hash=payload_digest, prev_hash=head.row_hash, row_hash=row_hash)
    head.seq = seq
    head.row_hash = row_hash
    head.save(update_fields=["seq", "row_hash", "updated_at"])
    return event


def chain_rows() -> list[dict]:
    """Every event as offline-verifier row dicts, in seq order (AuditEvent.Meta.ordering)."""
    return [e.as_bundle_row() for e in AuditEvent.objects.all()]


def current_head() -> AuditHead | None:
    return AuditHead.objects.filter(singleton=True).first()
