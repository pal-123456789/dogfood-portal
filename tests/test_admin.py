# tests/test_admin.py
"""DB-free checks that the admin site is wired the way the integrity design requires.

No ``django_db`` mark: these tests only import the admin modules (executing their
``@admin.register`` decorators), inspect ``admin.site._registry``, and call the overridden
permission methods on the read-only admins (which ignore the request). Nothing touches the
database, so pytest-django never builds a test DB for this module.

Django itself is configured for pytest via the container env (``DJANGO_SETTINGS_MODULE=
portal.settings`` + ``PYTHONPATH=/app/src``), the same way the other files in ``tests/`` import
app models.
"""
from django.contrib import admin

# importing each app's admin module executes its @admin.register(...) decorators, so registration
# is deterministic here regardless of admin autodiscovery order.
import accounts.admin      # noqa: F401
import audit.admin         # noqa: F401
import events.admin        # noqa: F401
import judging.admin       # noqa: F401
import normalize.admin     # noqa: F401
import submissions.admin   # noqa: F401

from accounts.models import AppUser, DemoSession
from audit.models import AuditEvent, AuditHead
from events.models import (
    BootstrapState,
    Event,
    EventMembership,
    Team,
    TeamMember,
    Track,
)
from judging.models import Ballot, BallotRevision, JudgeAssignment, RubricWeight
from normalize.models import NormalizationRun, ResultPublication
from submissions.models import Submission

from portal.admin_mixins import ReadOnlyModelAdmin

# every domain model should be manageable/inspectable in the admin
REGISTERED = [
    AppUser, DemoSession,
    AuditHead, AuditEvent,
    Event, EventMembership, Track, Team, TeamMember, BootstrapState,
    Submission,
    JudgeAssignment, Ballot, BallotRevision, RubricWeight,
    NormalizationRun, ResultPublication,
]

# append-only / signed integrity tables -- must be inspect-only in the admin
READ_ONLY = [AuditHead, AuditEvent, Ballot, BallotRevision, NormalizationRun, ResultPublication]

# configuration/management tables an organizer edits -- must NOT be read-only
EDITABLE = [
    AppUser, DemoSession, Event, EventMembership, Track, Team, TeamMember,
    BootstrapState, Submission, JudgeAssignment, RubricWeight,
]


def test_all_domain_models_registered():
    for model in REGISTERED:
        assert admin.site.is_registered(model), (
            "%s is not registered in the admin" % model.__name__
        )


def test_integrity_tables_are_read_only():
    for model in READ_ONLY:
        model_admin = admin.site._registry[model]
        assert isinstance(model_admin, ReadOnlyModelAdmin), (
            "%s admin must subclass ReadOnlyModelAdmin" % model.__name__
        )
        # writes are denied unconditionally (methods ignore the request -> None is safe)
        assert model_admin.has_add_permission(None) is False
        assert model_admin.has_change_permission(None) is False
        assert model_admin.has_change_permission(None, object()) is False
        assert model_admin.has_delete_permission(None) is False
        assert model_admin.has_delete_permission(None, object()) is False


def test_management_tables_are_not_read_only():
    for model in EDITABLE:
        model_admin = admin.site._registry[model]
        assert not isinstance(model_admin, ReadOnlyModelAdmin), (
            "%s should stay editable in the admin" % model.__name__
        )


def test_appuser_admin_never_exposes_the_password_hash():
    # the password hash must be unreachable as a form field: absent from every fieldset.
    model_admin = admin.site._registry[AppUser]
    fields = []
    for _label, opts in model_admin.fieldsets:
        fields.extend(opts.get("fields", ()))
    assert "password" not in fields
