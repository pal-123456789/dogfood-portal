# src/normalize/runs.py
"""Build and publish a signed, reproducible normalization run (P2 of the integrity spine).

`build_run` performs NO writes: it computes the leaderboard with a PINNED lambda and assembles the
two hashed blobs -- `inputs` (the exact weighted ballots + rubric weights + labels that fed the
engine, in a FROZEN order, each ballot pinned to its BallotRevision version) and `result` (the
leaderboard dict) -- then signs the run. `publish_run` wraps the persist in ONE transaction.atomic()
block and co-commits a `normalization.published` event on the audit chain, so a run and its tamper-
evident chain record commit or roll back together, exactly like judging.record_ballot.

Determinism note: a signed run pins ballot ORDER (order_by submission, judge, id) as well as lambda
and seed, because the parametric bootstrap pairs its seeded noise draw positionally with the ballot
list. The live leaderboard view does not need this (its point estimates are order-invariant), which
is why the stricter ordering lives here and not in services.observed. Nothing here is on the
acceptance checker's five routes.
"""
from __future__ import annotations

import json
import secrets
from pathlib import Path

from django.db import models, transaction
from django.utils import timezone

from audit import service
from judging.models import Ballot

from . import engine, services, signing
from .models import NormalizationRun


def _ordered_inputs(event):
    """The frozen, deterministic (weights, display, ballots, y, jk, sk) a run commits to.

    Ballots are ordered by (submission ext_id, judge ext_id, id) and annotated with the current
    (max) BallotRevision version, so `inputs` names the precise score history the run consumed.
    """
    weights = services.rubric_weights(event)
    disp = services._display(event)
    qs = (Ballot.objects
          .filter(assignment__submission__event=event)
          .select_related("assignment__judge", "assignment__submission")
          .annotate(cur_version=models.Max("revisions__version"))
          .order_by("assignment__submission__ext_id", "assignment__judge__ext_id", "id"))
    y, jk, sk, ballots = [], [], [], []
    for b in qs:
        raw = {"functionality": b.functionality, "quality": b.quality, "innovation": b.innovation}
        judge, sub = b.assignment.judge.ext_id, b.assignment.submission.ext_id
        y.append(engine.composite(raw, weights))
        jk.append(judge)
        sk.append(sub)
        ballots.append({"submission": sub, "judge": judge, "version": int(b.cur_version or 1),
                        "functionality": b.functionality, "quality": b.quality,
                        "innovation": b.innovation})
    return weights, disp, ballots, y, jk, sk


def build_run(event, *, key, n_boot=1000, seed=0, lam=None):
    """Compute + sign a run WITHOUT touching the DB for writes. Returns a plain dict of fields.

    `lam=None` selects lambda by CV once and PINS it into the run, so reproduction never re-runs
    CV (a knife-edge grid tie could otherwise pick a neighbouring lambda on a different BLAS build).
    Raises ValueError if the event has no ballots -- an empty run is never published.
    """
    weights, disp, ballots, y, jk, sk = _ordered_inputs(event)
    if not y:
        raise ValueError("no ballots to normalize for event %s" % event.ext_id)
    if lam is None:
        lam, _table = engine.select_lambda(y, jk, sk, seed=seed)
    result = engine.compute_leaderboard(y, jk, sk, disp, n_boot=n_boot, seed=seed, lam=lam)
    inputs = {
        "engine_version": engine.ENGINE_VERSION,
        "event_ext_id": event.ext_id,
        "criteria": list(engine.CRITERIA),
        "weights": {c: float(weights[c]) for c in engine.CRITERIA},
        "n_boot": int(n_boot),
        "seed": int(seed),
        "lambda": float(lam),
        "ballots": ballots,
        "submissions": [{"submission": s, "title": t, "track": tr}
                        for s, (t, tr) in sorted(disp.items())],
    }
    inputs_hash = signing.content_hash("inputs", inputs)
    result_hash = signing.content_hash("result", engine.canonical_result(result))

    head = service.current_head()
    if head is None:
        raise RuntimeError("no AuditHead -- run migrate before publishing a normalization run")
    run_ext_id = "nrun_" + secrets.token_hex(8)
    created_at = timezone.now().isoformat()
    pub = key.public_key()
    signature = signing.sign_run(
        key, engine_version=engine.ENGINE_VERSION, instance_id=head.instance_id,
        event_ext_id=event.ext_id, run_ext_id=run_ext_id,
        inputs_hash=inputs_hash, result_hash=result_hash, created_at=created_at)
    return {
        "run_ext_id": run_ext_id, "engine_version": engine.ENGINE_VERSION,
        "instance_id": head.instance_id, "event_ext_id": event.ext_id,
        "inputs_hash": inputs_hash, "result_hash": result_hash,
        "fingerprint": signing.public_fingerprint(pub), "signature": signature,
        "lambda_value": float(lam), "n_boot": int(n_boot), "seed": int(seed),
        "inputs": inputs, "result": result, "created_at": created_at,
    }


@transaction.atomic
def publish_run(event, *, key, n_boot=1000, seed=0, lam=None):
    """Sign a run and persist it, co-committing a `normalization.published` audit event in the
    SAME transaction. If either write raises, both roll back -- there is no run-without-chain-record
    (or chain-record-without-run) window. Returns the created NormalizationRun.
    """
    built = build_run(event, key=key, n_boot=n_boot, seed=seed, lam=lam)
    ev = service.record_event(
        event_type="normalization.published",
        object_type="normalization_run",
        object_id=built["run_ext_id"],
        payload={
            "engine_version": built["engine_version"],
            "event_ext_id": built["event_ext_id"],
            "run_ext_id": built["run_ext_id"],
            "inputs_hash": built["inputs_hash"],
            "result_hash": built["result_hash"],
            "fingerprint": built["fingerprint"],
            "lambda": built["lambda_value"],
            "n_boot": built["n_boot"],
            "seed": built["seed"],
            "n_ballots": built["result"]["n_ballots"],
            "n_submissions": built["result"].get("n_submissions", 0),
        },
    )
    return NormalizationRun.objects.create(
        run_ext_id=built["run_ext_id"], engine_version=built["engine_version"],
        instance_id=built["instance_id"], event_ext_id=built["event_ext_id"],
        inputs_hash=built["inputs_hash"], result_hash=built["result_hash"],
        fingerprint=built["fingerprint"], signature=built["signature"],
        lambda_value=built["lambda_value"], n_boot=built["n_boot"], seed=built["seed"],
        audit_seq=ev.seq, inputs=built["inputs"], result=built["result"],
        created_at=built["created_at"])


def export_bundle(run, pub, dest) -> Path:
    """Write the four-file offline bundle (run.json, inputs.json, result.json, public-key.pem).

    Uses the in-memory NormalizationRun (its JSON fields are the exact dicts that were hashed), so
    the written inputs/result canonicalize to the stored hashes regardless of file formatting.
    """
    d = Path(dest)
    d.mkdir(parents=True, exist_ok=True)
    run_json = {
        "engine_version": run.engine_version, "instance_id": run.instance_id,
        "event_ext_id": run.event_ext_id, "run_ext_id": run.run_ext_id,
        "inputs_hash": run.inputs_hash, "result_hash": run.result_hash,
        "fingerprint": run.fingerprint, "signature": run.signature,
        "lambda": run.lambda_value, "n_boot": run.n_boot, "seed": run.seed,
        "created_at": run.created_at, "audit_seq": run.audit_seq,
    }
    (d / "run.json").write_text(json.dumps(run_json, indent=2), encoding="utf-8")
    (d / "inputs.json").write_text(
        json.dumps(run.inputs, indent=2, sort_keys=True), encoding="utf-8")
    (d / "result.json").write_text(
        json.dumps(run.result, indent=2, sort_keys=True), encoding="utf-8")
    (d / "public-key.pem").write_bytes(signing.public_key_to_pem(pub))
    return d
