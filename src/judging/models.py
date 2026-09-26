# src/judging/models.py
"""Judging: who may score what (JudgeAssignment), and the score itself (Ballot).

Scores are integers 1..5 per criterion (functionality, quality, innovation), matching the
fixture and the spec rubric. The 1..5 bound is enforced in the record_ballot service
(judging/services.py); a DB CheckConstraint is added in a later, non-destructive migration.
One ballot per assignment (OneToOne). Assignment ownership — not the URL — decides who can
read a ballot, which is the crux of checks 4/5/6.
"""
from django.db import models

from events.models import Event, EventMembership
from submissions.models import Submission


class JudgeAssignment(models.Model):
    judge = models.ForeignKey(EventMembership, on_delete=models.CASCADE,
                              related_name="assignments")
    submission = models.ForeignKey(Submission, on_delete=models.CASCADE,
                                   related_name="assignments")
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        db_table = "judge_assignment"
        constraints = [
            models.UniqueConstraint(fields=["judge", "submission"],
                                    name="uniq_judge_submission"),
        ]

    def __str__(self):
        return "%s -> %s" % (self.judge_id, self.submission_id)


class Ballot(models.Model):
    assignment = models.OneToOneField(JudgeAssignment, on_delete=models.CASCADE,
                                       related_name="ballot")
    functionality = models.PositiveSmallIntegerField()
    quality = models.PositiveSmallIntegerField()
    innovation = models.PositiveSmallIntegerField()
    comment = models.TextField(blank=True, default="")
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        db_table = "ballot"

    def __str__(self):
        return "ballot#%s" % self.pk


class RubricWeight(models.Model):
    """Organizer-configurable criterion weights; the seed installs equal weights, and the
    weighted 0..5 score is derived from these (never hard-coded in the view)."""
    event = models.ForeignKey(Event, on_delete=models.CASCADE, related_name="rubric_weights")
    criterion = models.CharField(max_length=32)
    weight = models.FloatField(default=1.0)

    class Meta:
        db_table = "rubric_weight"
        constraints = [
            models.UniqueConstraint(fields=["event", "criterion"],
                                    name="uniq_event_criterion"),
        ]

    def __str__(self):
        return "%s=%s" % (self.criterion, self.weight)
