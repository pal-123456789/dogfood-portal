# src/submissions/tests.py
"""DB-backed tests for the submission-write throttle on POST /projects/new.

Two properties matter here. (1) A real logged-in participant over the limit gets 429. (2) The
DEMO shim -- the acceptance checker, identified by request.demo_shim -- is EXEMPT, so its POST to
a closed event stays a byte-identical 403 (check 3) no matter how many times it fires. Without
that exemption a burst from the checker could flip check 3 from 403 to 429 and break replay.

As in accounts/tests.py, CACHES is overridden to LocMemCache because the test DB has no cache
table, and the cache is cleared per test.
"""
from datetime import timedelta

from django.core.cache import caches
from django.test import TestCase, override_settings
from django.utils import timezone

from accounts.models import AppUser, DemoSession
from events.models import Event, EventMembership, Team, TeamMember, Track

_LOCMEM = {"default": {"BACKEND": "django.core.cache.backends.locmem.LocMemCache",
                       "LOCATION": "submissions-tests"}}


def _seed_participant(event, email):
    user = AppUser.objects.create_user(email=email, display_name=email.split("@")[0])
    EventMembership.objects.create(user=user, event=event, role=EventMembership.PARTICIPANT)
    team = Team.objects.create(ext_id="tm_%s" % email.split("@")[0], event=event, name="T")
    TeamMember.objects.create(team=team, user=user)
    return user


@override_settings(CACHES=_LOCMEM,
                   DOGFOOD_RATE_LIMITS={"login": "10/m", "invite_redeem": "20/h",
                                        "submission_write": "3/h", "ballot_write": "120/h"})
class SubmissionThrottleTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.event = Event.objects.create(
            ext_id="evt_rl", name="RL", state=Event.OPEN,
            submissions_close=timezone.now() + timedelta(days=1))
        cls.track = Track.objects.create(ext_id="trk_rl", event=cls.event, name="Main")
        cls.part = _seed_participant(cls.event, "part@t.demo")

    def setUp(self):
        caches["default"].clear()

    def _post(self):
        return self.client.post("/projects/new",
                                {"track": "trk_rl", "title": "P", "summary": "", "repo_url": ""})

    def test_real_user_is_throttled_after_the_limit(self):
        self.client.force_login(self.part)
        codes = [self._post().status_code for _ in range(4)]
        self.assertEqual(codes[:3], [201, 201, 201])       # 3/h allows three
        self.assertEqual(codes[3], 429)                     # the fourth is refused
        self.assertIn("Retry-After", self._post())


@override_settings(CACHES=_LOCMEM, DOGFOOD_DEMO=True,
                   DOGFOOD_RATE_LIMITS={"login": "10/m", "invite_redeem": "20/h",
                                        "submission_write": "2/h", "ballot_write": "120/h"})
class SubmissionDemoExemptionTests(TestCase):
    """The checker path must never be throttled -- proven by hammering a CLOSED event."""

    @classmethod
    def setUpTestData(cls):
        cls.event = Event.objects.create(
            ext_id="evt_rl_closed", name="RL", state=Event.CLOSED,
            submissions_close=timezone.now() - timedelta(days=1))
        cls.track = Track.objects.create(ext_id="trk_rl_c", event=cls.event, name="Main")
        cls.part = _seed_participant(cls.event, "demo@t.demo")
        DemoSession.objects.create(token="demo_tok", user=cls.part, label="participant")

    def setUp(self):
        caches["default"].clear()
        self.client.cookies["session"] = "demo_tok"        # the DEMO shim signal

    def test_demo_shim_is_exempt_and_stays_byte_identical(self):
        # submission_write is 2/h, but the checker POSTs five times; every response is the same
        # 403 (submissions closed), never a 429. That is the byte-stability guarantee for check 3.
        codes = [self.client.post(
            "/projects/new",
            {"track": "trk_rl_c", "title": "P"}).status_code for _ in range(5)]
        self.assertEqual(codes, [403, 403, 403, 403, 403])
