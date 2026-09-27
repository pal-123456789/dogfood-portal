# src/normalize/admin.py
"""Inspect-only admin for the signed, reproducible normalization runs and their publications.

Each run pins its inputs and carries an Ed25519 signature over a domain-tagged pre-image; each
publication records which run became provisional/final. They are written only through the
normalize run/publish path (which co-commits an audit event), so the admin surfaces them read-only
-- an operator can inspect a run's hashes and signature but cannot forge, edit or delete one.
"""
from django.contrib import admin

from portal.admin_mixins import ReadOnlyModelAdmin

from .models import NormalizationRun, ResultPublication


@admin.register(NormalizationRun)
class NormalizationRunAdmin(ReadOnlyModelAdmin):
    list_display = ("run_ext_id", "engine_version", "event_ext_id", "result_hash", "recorded_at")
    list_filter = ("engine_version", "event_ext_id")
    search_fields = ("run_ext_id", "event_ext_id", "result_hash", "fingerprint")
    ordering = ("id",)


@admin.register(ResultPublication)
class ResultPublicationAdmin(ReadOnlyModelAdmin):
    list_display = ("event_ext_id", "run_ext_id", "version", "status", "recorded_at")
    list_filter = ("status",)
    search_fields = ("event_ext_id", "run_ext_id")
