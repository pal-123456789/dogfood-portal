# src/normalize/views.py
"""Normalization routes: an organizer-only WORKBENCH and a public RESULTS surface.

Two audiences, one clean split (docs/THREAT-MODEL.md W1):
  * Organizer-only (gated 401/403 like the CSV export): `leaderboard` / `leaderboard_json` show the
    live, recomputed normalized ranking -- results-in-waiting -- and `results_publish` is the publish
    console. Severity-adjustment can reshuffle the podium, so these are never public.
  * Public: `results` / `results_json` serve ONLY what an organizer has explicitly published, and
    they serve the signed run's FROZEN `result` verbatim (never a recompute), so what the world sees
    canonicalizes to the signed `result_hash` and verifies offline. Before the first publish they say
    so with HTTP 200 -- an event's rankings are simply not public until the organizer publishes them.

Every route here lives under /normalize/ and is NOT one of the five the acceptance checker hits, so
nothing in this module can move replay off 7/7.
"""
from django.http import HttpResponse, JsonResponse
from django.shortcuts import redirect, render
from django.views.decorators.http import require_http_methods

from audit import keys
from events.models import EventMembership

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


def leaderboard(request):
    event, err = _gate(request)
    if err:
        msg, status = err
        return HttpResponse(msg, status=status, content_type="text/plain")
    return render(request, "normalize/leaderboard.html",
                  {"event": event, "data": services.leaderboard(event)})


def leaderboard_json(request):
    event, err = _gate(request)
    if err:
        msg, status = err
        return JsonResponse({"detail": msg}, status=status)
    return JsonResponse(services.leaderboard(event))


def results(request):
    """PUBLIC official results (frozen, signed run) -- or a 'not published yet' page. Never gated."""
    event = services.current_event()
    if event is None:
        return HttpResponse("no event configured", status=404, content_type="text/plain")
    data = results_svc.current_results(event)
    return render(request, "normalize/results.html",
                  {"event": event, "data": data,
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
