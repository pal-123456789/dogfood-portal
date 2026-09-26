# tests/test_smoke.py
"""F1 smoke. Every assertion here is about a decision that is expensive to undo.

No database: nothing in this file is marked django_db, so pytest never creates a test
database and CI's 15-minute budget is not spent on one. The three properties asserted
are exactly the three that F1 can get wrong in a way that looks fine.
"""
from django.contrib.auth import get_user_model


def test_healthz_returns_the_body_the_healthcheck_reads(client):
    # Compose's healthcheck compares the BODY, not the status (section 13), so the test
    # asserts the same thing it does. A 200 carrying a login page would satisfy a
    # status-only check and a status-only test alike.
    response = client.get("/healthz")
    assert response.status_code == 200
    assert response.content == b"ok"


def test_custom_middleware_is_actually_wired(client):
    # django.setup() does NOT load MIDDLEWARE, so a missing or unlisted middleware.py
    # builds clean and is only visible in a served response (section 0, row 6).
    response = client.get("/healthz")
    assert response["Content-Security-Policy"] == "script-src 'self'"


def test_user_model_identity_is_the_one_migrations_froze():
    # The three facts about AUTH_USER_MODEL that a migration cannot undo (section 18).
    user_model = get_user_model()
    assert user_model._meta.db_table == "app_user"
    assert user_model.USERNAME_FIELD == "email"
    assert user_model.REQUIRED_FIELDS == []
