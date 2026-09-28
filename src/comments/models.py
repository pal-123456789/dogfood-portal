# src/comments/models.py
"""Project comments -- a public discussion thread on a submission, with organizer moderation.

Two decisions mirror the rest of the graph. (1) `author` is SET_NULL + nullable so a comment
SURVIVES the deletion of its author (the thread is not silently rewritten when a user is
removed); posting, however, always requires an authenticated author (enforced in the view +
service), so a NULL author only ever means "the account was later deleted", never "posted
anonymously". (2) `hidden` is a SOFT moderation state, never a row delete -- the same
append-only / non-destructive stance the submission-withdraw flow and the admin cascade guards
take. Hiding a comment drops it from the public list but leaves the row (and its `comment.hidden`
audit event) intact.

These endpoints live under /comments/ and are OFF the five flat acceptance-checker routes, so no
byte of the checker surface moves.
"""
from django.conf import settings
from django.db import models

from submissions.models import Submission


class Comment(models.Model):
    ext_id = models.CharField(max_length=64, unique=True)
    submission = models.ForeignKey(
        Submission, on_delete=models.CASCADE, related_name="comments")
    author = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.SET_NULL, null=True, blank=True,
        related_name="comments_authored")
    body = models.TextField()
    hidden = models.BooleanField(default=False)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        db_table = "project_comment"
        ordering = ["id"]   # oldest-first, stable thread order

    def __str__(self):
        return "%s on %s" % (self.ext_id, self.submission_id)
