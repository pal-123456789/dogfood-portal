# src/api/views.py
"""Read-only public API views (/api/v1/). Everything here is AllowAny and GET-only.

Scoping is STRUCTURAL: tracks, teams, submissions and results all live under
/events/{ext_id}/, and each queryset is filtered to that event, so a response for event A can
never contain event B's rows. Submissions are additionally filtered to state=SUBMITTED, so
drafts and withdrawn projects never appear. Rankings come only from
normalize.results.current_results -- the FROZEN, signed run -- never a live recompute; an
unpublished event returns {"published": false} with no ranking rows.
"""
from django.shortcuts import get_object_or_404
from drf_spectacular.types import OpenApiTypes
from drf_spectacular.utils import OpenApiExample, extend_schema
from drf_spectacular.views import SpectacularSwaggerView
from rest_framework import generics
from rest_framework.response import Response
from rest_framework.views import APIView

from events.models import Event, Team, Track
from normalize import results as results_service
from submissions.models import Submission

from .serializers import (EventSerializer, SubmissionSerializer, TeamSerializer,
                          TrackSerializer)


def _event_or_404(ext_id):
    return get_object_or_404(Event, ext_id=ext_id)


@extend_schema(tags=["events"])
class EventListView(generics.ListAPIView):
    """List every event (public metadata only -- no memberships, no PII)."""
    serializer_class = EventSerializer
    queryset = Event.objects.all().order_by("id")


@extend_schema(tags=["events"])
class EventDetailView(generics.RetrieveAPIView):
    """One event by its public ext_id."""
    serializer_class = EventSerializer
    queryset = Event.objects.all()
    lookup_field = "ext_id"


@extend_schema(tags=["events"])
class EventTracksView(generics.ListAPIView):
    """Tracks belonging to one event."""
    serializer_class = TrackSerializer

    def get_queryset(self):
        return Track.objects.filter(event=_event_or_404(self.kwargs["ext_id"])).order_by("ext_id")


@extend_schema(tags=["events"])
class EventTeamsView(generics.ListAPIView):
    """Teams belonging to one event (team name only -- never members or their emails)."""
    serializer_class = TeamSerializer

    def get_queryset(self):
        return Team.objects.filter(event=_event_or_404(self.kwargs["ext_id"])).order_by("ext_id")


@extend_schema(tags=["submissions"])
class EventSubmissionsView(generics.ListAPIView):
    """Public submissions for one event: SUBMITTED only (drafts and withdrawn are excluded)."""
    serializer_class = SubmissionSerializer

    def get_queryset(self):
        event = _event_or_404(self.kwargs["ext_id"])
        return (Submission.objects
                .filter(event=event, state=Submission.SUBMITTED)
                .select_related("event", "track", "team")
                .order_by("id"))


_RESULTS_EXAMPLES = [
    OpenApiExample(
        "Published (frozen, signed run)", response_only=True,
        value={"published": True, "event": "evt_01", "version": 1, "status": "final",
               "note": "Final results.", "run_ext_id": "run_ab12cd34",
               "engine_version": "normalize-1", "result_hash": "<hex>", "inputs_hash": "<hex>",
               "fingerprint": "<hex>", "lambda": 1.0, "audit_seq": 42,
               "published_at": "2026-09-28T12:00:00+00:00",
               "result": {"rows": [{"rank": 1, "submission": "prj_a", "title": "Title prj_a",
                                    "track": "trk_01", "q": 4.0, "raw_mean": 4.0, "delta": 0.0,
                                    "rank_lo": 1, "rank_median": 1, "rank_hi": 1, "n_ballots": 3,
                                    "tied_with_next": False, "component": 0}],
                          "n_ballots": 9, "n_submissions": 3, "n_judges": 3, "lambda": 1.0,
                          "sigma": 0.0, "n_components": 1, "gauge_error": 0.0, "n_boot": 1000,
                          "unresolved_count": 0}}),
    OpenApiExample("Not yet published", value={"published": False}, response_only=True),
]


@extend_schema(tags=["results"], responses=OpenApiTypes.OBJECT, examples=_RESULTS_EXAMPLES)
class EventResultsView(APIView):
    """Official published results for one event -- the FROZEN ranking read verbatim from the
    signed normalization run (never a live recompute). Returns {"published": false} with no
    ranking rows if results are not published. Contains only aggregate, published-safe values:
    no per-judge scores, no ballots, no judge identities."""

    def get(self, request, ext_id):
        return Response(results_service.current_results(_event_or_404(ext_id)))


class CspSwaggerView(SpectacularSwaggerView):
    """Swagger UI served from self-hosted assets (drf-spectacular-sidecar), with a CSP scoped to
    THIS page that adds 'unsafe-inline' for the small bootstrap <script> the template emits. The
    site-wide policy stays `script-src 'self'`; ContentSecurityPolicyMiddleware uses
    response.setdefault, so the header set here wins for the docs page only. No user data and no
    state-changing form is served here, so the relaxation is bounded to a documentation page."""

    @extend_schema(exclude=True)  # HTML docs UI, not a data endpoint -> keep it out of the OpenAPI schema
    def get(self, request, *args, **kwargs):
        response = super().get(request, *args, **kwargs)
        response["Content-Security-Policy"] = "script-src 'self' 'unsafe-inline'"
        return response
