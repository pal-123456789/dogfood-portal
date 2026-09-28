# src/judging/models.py
"""Judging: who may score what (JudgeAssignment), the current score (Ballot), and the
append-only history behind it (BallotRevision).

Scores are integers 1..5 per criterion (functionality, quality, innovation), matching the
fixture and the spec rubric. The 1..5 bound is enforced three ways, deepest last: in the
record_ballot service (judging/services.py), and — as of migration 0002 — by a DB
CheckConstraint on BOTH Ballot and every BallotRevision, so a value outside 1..5 cannot be
persisted even by a writer that bypasses the service.

One ballot per assignment (OneToOne); Ballot holds the CURRENT (latest) score as a
denormalized read-pointer so existing readers stay byte-identical. Every write also appends
an immutable BallotRevision (UNIQUE(ballot, version)); revisions are never updated or
deleted, so the full score history is reconstructable and a silent overwrite is impossible
to hide — the tamper-evident audit chain records each `ballot.recorded` event with its
version. Assignment ownership — not the URL — decides who can read a ballot, which is the
crux of checks 4/5/6.
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
        constraints = [
            models.CheckConstraint(
                condition=(models.Q(functionality__range=(1, 5))
                           & models.Q(quality__range=(1, 5))
                           & models.Q(innovation__range=(1, 5))),
                name="ck_ballot_scores_1_5"),
        ]

    def __str__(self):
        return "ballot#%s" % self.pk


class BallotRevision(models.Model):
    """One immutable score revision. Append-only: rows are never updated or deleted, and
    UNIQUE(ballot, version) makes each version write-once, so the DB itself refuses a silent
    overwrite of history. Ballot mirrors the highest-version row as its denormalized current
    value; the audit chain records the matching version on every `ballot.recorded` event.
    """
    ballot = models.ForeignKey(Ballot, on_delete=models.CASCADE, related_name="revisions")
    version = models.PositiveIntegerField()
    functionality = models.PositiveSmallIntegerField()
    quality = models.PositiveSmallIntegerField()
    innovation = models.PositiveSmallIntegerField()
    comment = models.TextField(blank=True, default="")
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        db_table = "ballot_revision"
        constraints = [
            models.UniqueConstraint(fields=["ballot", "version"],
                                    name="uniq_ballot_version"),
            models.CheckConstraint(
                condition=(models.Q(functionality__range=(1, 5))
                           & models.Q(quality__range=(1, 5))
                           & models.Q(innovation__range=(1, 5))),
                name="ck_ballotrevision_scores_1_5"),
        ]

    def __str__(self):
        return "ballot#%s v%s" % (self.ballot_id, self.version)


class RubricWeight(models.Model):
    """Organizer-configurable criterion weights; the seed installs equal weights, and the
    weighted 0..5 score is derived from these (never hard-coded in the view).

    Weight is bounded non-negative three ways, deepest last: the set_rubric_weights service
    (rejects negative/NaN/inf and a zero sum), the admin form's clean_weight, and a DB
    CheckConstraint(weight >= 0) so a value below zero cannot be persisted even by a writer that
    bypasses the service. A single weight MAY be 0 (drop a criterion); the "not all zero" rule is
    cross-row, enforced by the service and the engine's den==0 guard, not by this per-row check.
    """
    event = models.ForeignKey(Event, on_delete=models.CASCADE, related_name="rubric_weights")
    criterion = models.CharField(max_length=32)
    weight = models.FloatField(default=1.0)

    class Meta:
        db_table = "rubric_weight"
        constraints = [
            models.UniqueConstraint(fields=["event", "criterion"],
                                    name="uniq_event_criterion"),
            models.CheckConstraint(condition=models.Q(weight__gte=0),
                                   name="ck_rubric_weight_nonneg"),
        ]

    def __str__(self):
        return "%s=%s" % (self.criterion, self.weight)
