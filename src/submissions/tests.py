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
from django.test import RequestFactory, TestCase, override_settings
from django.utils import timezone

from accounts.models import AppUser, DemoSession
from audit.models import AuditEvent
from events.models import Event, EventMembership, Team, TeamMember, Track
from submissions.models import Submission

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


class SubmissionAdminDeleteGuardTests(TestCase):
    """SubmissionAdmin refuses to delete a submission whose assignment is SCORED (its Ballot ->
    BallotRevision history cascades off it), keeps an unscored submission deletable, and drops the
    bulk delete action. This is the submission arm of the parent-cascade close (THREAT-MODEL A7/A8),
    mirroring JudgeAssignmentAdmin's direct-row guard via ScoredCascadeDeleteGuard.
    """

    def setUp(self):
        from judging import services
        self.event = Event.objects.create(
            ext_id="evt_sg", name="SG", state=Event.CLOSED,
            submissions_close=timezone.now() - timedelta(days=1))
        self.track = Track.objects.create(ext_id="trk_sg", event=self.event, name="SG")
        self.team = Team.objects.create(ext_id="tm_sg", event=self.event, name="SG")
        self.scored = Submission.objects.create(
            ext_id="prj_scored", event=self.event, team=self.team, track=self.track,
            title="S", state=Submission.SUBMITTED)
        self.unscored = Submission.objects.create(
            ext_id="prj_unscored", event=self.event, team=self.team, track=self.track,
            title="U", state=Submission.SUBMITTED)
        judge_user = AppUser.objects.create_user(email="jsg@t.demo", display_name="J")
        judge = EventMembership.objects.create(
            user=judge_user, event=self.event, role=EventMembership.JUDGE, ext_id="mem_sg")
        services.record_ballot(judge, self.scored, functionality=3, quality=3, innovation=3)

        su = AppUser.objects.create_user(email="su@t.demo", display_name="SU")
        su.is_staff = su.is_superuser = True
        su.save()
        self.req = RequestFactory().get("/admin/")
        self.req.user = su

    def test_scored_submission_delete_refused_unscored_allowed(self):
        from django.contrib.admin.sites import AdminSite
        from submissions.admin import SubmissionAdmin
        ma = SubmissionAdmin(Submission, AdminSite())
        self.assertFalse(ma.has_delete_permission(self.req, self.scored))
        self.assertTrue(ma.has_delete_permission(self.req, self.unscored))
        self.assertNotIn("delete_selected", ma.get_actions(self.req))


@override_settings(CACHES=_LOCMEM)
class SubmissionEditWithdrawTests(TestCase):
    """Participant self-service: revise or soft-withdraw an OWNED submission while the event
    is still accepting writes. The gates live in submissions.services (auth -> participant ->
    ownership -> deadline), so these tests drive them through the real /submissions/ routes.

    Soft-withdraw is the load-bearing property: withdrawing flips state to WITHDRAWN and drops
    the project from the public gallery, but never deletes the row -- so any Ballot ->
    BallotRevision history and the audit trail beneath it survive (the same non-destructive
    stance the A8 admin guards enforce). A single event is created so gallery/submissions'
    `_current_event()` (first by id) resolves to it.
    """

    @classmethod
    def setUpTestData(cls):
        cls.event = Event.objects.create(
            ext_id="evt_ew", name="EW", state=Event.OPEN,
            submissions_close=timezone.now() + timedelta(days=1))
        cls.track = Track.objects.create(ext_id="trk_ew", event=cls.event, name="Main")
        cls.alice = AppUser.objects.create_user(email="alice@t.demo", display_name="alice")
        EventMembership.objects.create(user=cls.alice, event=cls.event,
                                       role=EventMembership.PARTICIPANT)
        cls.team_a = Team.objects.create(ext_id="tm_a", event=cls.event, name="Team A")
        TeamMember.objects.create(team=cls.team_a, user=cls.alice)
        cls.bob = AppUser.objects.create_user(email="bob@t.demo", display_name="bob")
        EventMembership.objects.create(user=cls.bob, event=cls.event,
                                       role=EventMembership.PARTICIPANT)
        cls.team_b = Team.objects.create(ext_id="tm_b", event=cls.event, name="Team B")
        TeamMember.objects.create(team=cls.team_b, user=cls.bob)
        cls.sub = Submission.objects.create(
            ext_id="prj_ew", event=cls.event, team=cls.team_a, track=cls.track,
            title="Alpha Project", summary="first", repo_url="https://example.test/a",
            state=Submission.SUBMITTED)

    def setUp(self):
        caches["default"].clear()

    def _edit_url(self, ext="prj_ew"):
        return "/submissions/%s/edit" % ext

    # --- edit -----------------------------------------------------------------------------
    def test_owner_can_revise_while_open_and_it_is_audited(self):
        self.client.force_login(self.alice)
        resp = self.client.post(self._edit_url(), {
            "title": "Alpha Renamed", "summary": "revised", "repo_url": "https://example.test/z"})
        self.assertRedirects(resp, "/submissions/mine")
        self.sub.refresh_from_db()
        self.assertEqual(self.sub.title, "Alpha Renamed")
        self.assertEqual(self.sub.summary, "revised")
        self.assertTrue(AuditEvent.objects.filter(
            event_type="submission.revised", object_id="prj_ew").exists())

    def test_edit_get_prefills_current_values_for_owner(self):
        self.client.force_login(self.alice)
        resp = self.client.get(self._edit_url())
        self.assertEqual(resp.status_code, 200)
        self.assertContains(resp, "Alpha Project")

    def test_non_owner_cannot_edit(self):
        self.client.force_login(self.bob)
        self.assertEqual(self.client.get(self._edit_url()).status_code, 403)
        self.assertEqual(self.client.post(
            self._edit_url(), {"title": "hijack"}).status_code, 403)
        self.sub.refresh_from_db()
        self.assertEqual(self.sub.title, "Alpha Project")

    def test_unknown_ext_id_is_404(self):
        self.client.force_login(self.alice)
        self.assertEqual(self.client.get(self._edit_url("prj_nope")).status_code, 404)

    def test_anonymous_is_redirected_to_login(self):
        resp = self.client.get("/submissions/mine")
        self.assertEqual(resp.status_code, 302)
        self.assertTrue(resp["Location"].startswith("/accounts/login/"))
    # SENTINEL_EW_TESTS

    # --- withdraw (soft; preserves history) ------------------------------------------------
    def test_withdraw_hides_from_gallery_but_keeps_row_and_ballot(self):
        from judging import services as judging_services
        judge_user = AppUser.objects.create_user(email="jew@t.demo", display_name="J")
        judge = EventMembership.objects.create(
            user=judge_user, event=self.event, role=EventMembership.JUDGE, ext_id="mem_ew")
        judging_services.record_ballot(judge, self.sub,
                                       functionality=4, quality=4, innovation=4)
        from judging.models import Ballot
        self.assertEqual(Ballot.objects.filter(assignment__submission=self.sub).count(), 1)
        # visible before withdrawal
        self.assertContains(self.client.get("/projects"), "Alpha Project")

        self.client.force_login(self.alice)
        resp = self.client.post("/submissions/prj_ew/withdraw")
        self.assertRedirects(resp, "/submissions/mine")

        self.sub.refresh_from_db()
        self.assertEqual(self.sub.state, Submission.WITHDRAWN)
        # row + ballot history survive; only visibility changed
        self.assertTrue(Submission.objects.filter(ext_id="prj_ew").exists())
        self.assertEqual(Ballot.objects.filter(assignment__submission=self.sub).count(), 1)
        self.assertNotContains(self.client.get("/projects"), "Alpha Project")
        self.assertTrue(AuditEvent.objects.filter(
            event_type="submission.withdrawn", object_id="prj_ew").exists())

    def test_withdraw_is_idempotent_from_the_button(self):
        self.client.force_login(self.alice)
        self.client.post("/submissions/prj_ew/withdraw")
        # a second click still lands back on the list (no 500), state stays withdrawn
        self.assertRedirects(self.client.post("/submissions/prj_ew/withdraw"),
                             "/submissions/mine")
        self.sub.refresh_from_db()
        self.assertEqual(self.sub.state, Submission.WITHDRAWN)

    def test_withdrawn_submission_cannot_be_edited(self):
        self.client.force_login(self.alice)
        self.client.post("/submissions/prj_ew/withdraw")
        resp = self.client.post(self._edit_url(),
                                {"title": "sneak un-withdraw", "summary": "x"})
        self.assertEqual(resp.status_code, 400)
        self.sub.refresh_from_db()
        self.assertEqual(self.sub.state, Submission.WITHDRAWN)
        self.assertEqual(self.sub.title, "Alpha Project")

    # --- deadline gate (parity with check 3) -----------------------------------------------
    def test_edit_and_withdraw_refused_after_close(self):
        self.event.state = Event.CLOSED
        self.event.submissions_close = timezone.now() - timedelta(days=1)
        self.event.save(update_fields=["state", "submissions_close"])
        self.client.force_login(self.alice)
        self.assertEqual(self.client.post(
            self._edit_url(), {"title": "late", "summary": "x"}).status_code, 403)
        self.assertEqual(self.client.post("/submissions/prj_ew/withdraw").status_code, 403)
        self.sub.refresh_from_db()
        self.assertEqual(self.sub.title, "Alpha Project")
        self.assertEqual(self.sub.state, Submission.SUBMITTED)

    # --- my submissions list ---------------------------------------------------------------
    def test_mine_lists_owned_including_withdrawn(self):
        self.client.force_login(self.alice)
        self.client.post("/submissions/prj_ew/withdraw")
        resp = self.client.get("/submissions/mine")
        self.assertEqual(resp.status_code, 200)
        self.assertContains(resp, "Alpha Project")   # withdrawn rows still show on the owner's list
