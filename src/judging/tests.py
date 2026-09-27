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
from django.core.exceptions import ValidationError
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


class ControlRoomTests(TestCase):
    """Organizer control room: judging-progress coverage (#48), judge-assignment management
    (#39, append-only-safe removal), and rubric-weight configuration (#41). All event-scoped,
    organizer-only, no migration."""

    def setUp(self):
        self.event = Event.objects.create(ext_id="evt_cr", name="CR", state=Event.OPEN,
                                           submissions_close=timezone.now() + timedelta(days=1))
        self.other = Event.objects.create(ext_id="evt_other", name="Other", state=Event.OPEN,
                                           submissions_close=timezone.now() + timedelta(days=1))
        self.trk = Track.objects.create(ext_id="trk_cr", event=self.event, name="Main")
        self.tm = Team.objects.create(ext_id="tm_cr", event=self.event, name="Team")
        self.sub1 = Submission.objects.create(ext_id="prj_cr1", event=self.event, team=self.tm,
                                              track=self.trk, title="One",
                                              state=Submission.SUBMITTED)
        self.sub2 = Submission.objects.create(ext_id="prj_cr2", event=self.event, team=self.tm,
                                              track=self.trk, title="Two",
                                              state=Submission.SUBMITTED)
        self.sub3 = Submission.objects.create(ext_id="prj_cr3", event=self.event, team=self.tm,
                                              track=self.trk, title="Three",
                                              state=Submission.SUBMITTED)
        otrk = Track.objects.create(ext_id="trk_o", event=self.other, name="O")
        otm = Team.objects.create(ext_id="tm_o", event=self.other, name="O")
        self.foreign_sub = Submission.objects.create(ext_id="prj_o", event=self.other, team=otm,
                                                     track=otrk, title="Foreign",
                                                     state=Submission.SUBMITTED)
        self.org_u = User.objects.create_user(email="org@x.com", password="x")
        self.j1_u = User.objects.create_user(email="j1@x.com", password="x")
        self.j2_u = User.objects.create_user(email="j2@x.com", password="x")
        self.outsider = User.objects.create_user(email="p@x.com", password="x")
        EventMembership.objects.create(user=self.org_u, event=self.event,
                                       role=EventMembership.ORGANIZER, ext_id="org_cr")
        self.j1 = EventMembership.objects.create(user=self.j1_u, event=self.event,
                                                 role=EventMembership.JUDGE, ext_id="jdg_1")
        self.j2 = EventMembership.objects.create(user=self.j2_u, event=self.event,
                                                 role=EventMembership.JUDGE, ext_id="jdg_2")
        self.part = EventMembership.objects.create(user=self.outsider, event=self.event,
                                                   role=EventMembership.PARTICIPANT, ext_id="ptx_1")
    # SENTINEL_CR_TESTS

    # ---- #48 progress ----
    def test_progress_counts_and_status(self):
        services.assign_judge(self.org_u, self.event, judge_ext_id="jdg_1",
                              submission_ext_id="prj_cr1")
        services.assign_judge(self.org_u, self.event, judge_ext_id="jdg_2",
                              submission_ext_id="prj_cr1")
        services.assign_judge(self.org_u, self.event, judge_ext_id="jdg_1",
                              submission_ext_id="prj_cr2")
        # jdg_1 scores prj_cr1 -> that assignment is "scored"
        services.record_ballot(self.j1, self.sub1, functionality=4, quality=4, innovation=4)
        prog = services.judging_progress(self.event)
        self.assertEqual(prog["totals"]["assignments"], 3)
        self.assertEqual(prog["totals"]["scored"], 1)
        self.assertEqual(prog["totals"]["pending"], 2)
        rows = {r["submission"].ext_id: r for r in prog["sub_rows"]}
        self.assertEqual((rows["prj_cr1"]["assigned"], rows["prj_cr1"]["scored"]), (2, 1))
        self.assertEqual(rows["prj_cr1"]["status"], "partial")
        self.assertEqual(rows["prj_cr3"]["status"], "uncovered")   # never assigned
        jrows = {r["judge"].ext_id: r for r in prog["judge_rows"]}
        self.assertEqual((jrows["jdg_1"]["assigned"], jrows["jdg_1"]["scored"]), (2, 1))
        self.assertEqual(jrows["jdg_2"]["pending"], 1)

    # ---- #39 assignment management ----
    def test_assign_creates_and_audits_and_is_idempotent(self):
        services.assign_judge(self.org_u, self.event, judge_ext_id="jdg_1",
                              submission_ext_id="prj_cr1")
        self.assertTrue(JudgeAssignment.objects.filter(
            judge=self.j1, submission=self.sub1).exists())
        self.assertEqual(AuditEvent.objects.filter(event_type="judge.assigned").count(), 1)
        # second identical assign: no new row, no new audit event
        services.assign_judge(self.org_u, self.event, judge_ext_id="jdg_1",
                              submission_ext_id="prj_cr1")
        self.assertEqual(JudgeAssignment.objects.filter(
            judge=self.j1, submission=self.sub1).count(), 1)
        self.assertEqual(AuditEvent.objects.filter(event_type="judge.assigned").count(), 1)

    def test_assign_rejects_non_judge_and_foreign_submission(self):
        with self.assertRaises(ValidationError):        # participant, not a judge
            services.assign_judge(self.org_u, self.event, judge_ext_id="ptx_1",
                                  submission_ext_id="prj_cr1")
        with self.assertRaises(ValidationError):        # submission from another event
            services.assign_judge(self.org_u, self.event, judge_ext_id="jdg_1",
                                  submission_ext_id="prj_o")
        self.assertEqual(JudgeAssignment.objects.count(), 0)
    # SENTINEL_CR_TESTS_2

    def test_unassign_unscored_ok_scored_blocked(self):
        services.assign_judge(self.org_u, self.event, judge_ext_id="jdg_1",
                              submission_ext_id="prj_cr1")
        services.assign_judge(self.org_u, self.event, judge_ext_id="jdg_2",
                              submission_ext_id="prj_cr1")
        # jdg_2 scores -> its assignment becomes permanent (append-only history)
        services.record_ballot(self.j2, self.sub1, functionality=3, quality=3, innovation=3)
        # jdg_1 (unscored) can be removed
        services.unassign_judge(self.org_u, self.event, judge_ext_id="jdg_1",
                                submission_ext_id="prj_cr1")
        self.assertFalse(JudgeAssignment.objects.filter(
            judge=self.j1, submission=self.sub1).exists())
        self.assertEqual(AuditEvent.objects.filter(event_type="judge.unassigned").count(), 1)
        # jdg_2 (scored) cannot -- and its ballot + revision survive intact
        with self.assertRaises(ValidationError):
            services.unassign_judge(self.org_u, self.event, judge_ext_id="jdg_2",
                                    submission_ext_id="prj_cr1")
        scored = JudgeAssignment.objects.get(judge=self.j2, submission=self.sub1)
        self.assertTrue(Ballot.objects.filter(assignment=scored).exists())
        self.assertEqual(BallotRevision.objects.filter(ballot__assignment=scored).count(), 1)

    def test_admin_delete_guard_on_scored_assignment(self):
        # The permanence rule holds in Django admin too, not just the control-room path: a scored
        # assignment cannot be deleted (even by a superuser), an unscored one still can, and the
        # bulk "delete selected" action is removed so it cannot bypass the per-object check.
        from django.contrib.admin.sites import AdminSite
        from django.test import RequestFactory
        from judging.admin import JudgeAssignmentAdmin
        services.assign_judge(self.org_u, self.event, judge_ext_id="jdg_1",
                              submission_ext_id="prj_cr1")
        services.assign_judge(self.org_u, self.event, judge_ext_id="jdg_2",
                              submission_ext_id="prj_cr2")
        services.record_ballot(self.j1, self.sub1, functionality=4, quality=4, innovation=4)
        scored = JudgeAssignment.objects.get(judge=self.j1, submission=self.sub1)
        unscored = JudgeAssignment.objects.get(judge=self.j2, submission=self.sub2)
        self.org_u.is_staff = self.org_u.is_superuser = True
        self.org_u.save()
        ma = JudgeAssignmentAdmin(JudgeAssignment, AdminSite())
        req = RequestFactory().get("/admin/")
        req.user = self.org_u
        self.assertFalse(ma.has_delete_permission(req, scored))   # scored: permanent, even for admin
        self.assertTrue(ma.has_delete_permission(req, unscored))  # unscored: still removable
        self.assertNotIn("delete_selected", ma.get_actions(req))  # bulk delete path removed

    # ---- #41 rubric weights ----
    def test_set_rubric_weights_and_audit(self):
        clean = services.set_rubric_weights(self.org_u, self.event,
                                            weights={"functionality": "2", "quality": "1",
                                                     "innovation": "0.5"})
        self.assertEqual(clean, {"functionality": 2.0, "quality": 1.0, "innovation": 0.5})
        self.assertEqual(services.current_weights(self.event),
                         {"functionality": 2.0, "quality": 1.0, "innovation": 0.5})
        self.assertEqual(AuditEvent.objects.filter(event_type="rubric.reweighted").count(), 1)

    def test_rubric_weights_reject_invalid_writing_nothing(self):
        for bad in ({"functionality": "0", "quality": "0", "innovation": "0"},   # sum zero
                    {"functionality": "-1", "quality": "1", "innovation": "1"},  # negative
                    {"functionality": "abc", "quality": "1", "innovation": "1"}):  # non-number
            with self.assertRaises(ValidationError):
                services.set_rubric_weights(self.org_u, self.event, weights=bad)
        from judging.models import RubricWeight
        self.assertEqual(RubricWeight.objects.filter(event=self.event).count(), 0)
        self.assertEqual(AuditEvent.objects.filter(event_type="rubric.reweighted").count(), 0)

    # ---- organizer guard (HTTP) ----
    def test_organizer_guard_on_progress(self):
        url = reverse("judging:progress", args=["evt_cr"])
        r_anon = self.client.get(url)
        self.assertEqual(r_anon.status_code, 302)
        self.assertTrue(r_anon["Location"].startswith("/accounts/login/"))
        self.client.force_login(self.outsider)
        self.assertEqual(self.client.get(url).status_code, 403)
        self.client.force_login(self.org_u)
        self.assertEqual(self.client.get(url).status_code, 200)
