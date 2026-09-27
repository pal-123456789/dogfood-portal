# src/submissions/admin.py
"""Admin for submissions. Editable for organizer housekeeping; the participant-facing write path is
the create-only submit view, not this admin."""
from django.contrib import admin

from .models import Submission


@admin.register(Submission)
class SubmissionAdmin(admin.ModelAdmin):
    list_display = ("ext_id", "title", "event", "team", "track", "state", "submitted_at")
    list_filter = ("state", "event", "track")
    search_fields = ("ext_id", "title", "summary", "repo_url")
    autocomplete_fields = ("event", "team", "track")
