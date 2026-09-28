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
from django import forms
from django.contrib import admin

from portal.admin_mixins import ReadOnlyModelAdmin

from .models import Ballot, BallotRevision, JudgeAssignment, JudgeRecusal, RubricWeight

import math


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


class RubricWeightForm(forms.ModelForm):
    """Admin-side guard so a negative or non-finite weight is refused with a friendly validation
    error instead of a raw IntegrityError from the DB CheckConstraint (the deep backstop). Mirrors
    the set_rubric_weights service rule: a criterion may be 0 (dropped) but never negative, and NaN
    or inf -- which a FloatField will otherwise accept -- are rejected here too.
    """
    class Meta:
        model = RubricWeight
        fields = "__all__"

    def clean_weight(self):
        w = self.cleaned_data.get("weight")
        if w is None or not math.isfinite(w):
            raise forms.ValidationError("Weight must be a finite number.")
        if w < 0:
            raise forms.ValidationError(
                "Weight must be non-negative (a criterion may be 0 to drop it, never negative).")
        return w


@admin.register(RubricWeight)
class RubricWeightAdmin(admin.ModelAdmin):
    form = RubricWeightForm
    list_display = ("event", "criterion", "weight")
    list_filter = ("event",)
    search_fields = ("criterion",)
    autocomplete_fields = ("event",)


@admin.register(JudgeRecusal)
class JudgeRecusalAdmin(admin.ModelAdmin):
    """Organizer-managed conflicts of interest. Freely add/remove-able: a recusal changes only who
    the auto-assignment planner MAY assign, never any recorded ballot, so -- unlike a scored
    assignment -- it carries no append-only history to protect. raw_id_fields keep the judge/team
    pickers scalable and avoid depending on another app's admin search configuration."""
    list_display = ("event", "judge", "team", "reason", "created_at")
    list_filter = ("event",)
    search_fields = ("judge__user__email", "team__ext_id", "reason")
    raw_id_fields = ("event", "judge", "team")
