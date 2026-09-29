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
from django.utils.cache import patch_vary_headers
from drf_spectacular.types import OpenApiTypes
from drf_spectacular.utils import OpenApiExample, extend_schema
from drf_spectacular.views import SpectacularSwaggerView
from rest_framework import generics
from rest_framework.exceptions import NotFound
from rest_framework.permissions import IsAuthenticated
from rest_framework.response import Response
from rest_framework.views import APIView

from apitokens.authentication import BearerTokenAuthentication
from events.models import Event, EventMembership, Team, Track
from normalize import certificate, results as results_service
from submissions.models import Submission

from .serializers import (EventSerializer, ResultsCertificateSerializer, SubmissionSerializer,
                          TeamSerializer, TrackSerializer)


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


_CERTIFICATE_EXAMPLE = OpenApiExample(
    "Certificate for a published run", response_only=True,
    value={
        "kind": "dogfood.results-certificate.v1",
        "attestation": ("Attests that the operator's Ed25519 key signed the published run below; "
                        "not a measure of merit and not fraud detection."),
        "event": {"ext_id": "evt_01", "name": "Spring Hackathon"},
        "publication": {"version": 1, "status": "final",
                        "published_at": "2026-09-28T12:00:00+00:00"},
        "engine_version": "ridge-additive-v1",
        "result_hash": "<hex>", "signer_fingerprint": "<hex>",
        "signature": {"algorithm": "ed25519", "value": "<hex>", "public_key_pem": "<pem>"},
        "signed_material": {"engine_version": "ridge-additive-v1", "instance_id": "<id>",
                            "event_ext_id": "evt_01", "run_ext_id": "run_ab12cd34",
                            "inputs_hash": "<hex>", "result_hash": "<hex>",
                            "created_at": "2026-09-28T11:59:00+00:00"},
        "run_provenance": {"audit_seq": 42, "lambda_value": 1.0, "n_boot": 1000, "seed": 0},
        "ranking": [{"rank": 1, "submission": "prj_a", "title": "Title prj_a", "q": 4.0}],
        "verification": {"verifier_command": "python -m normalize.release <bundle_dir>",
                         "run_verifier_command": "python -m normalize.verify <bundle_dir>",
                         "steps": ["This certificate copies its result_hash, signature, fingerprint "
                                   "and ranking verbatim from a signed, frozen run."],
                         "scope": ("signed, published-run attestation -- not merit, not fraud "
                                   "detection")}})


@extend_schema(tags=["results"], responses=ResultsCertificateSerializer,
               examples=[_CERTIFICATE_EXAMPLE])
class EventCertificateView(APIView):
    """Verifiable results certificate for one event's official PUBLISHED results -- a self-contained
    attestation over the FROZEN, signed normalization run (never a live recompute). Returns 404 when
    the event has no published results. Carries only published-safe values: the run's result_hash,
    signer fingerprint, public key, signature and the public ranking (rank/submission/title/q) --
    no per-judge scores, no ballots, no judge identities, no user PII."""

    def get(self, request, ext_id):
        cert = certificate.certificate_for_event(_event_or_404(ext_id))
        if cert is None:
            raise NotFound("no published results to certify for this event")
        return Response(ResultsCertificateSerializer(cert).data)


_ME_EXAMPLE = OpenApiExample(
    "Authenticated caller (their own identity only)", response_only=True,
    value={"authenticated": True,
           "token": {"name": "CI read-only", "prefix": "dgf_AbC123xyz",
                     "last_used_at": "2026-09-28T12:00:00+00:00"},
           "memberships": [{"event": "evt_01", "role": "judge", "ext_id": "jdg_01"}]})


@extend_schema(tags=["me"], responses=OpenApiTypes.OBJECT, examples=[_ME_EXAMPLE])
class MeView(APIView):
    """The authenticated caller's OWN identity, for programmatic clients. This is the ONLY endpoint
    that requires authentication: send `Authorization: Bearer <token>` (mint one with
    `manage.py mint_api_token`). It returns only the calling token's metadata and the caller's own
    event memberships (operational ext_ids and roles) -- never an email, a display name, another
    user's data, a ballot, or a per-judge score. Setting the auth/permission classes HERE (not
    globally) keeps every other /api/v1/ endpoint public, authless, and byte-identical."""

    authentication_classes = [BearerTokenAuthentication]
    permission_classes = [IsAuthenticated]

    def get(self, request):
        token = request.auth  # the ApiToken this request authenticated with (set by the auth class)
        memberships = (EventMembership.objects.filter(user=request.user)
                       .select_related("event").order_by("event__ext_id", "role"))
        body = {
            "authenticated": True,
            "token": {"name": token.name, "prefix": token.prefix,
                      "last_used_at": token.last_used_at},
            "memberships": [{"event": m.event.ext_id, "role": m.role, "ext_id": m.ext_id}
                            for m in memberships],
        }
        resp = Response(body)
        # Per-caller data: never store it in a shared cache, and vary on the credential.
        resp["Cache-Control"] = "private, no-store"
        patch_vary_headers(resp, ("Authorization", "Cookie"))
        return resp


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
