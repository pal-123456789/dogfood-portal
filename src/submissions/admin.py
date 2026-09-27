# src/submissions/admin.py
"""Admin for submissions. Editable for organizer housekeeping; the participant-facing write path is
the create-only submit view, not this admin. One guard: deleting a submission whose assignments are
*scored* is refused (its Ballot -> BallotRevision history cascades off it), so append-only ballot
history cannot be destroyed through the admin UI (see `ScoredCascadeDeleteGuard`; THREAT-MODEL
A7/A8)."""
from django.contrib import admin

from judging.models import Ballot
from portal.admin_mixins import ScoredCascadeDeleteGuard

from .models import Submission


@admin.register(Submission)
class SubmissionAdmin(ScoredCascadeDeleteGuard, admin.ModelAdmin):
    list_display = ("ext_id", "title", "event", "team", "track", "state", "submitted_at")
    list_filter = ("state", "event", "track")
    search_fields = ("ext_id", "title", "summary", "repo_url")
    autocomplete_fields = ("event", "team", "track")

    def cascades_into_scored(self, obj):
        return Ballot.objects.filter(assignment__submission=obj).exists()
