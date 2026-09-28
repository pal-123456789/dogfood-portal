# src/records/tests.py
"""DB-backed tests for the signed participation-record endpoints (run: python src/manage.py test records).

They drive the real service+view+URL stack: the public signing key, the judge/participant gates
(401 anonymous, 403 wrong role, 403 cross-judge), a full sign -> /records/verify round-trip, the
doctored-record rejection, and the no-scores/no-ballots invariant.

The signature reuses the ONE /state Ed25519 key via audit.keys.ensure_private_key. Rather than write
to the real /state volume, setUpModule points DOGFOOD_AUDIT_KEY at a throwaway temp path (the env
override audit.keys.key_path() already honours), so signing in a view and verifying in /records/verify
share one key path and the suite is hermetic. No CACHES override is needed: these views touch neither
the rate limiter nor any cache table.
"""
import json
import os
import shutil
import tempfile

from django.contrib.auth import get_user_model
from django.test import TestCase
from django.utils import timezone

from events.models import Event, EventMembership, Team, TeamMember, Track
from judging.models import JudgeAssignment
from submissions.models import Submission

User = get_user_model()

_KEYDIR = None
_PREV_KEY = None


def setUpModule():
    global _KEYDIR, _PREV_KEY
    _KEYDIR = tempfile.mkdtemp(prefix="records_key_")
    _PREV_KEY = os.environ.get("DOGFOOD_AUDIT_KEY")
    os.environ["DOGFOOD_AUDIT_KEY"] = os.path.join(_KEYDIR, "audit_ed25519_key.pem")


def tearDownModule():
    if _PREV_KEY is None:
        os.environ.pop("DOGFOOD_AUDIT_KEY", None)
    else:
        os.environ["DOGFOOD_AUDIT_KEY"] = _PREV_KEY
    if _KEYDIR:
        shutil.rmtree(_KEYDIR, ignore_errors=True)


_SCORE_TOKENS = ("functionality", "quality", "innovation", "ballot", "score")


class RecordEndpointTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.event = Event.objects.create(
            ext_id="evt_r", name="Spring Hack", state=Event.CLOSED,
            submissions_close=timezone.now())
        trk = Track.objects.create(ext_id="trk_r", event=cls.event, name="AI")
        cls.team = Team.objects.create(ext_id="tm_r", event=cls.event, name="Falcons")
        cls.sub1 = Submission.objects.create(
            ext_id="prj_r1", event=cls.event, team=cls.team, track=trk,
            title="Alpha", state=Submission.SUBMITTED)
        cls.sub2 = Submission.objects.create(
            ext_id="prj_r2", event=cls.event, team=cls.team, track=trk,
            title="Beta", state=Submission.SUBMITTED)

        cls.judge_user = User.objects.create_user(
            email="judge@example.org", password="pw", display_name="Judge")
        cls.judge = EventMembership.objects.create(
            user=cls.judge_user, event=cls.event, role=EventMembership.JUDGE, ext_id="jdg_r")
        JudgeAssignment.objects.create(judge=cls.judge, submission=cls.sub1)
        JudgeAssignment.objects.create(judge=cls.judge, submission=cls.sub2)

        cls.part_user = User.objects.create_user(
            email="part@example.org", password="pw", display_name="Part")
        EventMembership.objects.create(
            user=cls.part_user, event=cls.event, role=EventMembership.PARTICIPANT)
        TeamMember.objects.create(team=cls.team, user=cls.part_user)

    # -- public signing key --------------------------------------------------
    def test_signing_key_is_public_pem_with_fingerprint(self):
        resp = self.client.get("/records/signing-key")          # anonymous
        self.assertEqual(resp.status_code, 200)
        self.assertIn("application/x-pem-file", resp["Content-Type"])
        body = resp.content.decode("ascii")
        self.assertIn("BEGIN PUBLIC KEY", body)
        self.assertEqual(len(resp["X-Signing-Key-Fingerprint"]), 64)  # sha256 hex
        self.assertNotIn("PRIVATE", body)                       # public half only

    # -- judge record --------------------------------------------------------
    def test_judge_record_ok_and_verifies(self):
        self.client.force_login(self.judge_user)
        resp = self.client.get("/records/judge?event=evt_r")
        self.assertEqual(resp.status_code, 200)
        rec = resp.json()
        self.assertEqual(rec["role"], "judge")
        self.assertEqual(rec["subject"]["membership_ext_id"], "jdg_r")
        self.assertEqual({i["title"] for i in rec["items"]}, {"Alpha", "Beta"})
        self.assertEqual(rec["signature"]["algorithm"], "ed25519")
        for token in _SCORE_TOKENS:                              # no scores/ballots ever
            self.assertNotIn(token, resp.content.decode("utf-8"))
        v = self.client.post("/records/verify", data=json.dumps(rec),
                             content_type="application/json")
        self.assertEqual(v.status_code, 200)
        self.assertTrue(v.json()["valid"])

    def test_judge_record_anonymous_401(self):
        self.assertEqual(self.client.get("/records/judge?event=evt_r").status_code, 401)

    def test_judge_record_non_judge_403(self):
        self.client.force_login(self.part_user)                 # a participant, not a judge
        self.assertEqual(self.client.get("/records/judge?event=evt_r").status_code, 403)

    def test_judge_record_cross_judge_403(self):
        self.client.force_login(self.judge_user)
        resp = self.client.get("/records/judge?event=evt_r&judge=jdg_other")
        self.assertEqual(resp.status_code, 403)

    def test_judge_record_own_judge_param_ok(self):
        self.client.force_login(self.judge_user)
        resp = self.client.get("/records/judge?event=evt_r&judge=jdg_r")
        self.assertEqual(resp.status_code, 200)

    # -- participant record --------------------------------------------------
    def test_participant_record_ok_no_scores(self):
        self.client.force_login(self.part_user)
        resp = self.client.get("/records/participant?event=evt_r")
        self.assertEqual(resp.status_code, 200)
        rec = resp.json()
        self.assertEqual(rec["role"], "participant")
        self.assertEqual(rec["subject"]["team_name"], "Falcons")
        self.assertIn("Alpha", {i["title"] for i in rec["items"]})
        text = resp.content.decode("utf-8")
        for token in _SCORE_TOKENS:
            self.assertNotIn(token, text)
        v = self.client.post("/records/verify", data=json.dumps(rec),
                             content_type="application/json")
        self.assertTrue(v.json()["valid"])

    def test_participant_record_anonymous_401(self):
        self.assertEqual(self.client.get("/records/participant?event=evt_r").status_code, 401)

    def test_participant_record_non_participant_403(self):
        self.client.force_login(self.judge_user)                # a judge, not a participant
        self.assertEqual(self.client.get("/records/participant?event=evt_r").status_code, 403)

    # -- verify --------------------------------------------------------------
    def test_verify_doctored_record_is_false(self):
        self.client.force_login(self.judge_user)
        rec = self.client.get("/records/judge?event=evt_r").json()
        rec["items"][0]["title"] = "Alpha (edited)"             # change a signed field
        v = self.client.post("/records/verify", data=json.dumps(rec),
                             content_type="application/json")
        self.assertEqual(v.status_code, 200)
        self.assertFalse(v.json()["valid"])

    def test_verify_malformed_body_is_false(self):
        v = self.client.post("/records/verify", data="not json",
                             content_type="application/json")
        self.assertEqual(v.status_code, 200)
        self.assertFalse(v.json()["valid"])

    def test_unknown_event_404(self):
        self.client.force_login(self.judge_user)
        self.assertEqual(self.client.get("/records/judge?event=evt_missing").status_code, 404)
