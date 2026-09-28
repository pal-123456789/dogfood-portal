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
