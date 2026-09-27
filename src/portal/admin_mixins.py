# src/portal/admin_mixins.py
"""Shared admin base for the append-only / signed integrity tables.

The audit chain, the ballots and their revision history, and the normalization runs and their
publications are written *only* through the domain writers (judging.services.record_ballot, the
audit hash-chain, the normalize run/publish path). Those writers keep the hashes, versions and
Ed25519 signatures internally consistent. If the Django admin could add, edit or delete those
rows, an operator with admin access could silently fork the hash chain, rewrite a score without a
revision, or delete a signed run -- defeating the integrity guarantees the platform advertises.

`ReadOnlyModelAdmin` closes that hole: the rows remain fully *viewable* in the admin (the default
view permission is untouched, so operators can inspect the chain), but add / change / delete are
denied unconditionally, independent of any granted model permission.
"""
from django.contrib import admin


class ReadOnlyModelAdmin(admin.ModelAdmin):
    """Inspect-only admin: view is allowed, all writes are denied."""

    def has_add_permission(self, request):
        return False

    def has_change_permission(self, request, obj=None):
        return False

    def has_delete_permission(self, request, obj=None):
        return False
