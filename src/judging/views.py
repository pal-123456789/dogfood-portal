# src/judging/views.py
"""T2 read surface: the judge scores API and the organizer CSV export.

The crux (checks 4/5/6): /api/judge/scores returns the CALLER'S own ballots. A `judge=`
query param naming anyone but the caller is 403 (ownership is the membership, not the URL);
a non-judge caller is 403; an unauthenticated caller is 401. check 7: only an organizer may
export, and the CSV's header line carries commas.
"""
import csv

from django.http import HttpResponse, JsonResponse
from django.views.decorators.http import require_GET

from events.models import Event, EventMembership

from . import services


def _current_event():
    return Event.objects.order_by("id").first()


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
    return JsonResponse({
        "judge": membership.ext_id,
        "count": len(scores),
        "scores": scores,
    })


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
    writer = csv.writer(response)
    for row in services.export_event_rows(event):
        writer.writerow(row)
    return response                                                                # check 7
