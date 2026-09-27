# src/accounts/tests.py
"""DB-backed tests for the human login flow and its per-IP throttle.

The throttle counts through the cache, but the `manage.py test` database has no cache table
(it is created by `createcachetable` in the entrypoint, not by a migration), so every test here
overrides CACHES to an in-memory backend and clears it in setUp. That is also why the limiter is
built to fail OPEN -- in an environment with no cache table it must allow, not deny.
"""
from django.core.cache import caches
from django.test import TestCase, override_settings

from accounts.models import AppUser

_LOCMEM = {"default": {"BACKEND": "django.core.cache.backends.locmem.LocMemCache",
                       "LOCATION": "accounts-tests"}}
_RATES = {"login": "3/m", "invite_redeem": "20/h",
          "submission_write": "60/h", "ballot_write": "120/h"}


@override_settings(CACHES=_LOCMEM, DOGFOOD_RATE_LIMITS=_RATES)
class LoginThrottleTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.user = AppUser.objects.create_user(
            email="judge@t.demo", display_name="J", password="correct-horse-battery")

    def setUp(self):
        caches["default"].clear()          # each test starts with an empty window counter

    def test_get_renders_form(self):
        r = self.client.get("/accounts/login/")
        self.assertEqual(r.status_code, 200)
        self.assertTemplateUsed(r, "accounts/login.html")

    def test_correct_credentials_log_in_and_redirect(self):
        r = self.client.post("/accounts/login/",
                             {"email": "judge@t.demo", "password": "correct-horse-battery"})
        self.assertEqual(r.status_code, 302)
        self.assertEqual(r["Location"], "/projects")
        self.assertIn("_auth_user_id", self.client.session)

    def test_email_match_is_case_insensitive(self):
        r = self.client.post("/accounts/login/",
                             {"email": "JUDGE@T.DEMO", "password": "correct-horse-battery"})
        self.assertEqual(r.status_code, 302)

    def test_wrong_password_is_401_and_does_not_log_in(self):
        r = self.client.post("/accounts/login/",
                             {"email": "judge@t.demo", "password": "nope"})
        self.assertEqual(r.status_code, 401)
        self.assertNotIn("_auth_user_id", self.client.session)

    def test_over_limit_is_429_with_retry_after(self):
        for _ in range(3):                 # 3/m -> the first three attempts are allowed
            self.client.post("/accounts/login/", {"email": "judge@t.demo", "password": "nope"})
        r = self.client.post("/accounts/login/", {"email": "judge@t.demo", "password": "nope"})
        self.assertEqual(r.status_code, 429)
        self.assertIn("Retry-After", r)

    def test_open_redirect_next_is_rejected(self):
        r = self.client.post("/accounts/login/",
                             {"email": "judge@t.demo", "password": "correct-horse-battery",
                              "next": "https://evil.example/steal"})
        self.assertEqual(r.status_code, 302)
        self.assertEqual(r["Location"], "/projects")     # not the attacker's host
