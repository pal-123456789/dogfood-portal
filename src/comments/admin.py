# src/comments/admin.py
"""Admin for project comments: read-oriented visibility for staff/organizers. The moderation path
is the organizer-only /comments/<ext_id>/moderate endpoint (soft-hide), not this admin; the `hidden`
flag is shown and filterable here so staff can see what has been moderated. Comments have no
append-only child history beneath them, so no cascade delete-guard is needed (unlike Submission)."""
from django.contrib import admin

from .models import Comment


@admin.register(Comment)
class CommentAdmin(admin.ModelAdmin):
    list_display = ("ext_id", "submission", "author", "hidden", "created_at")
    list_filter = ("hidden",)
    search_fields = ("ext_id", "body")
    readonly_fields = ("ext_id", "created_at")
