# src/normalize/views.py
"""Normalization routes: an organizer-only WORKBENCH and a public RESULTS surface.

Two audiences, one clean split (docs/THREAT-MODEL.md W1):
  * Organizer-only (gated 401/403 like the CSV export): `leaderboard` / `leaderboard_json` show the
    live, recomputed normalized ranking -- results-in-waiting -- and `results_publish` is the publish
    console. `diagnostics` / `diagnostics_json` are the review panel (residuals, decision influence,
    coverage -- NOT fraud detection). Severity-adjustment can reshuffle the podium, so none is public.
  * Public: `results` / `results_json` serve ONLY what an organizer has explicitly published, and
    they serve the signed run's FROZEN `result` verbatim (never a recompute), so what the world sees
    canonicalizes to the signed `result_hash` and verifies offline. Before the first publish they say
    so with HTTP 200 -- an event's rankings are simply not public until the organizer publishes them.

Every route here lives under /normalize/ and is NOT one of the five the acceptance checker hits, so
nothing in this module can move replay off 7/7.
"""
from django.contrib.auth.views import redirect_to_login
from django.http import HttpResponse, JsonResponse
from django.shortcuts import redirect, render
from django.views.decorators.http import require_http_methods

from audit import keys
from events.models import EventMembership
from events import services as events_services
from submissions import services as submissions_services

from . import explain as explain_mod
from . import results as results_svc  # module aliased so the public `results` view can keep its name
from . import services
from .models import ResultPublication


def _gate(request):
    """Return (event, error): error is None for an organizer, else a (message, status) pair."""
    event = services.current_event()
    if event is None:
        return None, ("no event configured", 404)
    if not (request.user and request.user.is_authenticated):
        return event, ("authentication required", 401)
    is_org = EventMembership.objects.filter(
        user=request.user, event=event, role=EventMembership.ORGANIZER).exists()
    if not is_org:
        return event, ("organizer only", 403)
    return event, None


def _result_state(data):
    """Map current_results() output to a governance-banner state for the public page:
    official / provisional (published, by status), degraded (run missing), or unpublished."""
    if not data.get("published"):
        return "degraded" if data.get("detail") else "unpublished"
    return "official" if data.get("status") == ResultPublication.FINAL else "provisional"


def _pairwise_grid(report):
    """Adapt services.pairwise_report() into rows of pre-shaded cells for the template.

    CSP is script-src 'self', so there is no JS to color a heatmap client-side: every cell's decile
    bucket (0..10 -> the .hm* classes) is precomputed here. matrix[a][b] = P(row a outranks col b);
    the diagonal is left blank and unresolved pairs (symmetric) are flagged.
    """
    unresolved = {tuple(p) for p in report.get("unresolved_pairs", [])}
    rows = []
    for a, (label, row) in enumerate(zip(report["labels"], report["matrix"])):
        cells = []
        for b, p in enumerate(row):
            diag = a == b
            flagged = (not diag) and ((a, b) in unresolved or (b, a) in unresolved)
            cells.append({
                "diag": diag,
                "unresolved": flagged,
                "pct": None if diag else int(round(p * 100)),
                "bucket": 0 if diag else int(round(p * 10)),
            })
        rows.append({"rank": a + 1, "label": label, "cells": cells})
    return rows


def leaderboard(request):
    event, err = _gate(request)
    if err:
        msg, status = err
        return HttpResponse(msg, status=status, content_type="text/plain")
    data = services.leaderboard(event)
    # Live credibility reads (permutation consensus + per-rank SE): a recompute over CURRENT ballots,
    # shown only on this organizer workbench and never presented as part of the signed/frozen result.
    # n_boot is trimmed here (the SEs stay honest) so the organizer page renders snappily.
    proof = services.proof_report(event, n_boot=400) if data.get("rows") else None
    pub = results_svc.current_publication(event)
    # services.leaderboard() self-seeds at 0 (its default); disclosed so the bootstrap columns
    # (rank interval + win prob) are reproducible. Display-only -- never part of a signed run.
    return render(request, "normalize/leaderboard.html",
                  {"event": event, "data": data, "proof": proof,
                   "bootstrap_seed": 0,
                   "live_published": pub is not None,
                   "live_status": pub.status if pub else "",
                   "live_version": pub.version if pub else 0})


def leaderboard_json(request):
    event, err = _gate(request)
    if err:
        msg, status = err
        return JsonResponse({"detail": msg}, status=status)
    return JsonResponse(services.leaderboard(event))


def pairwise(request):
    """Organizer-only pairwise-sensitivity workbench: the model-based probability that one top
    contender outranks another, from the SAME live parametric bootstrap as the rank intervals.

    Same 401/403/200 gate as leaderboard() (copied via _gate). It is a LIVE recompute over the
    current ballots and is NEVER part of a signed or published result. Computes pairwise_report only
    when the event has ballots (it self-short-circuits otherwise); an empty event renders a graceful
    empty state. `live_*` mirror leaderboard() so the shared banner names any already-published run
    truthfully rather than claiming nothing is published.
    """
    event, err = _gate(request)
    if err:
        msg, status = err
        return HttpResponse(msg, status=status, content_type="text/plain")
    report = services.pairwise_report(event)
    grid = _pairwise_grid(report) if report.get("labels") else None
    pub = results_svc.current_publication(event)
    return render(request, "normalize/pairwise.html",
                  {"event": event, "report": report, "grid": grid,
                   "live_published": pub is not None,
                   "live_status": pub.status if pub else "",
                   "live_version": pub.version if pub else 0})


def results(request):
    """PUBLIC official results (frozen, signed run) -- or a 'not published yet' page. Never gated."""
    event = services.current_event()
    if event is None:
        return HttpResponse("no event configured", status=404, content_type="text/plain")
    data = results_svc.current_results(event)
    return render(request, "normalize/results.html",
                  {"event": event, "data": data, "gov_state": _result_state(data),
                   "history": results_svc.publication_history(event)})


def results_json(request):
    """PUBLIC official results as JSON -- the exact frozen run `result` plus provenance."""
    event = services.current_event()
    if event is None:
        return JsonResponse({"detail": "no event configured"}, status=404)
    return JsonResponse(results_svc.current_results(event))


@require_http_methods(["GET", "POST"])
def results_publish(request):
    """Organizer-only publish console. GET shows current/history + a form; POST publishes a run.

    POST builds+signs a fresh run and appends the next-version publication atomically (run +
    `results.published` audit event + row + Event.results_published), then redirects (PRG). A 'final'
    or 'provisional' status is accepted; anything else is coerced to 'final'. No ballots -> 400.
    """
    event, err = _gate(request)
    if err:
        msg, status = err
        return HttpResponse(msg, status=status, content_type="text/plain")
    if request.method == "POST":
        status = request.POST.get("status", ResultPublication.FINAL)
        if status not in (ResultPublication.PROVISIONAL, ResultPublication.FINAL):
            status = ResultPublication.FINAL
        note = (request.POST.get("note", "") or "")[:200]
        key, _created = keys.ensure_private_key(None)
        try:
            results_svc.publish_results(event, key=key, note=note, status=status,
                                        actor_user_id=request.user.pk)
        except ValueError as exc:
            return HttpResponse(str(exc), status=400, content_type="text/plain")
        return redirect("normalize:results_publish")
    return render(request, "normalize/results_publish.html",
                  {"event": event, "current": results_svc.current_publication(event),
                   "data": results_svc.current_results(event),
                   "history": results_svc.publication_history(event)})


def diagnostics(request):
    """Organizer-only review-diagnostics panel (robustness + coverage, NOT fraud detection)."""
    event, err = _gate(request)
    if err:
        msg, status = err
        return HttpResponse(msg, status=status, content_type="text/plain")
    return render(request, "normalize/diagnostics.html",
                  {"event": event, "data": services.diagnostics_report(event),
                   "duplicate_clusters": services.duplicate_clusters(event)})


def diagnostics_json(request):
    event, err = _gate(request)
    if err:
        msg, status = err
        return JsonResponse({"detail": msg}, status=status)
    return JsonResponse(services.diagnostics_report(event))


def explain_rank(request, ext_id):
    """PARTICIPANT-facing "explain my rank" -- a read-only, plain-language interpretation of ONE
    project's row in the event's PUBLISHED, signed results.

    It never recomputes: it reads the frozen signed `result` (results_svc.current_results) and
    hands a single row to normalize.explain, so it cannot move a `result_hash`. Access is the
    owning team OR an organizer; authentication is required (browsers are sent to login) and every
    other case returns a uniform 404. Because the per-project figures are ALREADY public on
    /normalize/results(.json), this scoping is the "explain MY rank" frame plus defense-in-depth,
    not a secrecy boundary. The response is per-caller, so it is marked private/no-store/noindex.
    """
    event = services.current_event()
    if event is None:
        return HttpResponse("no event configured", status=404, content_type="text/plain")
    if not (request.user and request.user.is_authenticated):
        return redirect_to_login(request.get_full_path())

    is_org = events_services.is_organizer(request.user, event)
    owned = set(submissions_services.owned_submissions(request.user, event)
                .values_list("ext_id", flat=True))
    if not (is_org or ext_id in owned):
        return HttpResponse("no such project", status=404, content_type="text/plain")

    data = results_svc.current_results(event)
    if not data.get("published"):
        return HttpResponse("results not published", status=404, content_type="text/plain")
    result = data.get("result") or {}
    row, next_title = explain_mod.find_row(result, ext_id)
    if row is None:
        return HttpResponse("no such project", status=404, content_type="text/plain")

    explanation = explain_mod.explain_row(
        row, field_size=len(result.get("rows") or []),
        n_boot=result.get("n_boot", 0), seed=data.get("seed", 0),
        n_components=result.get("n_components", 1), next_title=next_title)
    resp = render(request, "normalize/explain.html", {
        "event": event, "data": data, "row": row, "explain": explanation,
        "organizer_view": is_org and ext_id not in owned})
    resp["Cache-Control"] = "private, no-store"
    resp["Vary"] = "Cookie"
    resp["X-Robots-Tag"] = "noindex, nofollow"
    resp["Referrer-Policy"] = "same-origin"
    return resp
