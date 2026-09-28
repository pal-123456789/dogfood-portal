# tests/test_api_contract.py
"""DB-free contract tests for the public API serializers (run by `pytest tests/`).

They guard the two properties that keep the public API safe without needing a database:
  1. every serializer declares its fields EXPLICITLY (a tuple/list), never `fields = "__all__"`,
     so adding a model column can never silently widen the public surface;
  2. no serializer's field allowlist contains a sensitive name (user PII, per-judge scores,
     ballots, or judge identity).

Django is configured by the container env (DJANGO_SETTINGS_MODULE=portal.settings, PYTHONPATH=
/app/src); pytest-django runs django.setup() before collection, so importing the serializer
module at top level is safe and touches no database.
"""
from rest_framework import serializers as drf_serializers

from api import serializers as api_serializers

_SERIALIZERS = [
    api_serializers.EventSerializer,
    api_serializers.TrackSerializer,
    api_serializers.TeamSerializer,
    api_serializers.SubmissionSerializer,
]

# Substrings that must never appear in ANY public serializer field name.
_FORBIDDEN = ("email", "password", "display_name", "ballot",
              "functionality", "quality", "innovation", "judge")


def _declared_fields(cls):
    fields = cls.Meta.fields
    assert isinstance(fields, (tuple, list)), (
        "%s.Meta.fields must be an explicit tuple/list, got %r" % (cls.__name__, type(fields)))
    assert fields != "__all__", "%s uses fields='__all__'" % cls.__name__
    return tuple(fields)


def test_every_serializer_declares_explicit_fields():
    for cls in _SERIALIZERS:
        assert issubclass(cls, drf_serializers.ModelSerializer)
        _declared_fields(cls)


def test_no_serializer_uses_all_or_exclude():
    for cls in _SERIALIZERS:
        assert getattr(cls.Meta, "fields", None) != "__all__"
        # `exclude` is the other way to smuggle in every column; it must not be used either.
        assert not hasattr(cls.Meta, "exclude"), "%s uses Meta.exclude" % cls.__name__


def test_no_sensitive_field_names_in_allowlists():
    for cls in _SERIALIZERS:
        for name in _declared_fields(cls):
            low = name.lower()
            for bad in _FORBIDDEN:
                assert bad not in low, "%s exposes sensitive field %r" % (cls.__name__, name)


def test_submission_allowlist_is_exactly_the_public_subset():
    assert set(_declared_fields(api_serializers.SubmissionSerializer)) == {
        "ext_id", "title", "summary", "repo_url", "state", "submitted_at", "created_at",
        "event", "track", "track_name", "team", "team_name",
    }


def test_event_allowlist_has_no_membership_or_pii():
    assert set(_declared_fields(api_serializers.EventSerializer)) == {
        "ext_id", "name", "state", "submissions_close", "results_published", "created_at",
    }
