# src/judging/admin.py
"""Admin for the judging tables.

Assignments and rubric weights are configuration an organizer manages, so they are editable here --
except that a SCORED assignment cannot be deleted (its Ballot + append-only BallotRevision history
cascade off it), mirroring the control-room unassign rule. Ballots and their revision history are
NOT editable at all: a score is only ever written through judging.services.record_ballot, which
appends an immutable BallotRevision and chains an audit event in one atomic step. Exposing them as
editable admin rows would let an operator change a score without a revision or an audit trail, so
both are registered inspect-only.
"""
from django.contrib import admin

from portal.admin_mixins import ReadOnlyModelAdmin

from .models import Ballot, BallotRevision, JudgeAssignment, RubricWeight


@admin.register(JudgeAssignment)
class JudgeAssignmentAdmin(admin.ModelAdmin):
    list_display = ("judge", "submission", "created_at")
    list_filter = ("judge__event",)
    search_fields = ("judge__user__email", "submission__ext_id", "submission__title")
    autocomplete_fields = ("judge", "submission")

    # A scored assignment is permanent even here. Ballot + append-only BallotRevision history
    # cascade off JudgeAssignment, so deleting a scored one in admin would destroy score history --
    # exactly what the control-room unassign path refuses. We refuse it too: a single UNSCORED row
    # stays deletable, but a scored row cannot be removed (not even by a superuser).
    def has_delete_permission(self, request, obj=None):
        if obj is not None and Ballot.objects.filter(assignment=obj).exists():
            return False
        return super().has_delete_permission(request, obj)

    def get_actions(self, request):
        # Drop bulk "delete selected": it bypasses per-object has_delete_permission and could
        # cascade-delete scored assignments. Removal stays a per-row, unscored-only operation.
        actions = super().get_actions(request)
        actions.pop("delete_selected", None)
        return actions


@admin.register(Ballot)
class BallotAdmin(ReadOnlyModelAdmin):
    list_display = ("assignment", "functionality", "quality", "innovation", "updated_at")
    search_fields = ("assignment__submission__ext_id",)


@admin.register(BallotRevision)
class BallotRevisionAdmin(ReadOnlyModelAdmin):
    list_display = ("ballot", "version", "functionality", "quality", "innovation", "created_at")
    list_filter = ("version",)
    search_fields = ("ballot__assignment__submission__ext_id",)


@admin.register(RubricWeight)
class RubricWeightAdmin(admin.ModelAdmin):
    list_display = ("event", "criterion", "weight")
    list_filter = ("event",)
    search_fields = ("criterion",)
    autocomplete_fields = ("event",)
