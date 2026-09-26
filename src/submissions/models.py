# src/submissions/models.py
"""A project submitted by a team to a track within an event.

No unique(team, track, title) constraint ON PURPOSE: the released fixture plants a
within-track duplicate (tm_07 submits "Dry Harbour" as both prj_07 and prj_41 in trk_03),
and a uniqueness guard here would crash the seed. Duplicate control lives in the
normalizer as a diagnostic, not in a DB constraint. `title`/`summary` are load-bearing:
check 2 reads titles off the gallery, check 3 POSTs {"title","summary"}.
"""
from django.db import models

from events.models import Event, Team, Track


class Submission(models.Model):
    DRAFT, SUBMITTED = "draft", "submitted"
    STATES = [(DRAFT, "draft"), (SUBMITTED, "submitted")]

    ext_id = models.CharField(max_length=64, unique=True)
    event = models.ForeignKey(Event, on_delete=models.CASCADE, related_name="submissions")
    team = models.ForeignKey(Team, on_delete=models.CASCADE, related_name="submissions")
    track = models.ForeignKey(Track, on_delete=models.PROTECT, related_name="submissions")
    title = models.CharField(max_length=200)
    summary = models.TextField(blank=True, default="")
    repo_url = models.URLField(blank=True, default="", max_length=500)
    state = models.CharField(max_length=16, choices=STATES, default=DRAFT)
    submitted_at = models.DateTimeField(null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        db_table = "submission"
        ordering = ["id"]   # oldest-first: prj_01/02/03 (the check-2 titles) sort to the top

    def __str__(self):
        return self.title
