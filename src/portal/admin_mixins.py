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

`ScoredCascadeDeleteGuard` closes a related, quieter hole. A `Ballot` and its append-only
`BallotRevision` history are `CASCADE` off `JudgeAssignment`, which is itself `CASCADE` off
`EventMembership` (the judge) and `Submission`, and those off `Event` / `Team` / `AppUser` above
them. Deleting any such *parent* row in the admin would silently take a scored ballot's history
with it. This mixin refuses to delete a row whose cascade reaches a *scored* assignment and drops
the bulk `delete_selected` action; a parent with no scored descendant stays deletable. It binds the
application (admin UI) surface only -- a party with direct database access is the A8 operator
boundary and is out of scope by construction (see docs/THREAT-MODEL.md A7/A8).
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


class ScoredCascadeDeleteGuard:
    """Delete-guard for a *parent* row whose cascade would reach a SCORED assignment.

    Mixed in *before* ``admin.ModelAdmin`` so it overrides via MRO. A `Ballot` and its append-only
    `BallotRevision` history cascade off `JudgeAssignment`, which is `CASCADE` off `EventMembership`
    (the judge) and `Submission`, and those off `Event` / `Team` / `AppUser`. Deleting such a parent
    in the admin would silently destroy scored ballot history -- the same loss the control-room
    unassign and `JudgeAssignmentAdmin` already refuse on the *direct* row (THREAT-MODEL A7/A8).

    Each subclass implements `cascades_into_scored(obj)`; a parent with no scored descendant stays
    deletable, and the bulk `delete_selected` action is dropped because it bypasses the per-object
    check. This guards the admin UI only, never direct database access (the A8 operator boundary).
    """

    def cascades_into_scored(self, obj):
        raise NotImplementedError(
            "%s must implement cascades_into_scored(obj)" % type(self).__name__)

    def has_delete_permission(self, request, obj=None):
        if obj is not None and self.cascades_into_scored(obj):
            return False
        return super().has_delete_permission(request, obj)

    def get_actions(self, request):
        actions = super().get_actions(request)
        actions.pop("delete_selected", None)
        return actions
