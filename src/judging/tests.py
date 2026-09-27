# src/judging/tests.py
"""DB-backed tests for append-only ballot versioning (needs Postgres: DB CHECK + UNIQUE are
enforced there, and record_ballot's audit co-commit takes select_for_update).

Run by `manage.py test judging`. These prove the three guarantees migration 0002 adds: every
score write appends an immutable, monotonically-versioned revision; the DB itself refuses a
second write to the same (ballot, version) and any score outside 1..5 on EITHER table; and
the denormalized Ballot latest-pointer (what every existing reader sees) still tracks the
newest revision, so checks 4/5/6/7 stay byte-identical.
"""
from datetime import timedelta

from django.db import IntegrityError, transaction
from django.contrib.auth import get_user_model
from django.test import TestCase, override_settings
from django.urls import reverse
from django.utils import timezone

from audit.models import AuditEvent
from events.models import Event, EventMembership, Team, Track
from judging import services
from judging.models import Ballot, BallotRevision, JudgeAssignment
from submissions.models import Submission

User = get_user_model()


class BallotVersioningTests(TestCase):
    def _judgeable(self):
        u = User.objects.create_user(email="j@x.com", password=None)
        ev = Event.objects.create(ext_id="evt_t", name="T", state=Event.CLOSED,
                                  submissions_close=timezone.now() - timedelta(days=1))
        trk = Track.objects.create(ext_id="trk_t", event=ev, name="T")
        tm = Team.objects.create(ext_id="tm_t", event=ev, name="T")
        sub = Submission.objects.create(ext_id="prj_t", event=ev, team=tm, track=trk,
                                        title="X", state=Submission.SUBMITTED)
        mem = EventMembership.objects.create(user=u, event=ev, role=EventMembership.JUDGE,
                                             ext_id="jdg_t")
        return mem, sub

    def test_record_ballot_appends_immutable_versioned_revisions(self):
        mem, sub = self._judgeable()
        services.record_ballot(mem, sub, functionality=5, quality=4, innovation=3)
        services.record_ballot(mem, sub, functionality=2, quality=2, innovation=1)

        # one ballot identity, two immutable revisions in order
        self.assertEqual(Ballot.objects.count(), 1)
        ballot = Ballot.objects.get()
        revs = list(ballot.revisions.order_by("version"))
        self.assertEqual([r.version for r in revs], [1, 2])
        # v1 is preserved verbatim -- the overwrite did NOT mutate history
        self.assertEqual((revs[0].functionality, revs[0].quality, revs[0].innovation),
                         (5, 4, 3))
        self.assertEqual((revs[1].functionality, revs[1].quality, revs[1].innovation),
                         (2, 2, 1))
        # the Ballot latest-pointer mirrors the newest revision (readers stay byte-stable)
        self.assertEqual((ballot.functionality, ballot.quality, ballot.innovation),
                         (2, 2, 1))
        # the audit event for the latest write carries the matching version
        ev = AuditEvent.objects.filter(event_type="ballot.recorded").order_by("seq").last()
        self.assertEqual(ev.payload["version"], 2)

    def test_version_is_write_once(self):
        mem, sub = self._judgeable()
        services.record_ballot(mem, sub, functionality=3, quality=3, innovation=3)
        ballot = Ballot.objects.get()
        with self.assertRaises(IntegrityError):
            with transaction.atomic():
                BallotRevision.objects.create(ballot=ballot, version=1,
                                              functionality=1, quality=1, innovation=1)

    def test_db_rejects_out_of_range_revision(self):
        mem, sub = self._judgeable()
        services.record_ballot(mem, sub, functionality=3, quality=3, innovation=3)
        ballot = Ballot.objects.get()
        with self.assertRaises(IntegrityError):
            with transaction.atomic():
                BallotRevision.objects.create(ballot=ballot, version=99,
                                              functionality=6, quality=3, innovation=3)

    def test_db_rejects_out_of_range_ballot(self):
        mem, sub = self._judgeable()
        assignment = JudgeAssignment.objects.create(judge=mem, submission=sub)
        with self.assertRaises(IntegrityError):
            with transaction.atomic():
                Ballot.objects.create(assignment=assignment,
                                      functionality=0, quality=3, innovation=3)

    def test_reader_sees_latest_revision_values(self):
        mem, sub = self._judgeable()
        services.record_ballot(mem, sub, functionality=5, quality=5, innovation=5)
        services.record_ballot(mem, sub, functionality=1, quality=2, innovation=3)
        rows = services.scores_for_judge(mem)
        self.assertEqual(len(rows), 1)
        self.assertEqual((rows[0]["functionality"], rows[0]["quality"],
                          rows[0]["innovation"]), (1, 2, 3))


_RATES = {"login": "10/m", "invite_redeem": "20/h",
          "submission_write": "60/h", "ballot_write": "2/h"}
_LOCMEM = {"default": {"BACKEND": "django.core.cache.backends.locmem.LocMemCache"}}


class ScoreEndpointTests(TestCase):
    """The in-app judge scoring page (GET/POST /judging/score). Proves the write path is
    judge-only, assignment-scoped, append-only + audited, and throttled -- without touching the
    five flat checker routes."""

    def setUp(self):
        self.url = reverse("judging:score")
        self.assertEqual(self.url, "/judging/score")           # ordering vs the flat routes
        self.event = Event.objects.create(
            ext_id="evt_s", name="S", state=Event.CLOSED,
            submissions_close=timezone.now() - timedelta(days=1))
        self.trk = Track.objects.create(ext_id="trk_s", event=self.event, name="S")
        self.team = Team.objects.create(ext_id="tm_s", event=self.event, name="S")
        self.assigned = Submission.objects.create(
            ext_id="prj_assigned", event=self.event, team=self.team, track=self.trk,
            title="Assigned Project", state=Submission.SUBMITTED)
        self.unassigned = Submission.objects.create(
            ext_id="prj_unassigned", event=self.event, team=self.team, track=self.trk,
            title="Unassigned Project", state=Submission.SUBMITTED)
        self.judge_user = User.objects.create_user(email="judge@x.com", password="pw")
        self.judge = EventMembership.objects.create(
            user=self.judge_user, event=self.event, role=EventMembership.JUDGE, ext_id="jdg_s")
        JudgeAssignment.objects.create(judge=self.judge, submission=self.assigned)
        self.outsider = User.objects.create_user(email="outsider@x.com", password="pw")

    def _post(self, **over):
        data = {"submission": self.assigned.ext_id, "functionality": "5",
                "quality": "4", "innovation": "3", "comment": "solid"}
        data.update(over)
        return self.client.post(self.url, data)

    def test_get_lists_only_assigned_submissions(self):
        self.client.force_login(self.judge_user)
        resp = self.client.get(self.url)
        self.assertEqual(resp.status_code, 200)
        self.assertContains(resp, "Assigned Project")
        self.assertNotContains(resp, "Unassigned Project")

    def test_post_records_ballot_revision_and_audit(self):
        self.client.force_login(self.judge_user)
        resp = self._post()
        self.assertEqual(resp.status_code, 302)                       # PRG
        self.assertEqual(Ballot.objects.count(), 1)
        ballot = Ballot.objects.get()
        self.assertEqual((ballot.functionality, ballot.quality, ballot.innovation), (5, 4, 3))
        self.assertEqual([r.version for r in ballot.revisions.order_by("version")], [1])
        self.assertTrue(AuditEvent.objects.filter(event_type="ballot.recorded").exists())
        # a second write appends v2 and moves the latest-pointer -- history is preserved
        self._post(functionality="2", quality="2", innovation="1")
        ballot.refresh_from_db()
        self.assertEqual((ballot.functionality, ballot.quality, ballot.innovation), (2, 2, 1))
        self.assertEqual([r.version for r in ballot.revisions.order_by("version")], [1, 2])

    def test_out_of_range_score_is_400_and_writes_nothing(self):
        self.client.force_login(self.judge_user)
        resp = self._post(functionality="6")
        self.assertEqual(resp.status_code, 400)
        self.assertEqual(Ballot.objects.count(), 0)

    def test_cannot_score_a_submission_not_assigned_to_you(self):
        self.client.force_login(self.judge_user)
        resp = self._post(submission=self.unassigned.ext_id)
        self.assertEqual(resp.status_code, 403)
        self.assertFalse(Ballot.objects.filter(
            assignment__submission=self.unassigned).exists())

    def test_non_judge_is_forbidden(self):
        self.client.force_login(self.outsider)
        self.assertEqual(self.client.get(self.url).status_code, 403)
        self.assertEqual(self._post().status_code, 403)

    def test_anonymous_is_redirected_to_login(self):
        resp = self.client.get(self.url)
        self.assertEqual(resp.status_code, 302)
        self.assertTrue(resp["Location"].startswith("/accounts/login/"))

    @override_settings(CACHES=_LOCMEM, DOGFOOD_RATE_LIMITS=_RATES)
    def test_rate_limited_after_quota(self):
        self.client.force_login(self.judge_user)
        self.assertEqual(self._post().status_code, 302)               # 1/2
        self.assertEqual(self._post().status_code, 302)               # 2/2
        resp = self._post()                                           # 3rd -> over
        self.assertEqual(resp.status_code, 429)
        self.assertIn("Retry-After", resp)
