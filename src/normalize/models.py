# src/normalize/models.py
"""NormalizationRun: one immutable, Ed25519-signed, reproducible normalization run.

A run is a published leaderboard pinned to the EXACT inputs that produced it. It is append-only
-- rows are never updated or deleted; re-normalizing creates a NEW run with a new run_ext_id.

Why this is the integrity capstone (P2), not just a cache:
  * `inputs` stores the weighted-composite ingredients verbatim -- every consumed ballot's raw
    1..5 scores AND the BallotRevision `version` it came from (Increment 5), plus the rubric
    weights and the display labels -- in a FROZEN order. So a run names the precise score history
    it read.
  * `result` stores the full leaderboard dict. `result_hash` binds its reproducible projection
    (normalize.engine.canonical_result), and `inputs_hash` binds `inputs`.
  * `signature` is an Ed25519 signature (normalize.signing, tag dogfood.normalize.run.v1) over
    engine_version + instance_id + event_ext_id + run_ext_id + inputs_hash + result_hash +
    created_at. Anyone holding the pinned public key can verify it; only the /state key-holder
    can sign. normalize.verify recomputes the ranking from `inputs` and confirms it canonicalizes
    to result_hash -- reproduction from first principles, not an asserted number.
  * `audit_seq` is the seq of the `normalization.published` event co-committed on the tamper-
    evident audit chain in the SAME transaction as this row (normalize.runs.publish_run), so a
    run and its chain record commit or roll back together -- exactly like record_ballot.

Honest scope (docs/THREAT-MODEL.md): the operator holds the signing key, so a signature is
retrospective, comparison-based evidence -- a run whose public key + fingerprint were pinned by an
independent party before judging cannot later be swapped for a different ranking without detection.
It does not bind the operator against themselves in real time.
"""
from django.db import models


class NormalizationRun(models.Model):
    run_ext_id = models.CharField(max_length=40, unique=True)
    engine_version = models.CharField(max_length=64)
    instance_id = models.CharField(max_length=64)
    event_ext_id = models.CharField(max_length=64)
    inputs_hash = models.CharField(max_length=64)
    result_hash = models.CharField(max_length=64)
    fingerprint = models.CharField(max_length=64)
    signature = models.CharField(max_length=128)
    lambda_value = models.FloatField()
    n_boot = models.PositiveIntegerField()
    seed = models.PositiveIntegerField()
    audit_seq = models.PositiveIntegerField()
    inputs = models.JSONField()
    result = models.JSONField()
    created_at = models.CharField(max_length=40)
    recorded_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        db_table = "normalization_run"
        ordering = ["id"]

    def __str__(self):
        return "%s (%s)" % (self.run_ext_id, self.event_ext_id)
