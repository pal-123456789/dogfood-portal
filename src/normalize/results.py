# src/normalize/results.py
"""Publish official, publicly visible results for an event (W1) -- the governance layer on top
of the signed, reproducible NormalizationRun (P2).

`publish_results` is the one write path. It is @transaction.atomic and, in a SINGLE transaction:
  1. builds + signs + persists a NormalizationRun (normalize.runs.publish_run), which itself
     co-commits a `normalization.published` audit event;
  2. appends the next-version ResultPublication for the event;
  3. co-commits a `results.published` audit event cross-linked to that row (audit_seq);
  4. flips Event.results_published so the rest of the app knows results are live.
If any step raises, ALL of it rolls back -- there is no run-without-publication, publication-
without-chain-record, or half-published-event window. The killer test proves this by making the
`results.published` append raise and asserting the run, the publication, and the flag are all absent.

Reads (`current_results`, `publication_history`) are DB-only and never recompute: the served ranking
is the FROZEN `result` of the referenced run, so what the public sees canonicalizes to the signed
`result_hash` and verifies offline with `python -m normalize.verify`. None of this touches the
acceptance checker's five routes, so replay stays 7/7.
"""
from __future__ import annotations

from django.db import models, transaction

from audit import service

from . import runs
from .models import NormalizationRun, ResultPublication


def _next_version(event_ext_id: str) -> int:
    top = (ResultPublication.objects.filter(event_ext_id=event_ext_id)
           .aggregate(m=models.Max("version"))["m"])
    return (top or 0) + 1


@transaction.atomic
def publish_results(event, *, key, note: str = "", status: str = ResultPublication.FINAL,
                    actor_user_id: str = "", n_boot: int = 1000, seed: int = 0, lam=None):
    """Build+sign a run and publish it as the event's official results. Returns (publication, run).

    Raises ValueError (via build_run) if the event has no ballots -- empty results are never
    published. `status` is 'final' by default; a 'provisional' publish is still a real, audited,
    versioned row (organizers can supersede it with a later final publish).
    """
    run = runs.publish_run(event, key=key, n_boot=n_boot, seed=seed, lam=lam)
    version = _next_version(event.ext_id)
    result = run.result
    ev = service.record_event(
        event_type="results.published",
        object_type="result_publication",
        object_id="%s:v%d" % (event.ext_id, version),
        actor_user_id=str(actor_user_id),
        payload={
            "event_ext_id": event.ext_id,
            "run_ext_id": run.run_ext_id,
            "version": version,
            "status": status,
            "result_hash": run.result_hash,
            "fingerprint": run.fingerprint,
            "engine_version": run.engine_version,
            "n_submissions": result.get("n_submissions", 0),
            "n_ballots": result.get("n_ballots", 0),
        },
    )
    pub = ResultPublication.objects.create(
        event_ext_id=event.ext_id, run_ext_id=run.run_ext_id, version=version,
        status=status, note=note, published_by=str(actor_user_id), audit_seq=ev.seq)
    if not event.results_published:
        event.results_published = True
        event.save(update_fields=["results_published"])
    return pub, run


def current_publication(event):
    """The latest (highest-version) publication for the event, or None if never published."""
    return (ResultPublication.objects.filter(event_ext_id=event.ext_id)
            .order_by("-version").first())


def publication_history(event):
    """Every publication for the event, newest first -- the append-only provenance trail."""
    return list(ResultPublication.objects.filter(event_ext_id=event.ext_id).order_by("-version"))


def current_results(event) -> dict:
    """The official published ranking (frozen run `result` + provenance), or {published: False}.

    Never recomputes: it returns the signed run's stored `result` verbatim, so the public view and
    an offline verifier agree by construction.
    """
    pub = current_publication(event)
    if pub is None:
        return {"published": False}
    run = NormalizationRun.objects.filter(run_ext_id=pub.run_ext_id).first()
    if run is None:  # co-committed, so unreachable in practice; degrade instead of 500.
        return {"published": False, "detail": "referenced run missing"}
    return {
        "published": True,
        "event": event.ext_id,
        "version": pub.version,
        "status": pub.status,
        "note": pub.note,
        "run_ext_id": run.run_ext_id,
        "engine_version": run.engine_version,
        "result_hash": run.result_hash,
        "inputs_hash": run.inputs_hash,
        "fingerprint": run.fingerprint,
        "lambda": run.lambda_value,
        "audit_seq": pub.audit_seq,
        "published_at": pub.recorded_at.isoformat(),
        "result": run.result,
    }
