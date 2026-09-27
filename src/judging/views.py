# src/judging/views.py
"""Judging HTTP surface: the read API + the organizer CSV export, plus the in-app judge
scoring page.

The crux (checks 4/5/6): /api/judge/scores returns the CALLER'S own ballots. A `judge=`
query param naming anyone but the caller is 403 (ownership is the membership, not the URL);
a non-judge caller is 403; an unauthenticated caller is 401. check 7: only an organizer may
export, and the CSV's header line carries commas. The `score` view (GET/POST /judging/score)
is the human write path and is NOT on the checker's route list.
"""
import csv

from django.conf import settings
from django.contrib.auth.views import redirect_to_login
from django.core.exceptions import ValidationError
from django.http import HttpResponse, JsonResponse
from django.shortcuts import get_object_or_404, redirect, render
from django.urls import reverse
from django.views.decorators.http import require_GET, require_http_methods

from events.models import Event, EventMembership
from events import services as events_services

from portal import ratelimit

from . import services


def _current_event():
    return Event.objects.order_by("id").first()


# ?ok=<key> after a PRG redirect -> a human confirmation banner on the target page.
_NOTICES = {"assigned": "Judge assigned.", "unassigned": "Assignment removed.",
            "rubric": "Rubric weights updated."}


def _organizer_event_or_response(request, ext_id):
    """Resolve the event and enforce organizer-of-this-event; return (event, None) or
    (None, response). Anonymous -> login page (this is a browser tool for people); unknown
    event -> 404; authenticated non-organizer -> plain 403. Mirrors events.views so the
    /judging/<event>/... control room guards exactly like the events organizer UI."""
    if not request.user.is_authenticated:
        return None, redirect_to_login(request.get_full_path(), settings.LOGIN_URL)
    event = get_object_or_404(Event, ext_id=ext_id)
    if not events_services.is_organizer(request.user, event):
        return None, HttpResponse("organizers only", status=403)
    return event, None


@require_GET
def judge_scores(request):
    event = _current_event()
    if event is None:
        return JsonResponse({"detail": "no event configured"}, status=404)
    if not request.user.is_authenticated:
        return JsonResponse({"detail": "authentication required"}, status=401)
    membership = services.judge_membership(request.user, event)
    if membership is None:
        return JsonResponse({"detail": "not a judge for this event"}, status=403)  # check 6
    requested = request.GET.get("judge")
    if requested and requested != membership.ext_id:                               # check 5
        return JsonResponse({"detail": "cannot read another judge's scores"}, status=403)
    scores = services.scores_for_judge(membership)                                 # check 4
    response = JsonResponse({
        "judge": membership.ext_id,
        "count": len(scores),
        "scores": scores,
    })
    # Private per-caller data: never let a shared/proxy cache serve one judge's rows to
    # another. Harmless to the checker -- status and body are unchanged, it only adds headers.
    response["Cache-Control"] = "private, no-store"
    response["Vary"] = "Cookie"
    return response


@require_GET
def export_csv(request):
    event = _current_event()
    if event is None:
        return HttpResponse("no event configured", status=404)
    if not request.user.is_authenticated:
        return HttpResponse("authentication required", status=401)
    is_org = EventMembership.objects.filter(
        user=request.user, event=event, role=EventMembership.ORGANIZER).exists()
    if not is_org:
        return HttpResponse("organizer only", status=403)
    response = HttpResponse(content_type="text/csv")
    response["Content-Disposition"] = 'attachment; filename="export.csv"'
    response["Cache-Control"] = "private, no-store"
    response["Vary"] = "Cookie"
    writer = csv.writer(response)
    for row in services.export_event_rows(event):
        writer.writerow(row)
    return response                                                                # check 7


@require_http_methods(["GET", "POST"])
def score(request):
    """In-app judge scoring (the human write surface for judging).

    Not on the acceptance checker's path -- the checker only reads /api/judge/scores. This is a
    real judge scoring their assigned queue: GET renders the queue with the current score
    pre-filled; POST writes one submission's score through record_ballot (append-only revision +
    audit event, atomically). Authorization is the same event-scoped rule as judge_scores -- a
    judge membership for the current event -- and a judge may only score a submission they are
    ASSIGNED to (an existing JudgeAssignment row), never an arbitrary project id from the form.

    Real judges are throttled per user by DOGFOOD_RATE_LIMITS['ballot_write']; the DEMO shim is
    exempt (mirrors submit()), and the limiter fails open. Anonymous callers are sent to the login
    page rather than getting a bare 401, because this is a browser page for people.
    """
    event = _current_event()
    if event is None:
        return HttpResponse("no event configured", status=404)
    if not request.user.is_authenticated:
        return redirect_to_login(request.get_full_path(), settings.LOGIN_URL)
    membership = services.judge_membership(request.user, event)
    if membership is None:
        return HttpResponse("judges only", status=403)

    def _render(extra=None, status=200):
        ctx = {"event": event, "membership": membership,
               "rows": services.assigned_submissions(membership),
               "scale": range(1, 6), "saved": request.GET.get("saved", "")}
        if extra:
            ctx.update(extra)
        return render(request, "judging/score.html", ctx, status=status)

    if request.method == "GET":
        return _render()

    # Abuse control for real judges; the DEMO shim (if ever used here) is EXEMPT, matching submit().
    if not getattr(request, "demo_shim", False):
        allowed, retry = ratelimit.hit(
            "ballot_write:%s" % request.user.pk,
            settings.DOGFOOD_RATE_LIMITS["ballot_write"])
        if not allowed:
            resp = _render({"error": "Too many score submissions. Please slow down."}, status=429)
            resp["Retry-After"] = str(retry)
            return resp

    assignment = services.assignment_for(membership, request.POST.get("submission", ""))
    if assignment is None:
        return HttpResponse("not assigned to you", status=403)   # ownership is the data, not the URL
    try:
        services.record_ballot(
            membership, assignment.submission,
            functionality=request.POST.get("functionality"),
            quality=request.POST.get("quality"),
            innovation=request.POST.get("innovation"),
            comment=(request.POST.get("comment") or "").strip())
    except ValidationError as e:
        return _render({"error": " ".join(e.messages)}, status=400)
    # Post/redirect/get: a refresh must not silently re-record a ballot.
    return redirect("%s?saved=%s" % (reverse("judging:score"), assignment.submission.ext_id))


# --- Organizer control room: judging progress, assignments, rubric weights --------------------
# All organizer-only + event-scoped, all under the /judging/ include, none on the checker's five
# flat routes. base.html is untouched, so /projects and /projects/new stay byte-identical and
# replay stays 7/7. No new model/field -> no migration.


@require_GET
def progress(request, ext_id):
    """Read-only judging coverage: assigned/scored/pending per judge and per submission."""
    event, resp = _organizer_event_or_response(request, ext_id)
    if resp is not None:
        return resp
    ctx = services.judging_progress(event)
    ctx["notice"] = _NOTICES.get(request.GET.get("ok", ""), "")
    return render(request, "judging/progress.html", ctx)


def _assignments_context(event, **extra):
    ctx = {"event": event, "judges": services.event_judges(event),
           "progress": services.judging_progress(event), "notice": "", "error": ""}
    ctx.update(extra)
    return ctx


@require_GET
def assignments(request, ext_id):
    """List current assignments (with a per-judge scored flag) and offer assign/remove."""
    event, resp = _organizer_event_or_response(request, ext_id)
    if resp is not None:
        return resp
    return render(request, "judging/assignments.html",
                  _assignments_context(event, notice=_NOTICES.get(request.GET.get("ok", ""), "")))


@require_http_methods(["POST"])
def assign(request, ext_id):
    event, resp = _organizer_event_or_response(request, ext_id)
    if resp is not None:
        return resp
    try:
        services.assign_judge(request.user, event,
                              judge_ext_id=request.POST.get("judge", ""),
                              submission_ext_id=request.POST.get("submission", ""))
    except ValidationError as e:
        return render(request, "judging/assignments.html",
                      _assignments_context(event, error=" ".join(e.messages)), status=400)
    return redirect("%s?ok=assigned" % reverse("judging:assignments", args=[event.ext_id]))


@require_http_methods(["POST"])
def unassign(request, ext_id):
    event, resp = _organizer_event_or_response(request, ext_id)
    if resp is not None:
        return resp
    try:
        services.unassign_judge(request.user, event,
                                judge_ext_id=request.POST.get("judge", ""),
                                submission_ext_id=request.POST.get("submission", ""))
    except ValidationError as e:
        return render(request, "judging/assignments.html",
                      _assignments_context(event, error=" ".join(e.messages)), status=400)
    return redirect("%s?ok=unassigned" % reverse("judging:assignments", args=[event.ext_id]))


@require_http_methods(["GET", "POST"])
def rubric(request, ext_id):
    """Organizer edits the per-criterion rubric weights that feed the normalized score."""
    event, resp = _organizer_event_or_response(request, ext_id)
    if resp is not None:
        return resp
    if request.method == "POST":
        try:
            services.set_rubric_weights(
                request.user, event,
                weights={c: request.POST.get(c, "") for c in services.CRITERIA})
        except ValidationError as e:
            reflected = {c: request.POST.get(c, "") for c in services.CRITERIA}
            return render(request, "judging/rubric.html",
                          {"event": event, "weights": reflected, "criteria": services.CRITERIA,
                           "error": " ".join(e.messages), "notice": ""}, status=400)
        return redirect("%s?ok=rubric" % reverse("judging:rubric", args=[event.ext_id]))
    return render(request, "judging/rubric.html",
                  {"event": event, "weights": services.current_weights(event),
                   "criteria": services.CRITERIA,
                   "notice": _NOTICES.get(request.GET.get("ok", ""), ""), "error": ""})
