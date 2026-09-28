# src/api/serializers.py
"""Read-only serializers for the public API (/api/v1/).

Every serializer lists its fields EXPLICITLY -- never `fields = "__all__"` -- so a column added
to a model can never silently widen the public surface. Nothing here exposes a user
(email/display_name), a ballot or per-judge score, a judge identity, an invite, or the audit
chain. Related objects are rendered by their public string `ext_id` (or public name), never their
internal primary key. The API is read-only by construction: the views are GET-only (ListAPIView/
RetrieveAPIView/APIView.get), so these serializers are only ever used to serialize OUT.
"""
from rest_framework import serializers

from events.models import Event, Team, Track
from submissions.models import Submission


class EventSerializer(serializers.ModelSerializer):
    class Meta:
        model = Event
        # Public metadata only. Memberships (which carry users/PII) are a reverse relation and
        # are deliberately NOT listed, so they can never be serialized.
        fields = ("ext_id", "name", "state", "submissions_close",
                  "results_published", "created_at")


class TrackSerializer(serializers.ModelSerializer):
    event = serializers.SlugRelatedField(slug_field="ext_id", read_only=True)

    class Meta:
        model = Track
        fields = ("ext_id", "name", "event")


class TeamSerializer(serializers.ModelSerializer):
    event = serializers.SlugRelatedField(slug_field="ext_id", read_only=True)

    class Meta:
        model = Team
        # Team NAME only. Members (TeamMember -> AppUser) are a reverse relation and are NOT
        # listed, so no participant email or display_name can leak through a team.
        fields = ("ext_id", "name", "event")


class SubmissionSerializer(serializers.ModelSerializer):
    event = serializers.SlugRelatedField(slug_field="ext_id", read_only=True)
    track = serializers.SlugRelatedField(slug_field="ext_id", read_only=True)
    track_name = serializers.CharField(source="track.name", read_only=True)
    team = serializers.SlugRelatedField(slug_field="ext_id", read_only=True)
    team_name = serializers.CharField(source="team.name", read_only=True)

    class Meta:
        model = Submission
        # PUBLIC subset. The view filters the queryset to state=SUBMITTED, so DRAFT and
        # WITHDRAWN rows never reach this serializer. No internal pk; no scores of any kind
        # (those live on Ballot, an unrelated model that is never serialized anywhere here).
        fields = ("ext_id", "title", "summary", "repo_url", "state",
                  "submitted_at", "created_at",
                  "event", "track", "track_name", "team", "team_name")


# --- Verifiable results certificate (T4) ---------------------------------------------------------
# Output-only, EXPLICIT-field serializers for the results certificate served at
# /api/v1/events/<ext_id>/certificate/. Like the ModelSerializers above they are an allowlist --
# they can only ever emit the fields declared here, so serializing the certificate through them is a
# second, structural guarantee that no PII escapes (the certificate dict built by
# normalize.certificate already excludes it). These are plain Serializers, not ModelSerializers:
# the certificate is assembled from a signed, published NormalizationRun, not a single model row.
# Nested dicts each get a tiny serializer so drf-spectacular emits a fully typed schema.

class CertificateEventSerializer(serializers.Serializer):
    ext_id = serializers.CharField()
    name = serializers.CharField(allow_blank=True)


class CertificatePublicationSerializer(serializers.Serializer):
    version = serializers.IntegerField()
    status = serializers.CharField()                       # 'provisional' | 'final'
    published_at = serializers.CharField(allow_blank=True)


class CertificateSignatureSerializer(serializers.Serializer):
    algorithm = serializers.CharField()                    # 'ed25519'
    value = serializers.CharField()                        # hex signature
    public_key_pem = serializers.CharField(allow_null=True, required=False)


class CertificateSignedMaterialSerializer(serializers.Serializer):
    # EXACTLY the fields the Ed25519 run signature covers (normalize.signing.RUN_FIELDS). No PII.
    engine_version = serializers.CharField()
    instance_id = serializers.CharField()
    event_ext_id = serializers.CharField()
    run_ext_id = serializers.CharField()
    inputs_hash = serializers.CharField()
    result_hash = serializers.CharField()
    created_at = serializers.CharField()


class CertificateRunProvenanceSerializer(serializers.Serializer):
    audit_seq = serializers.IntegerField(allow_null=True)
    lambda_value = serializers.FloatField(allow_null=True)
    n_boot = serializers.IntegerField(allow_null=True)
    seed = serializers.IntegerField(allow_null=True)


class CertificateRankingRowSerializer(serializers.Serializer):
    # The public ranking columns only (same as ranking.csv): rank, submission ext_id, title,
    # normalized q. No per-judge or per-criterion scores, no ballots.
    rank = serializers.IntegerField()
    submission = serializers.CharField()
    title = serializers.CharField(allow_blank=True)
    q = serializers.FloatField()


class CertificateVerificationSerializer(serializers.Serializer):
    verifier_command = serializers.CharField()
    run_verifier_command = serializers.CharField()
    steps = serializers.ListField(child=serializers.CharField())
    scope = serializers.CharField()


class ResultsCertificateSerializer(serializers.Serializer):
    """The full certificate envelope. Read-only; every field is non-PII and copied verbatim from a
    signed, published normalization run (see normalize.certificate)."""
    kind = serializers.CharField()
    attestation = serializers.CharField()
    event = CertificateEventSerializer()
    publication = CertificatePublicationSerializer()
    engine_version = serializers.CharField()
    result_hash = serializers.CharField()
    signer_fingerprint = serializers.CharField()
    signature = CertificateSignatureSerializer()
    signed_material = CertificateSignedMaterialSerializer()
    run_provenance = CertificateRunProvenanceSerializer()
    ranking = CertificateRankingRowSerializer(many=True)
    verification = CertificateVerificationSerializer()
