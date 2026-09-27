# src/audit/admin.py
"""Inspect-only admin for the tamper-evident audit spine.

Both tables are append-only and hash-chained. They are surfaced read-only so an operator can
inspect the chain (seq, row_hash, prev_hash) from the admin without ever being able to insert,
edit or delete an event -- writes flow only through the audit writer, which recomputes the chain.
"""
from django.contrib import admin

from portal.admin_mixins import ReadOnlyModelAdmin

from .models import AuditEvent, AuditHead


@admin.register(AuditHead)
class AuditHeadAdmin(ReadOnlyModelAdmin):
    list_display = ("instance_id", "seq", "row_hash", "updated_at")
    search_fields = ("instance_id", "row_hash")


@admin.register(AuditEvent)
class AuditEventAdmin(ReadOnlyModelAdmin):
    list_display = ("seq", "event_type", "object_type", "object_id", "occurred_at")
    list_filter = ("event_type", "object_type")
    search_fields = ("event_type", "object_type", "object_id", "row_hash", "prev_hash")
    ordering = ("seq",)
