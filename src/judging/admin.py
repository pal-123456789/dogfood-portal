# src/judging/admin.py
"""Admin for the judging tables.

Assignments and rubric weights are configuration an organizer manages, so they are editable here.
Ballots and their revision history are NOT: a score is only ever written through
judging.services.record_ballot, which appends an immutable BallotRevision and chains an audit
event in one atomic step. Exposing them as editable admin rows would let an operator change a
score without a revision or an audit trail, so both are registered inspect-only.
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
