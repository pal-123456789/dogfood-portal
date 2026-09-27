# src/audit/models.py
"""Tamper-evident audit chain -- the DB half of the integrity spine.

Two tables, and the split between them is the whole point:

  AuditHead   ONE row (a DB-enforced singleton). It is the chain TIP: the last committed
              `seq` and its `row_hash`, plus a per-deployment `instance_id`. Every append
              locks this row FOR UPDATE, so seq issuance and prev_hash linkage are
              serialized even under concurrent writers. We NEVER use an auto-increment PK
              as the chain seq: Postgres sequences gap on rollback and don't reflect commit
              order, which would silently punch holes in the chain.

  AuditEvent  One immutable row per audited action: the frozen schema-v1 header
              (hashchain.HEADER_FIELDS) + the JSON payload + the three chain hashes
              (payload_hash, prev_hash, row_hash). UNIQUE(instance_id, seq) makes a
              duplicate or back-filled seq impossible at the DB level.

`occurred_at` is stored as the exact canonical ISO-8601 STRING that was hashed (not a
DateTimeField): the signed bytes must reproduce byte-for-byte when an independent party
re-verifies an exported bundle, and a timezone/precision round-trip through a DateTimeField
would break that. `payload` is jsonb; key order is irrelevant because hashchain.canonical_bytes
sorts keys, so a re-verify recomputes the same digest regardless of how Postgres stored it.

Honest scope (docs/THREAT-MODEL.md): an in-DB chain detects insert/delete/reorder/edit within
the stored rows and app- or DB-role tampering; it does NOT bind the operator, who holds the
DB password and the signing key. Binding requires a checkpoint that left the operator's
control before a disputed change (see audit/receipts.py + audit/verify.py).
"""
from django.db import models

from .hashchain import GENESIS_HASH, SCHEMA_VERSION


class AuditHead(models.Model):
    """The single chain tip. `singleton` is unique and always True, so at most one row exists."""
    singleton = models.BooleanField(default=True, unique=True, editable=False)
    instance_id = models.CharField(max_length=64)
    seq = models.BigIntegerField(default=0)                       # last committed seq; next = seq+1
    row_hash = models.CharField(max_length=64, default=GENESIS_HASH)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        db_table = "audit_head"

    def __str__(self):
        return "head(%s)@seq=%d" % (self.instance_id, self.seq)


class AuditEvent(models.Model):
    """One immutable, hash-chained audit row. Never updated or deleted in normal operation."""
    schema_version = models.IntegerField(default=SCHEMA_VERSION)
    instance_id = models.CharField(max_length=64)
    seq = models.BigIntegerField()
    event_type = models.CharField(max_length=64)
    object_type = models.CharField(max_length=64)
    object_id = models.CharField(max_length=128)
    actor_user_id = models.CharField(max_length=64, blank=True, default="")
    actor_membership_id = models.CharField(max_length=64, blank=True, default="")
    occurred_at = models.CharField(max_length=40)                 # canonical ISO-8601, as hashed
    payload = models.JSONField(default=dict)
    payload_hash = models.CharField(max_length=64)
    prev_hash = models.CharField(max_length=64)
    row_hash = models.CharField(max_length=64)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        db_table = "audit_event"
        ordering = ["seq"]
        constraints = [
            models.UniqueConstraint(fields=["instance_id", "seq"],
                                    name="uniq_audit_instance_seq"),
        ]

    def __str__(self):
        return "event#%d %s %s" % (self.seq, self.event_type, self.object_id)

    def header(self):
        """The schema-v1 header dict exactly as hashed (hashchain.HEADER_FIELDS order-agnostic)."""
        return {"schema_version": self.schema_version, "instance_id": self.instance_id,
                "seq": self.seq, "event_type": self.event_type, "object_type": self.object_type,
                "object_id": self.object_id, "actor_user_id": self.actor_user_id,
                "actor_membership_id": self.actor_membership_id, "occurred_at": self.occurred_at}

    def as_bundle_row(self):
        """The JSONL row shape the offline verifier (audit/verify.py) expects."""
        return dict(self.header(), prev_hash=self.prev_hash, row_hash=self.row_hash,
                    payload_hash=self.payload_hash, payload=self.payload)
