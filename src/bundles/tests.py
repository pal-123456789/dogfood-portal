# src/bundles/tests.py
"""DB-backed tests for the signed event export/import endpoints (run: python src/manage.py test bundles).

They drive the real service+view+URL stack: the site-admin gate (401 anonymous, 403 non-superuser
organizer), export -> verify_signed_bundle round-trip, the all-stages CSV, and the import paths
(round-trip reconstruct, skipped unknown users, tampered -> 400, existing ext_id -> 409, bad body).

The signature reuses the ONE /state Ed25519 key via audit.keys.ensure_private_key. setUpModule points
DOGFOOD_AUDIT_KEY at a throwaway temp path (the env override audit.keys.key_path honours), so signing
in a view and verifying share one key path and the suite is hermetic.
"""
import copy
import json
import os
import shutil
import tempfile

from django.contrib.auth import get_user_model
from django.test import TestCase
from django.utils import timezone

from events.models import Event, EventMembership, Team, TeamMember, Track
from submissions.models import Submission
from bundles import services

User = get_user_model()

_KEYDIR = None
_PREV_KEY = None


def setUpModule():
    global _KEYDIR, _PREV_KEY
    _KEYDIR = tempfile.mkdtemp(prefix="bundles_key_")
    _PREV_KEY = os.environ.get("DOGFOOD_AUDIT_KEY")
    os.environ["DOGFOOD_AUDIT_KEY"] = os.path.join(_KEYDIR, "audit_ed25519_key.pem")


def tearDownModule():
    if _PREV_KEY is None:
        os.environ.pop("DOGFOOD_AUDIT_KEY", None)
    else:
        os.environ["DOGFOOD_AUDIT_KEY"] = _PREV_KEY
    if _KEYDIR:
        shutil.rmtree(_KEYDIR, ignore_errors=True)
from judging.models import RubricWeight
from voting.models import VotingCampaign

# The 7 STRUCTURAL keys a submission carries -- proof that no per-judge score/ballot leaks.
_SUBMISSION_KEYS = {"ext_id", "title", "summary", "repo_url", "state",
                    "track_ext_id", "team_ext_id"}


class BundleEndpointTests(TestCase):
    # Structural counts the round-trip import must reproduce exactly (see setUpTestData).
    EXPECT = {"tracks": 2, "teams": 2, "team_members": 2, "memberships": 2,
              "submissions": 3, "rubric_weights": 2, "voting_campaign": 1}

    @classmethod
    def setUpTestData(cls):
        now = timezone.now()
        # results_published starts True so the round-trip can prove import RESETS it to False.
        cls.event = Event.objects.create(
            ext_id="evt_b", name="Spring Hack", state=Event.CLOSED,
            submissions_close=now, results_published=True)
        cls.trk_a = Track.objects.create(ext_id="trk_a", event=cls.event, name="AI")
        cls.trk_b = Track.objects.create(ext_id="trk_b", event=cls.event, name="Web")
        cls.tm_a = Team.objects.create(ext_id="tm_a", event=cls.event, name="Falcons")
        cls.tm_b = Team.objects.create(ext_id="tm_b", event=cls.event, name="Owls")

        cls.user_a = User.objects.create_user(
            email="a@example.org", password="pw", display_name="Ann")
        cls.user_b = User.objects.create_user(
            email="b@example.org", password="pw", display_name="Ben")
        for u, tm in ((cls.user_a, cls.tm_a), (cls.user_b, cls.tm_b)):
            EventMembership.objects.create(
                user=u, event=cls.event, role=EventMembership.PARTICIPANT)
            TeamMember.objects.create(team=tm, user=u)

        Submission.objects.create(ext_id="prj_1", event=cls.event, team=cls.tm_a,
                                  track=cls.trk_a, title="Alpha", summary="a",
                                  state=Submission.SUBMITTED)
        Submission.objects.create(ext_id="prj_2", event=cls.event, team=cls.tm_b,
                                  track=cls.trk_b, title="Beta", summary="b",
                                  state=Submission.SUBMITTED)
        Submission.objects.create(ext_id="prj_3", event=cls.event, team=cls.tm_a,
                                  track=cls.trk_a, title="Gamma", state=Submission.DRAFT)

        RubricWeight.objects.create(event=cls.event, criterion="quality", weight=1.0)
        RubricWeight.objects.create(event=cls.event, criterion="innovation", weight=2.0)
        VotingCampaign.objects.create(
            ext_id="vcmp_b", event=cls.event, mode=VotingCampaign.AUTHENTICATED,
            credit_budget=25, opens_at=now, closes_at=now + timezone.timedelta(days=1))

        # A site administrator (is_superuser) and a NON-superuser event organizer.
        cls.admin = User.objects.create_superuser(
            email="admin@example.org", password="pw", display_name="Admin")
        cls.organizer_user = User.objects.create_user(
            email="org@example.org", password="pw", display_name="Org")
        EventMembership.objects.create(
            user=cls.organizer_user, event=cls.event, role=EventMembership.ORGANIZER)

    # -- helpers -------------------------------------------------------------
    def _export_doc(self):
        """Log in as admin, GET the signed bundle, return the parsed doc (asserts 200)."""
        self.client.force_login(self.admin)
        resp = self.client.get("/bundles/evt_b/export.json")
        self.assertEqual(resp.status_code, 200)
        return resp.json()

    def _free_ext_ids(self, *, drop_user_b=False):
        """Delete the source event graph so its ext_ids are free to re-import.

        Submissions are deleted first (Submission.track is PROTECT), then the event cascades
        tracks/teams/memberships/rubric weights/voting campaign. AppUser rows survive unless
        drop_user_b, which removes b@example.org to exercise the skipped-user path.
        """
        Submission.objects.filter(event=self.event).delete()
        self.event.delete()
        if drop_user_b:
            User.objects.filter(email__iexact="b@example.org").delete()
    # -- export.json gate + shape ------------------------------------------
    def test_export_superuser_ok_and_verifies(self):
        doc = self._export_doc()
        self.assertEqual(doc["bundle"]["kind"], "dogfood.event-bundle.v1")
        self.assertEqual(doc["signature"]["algorithm"], "ed25519")
        self.assertTrue(services.verify_signed_bundle(doc))
        bundle = doc["bundle"]
        # No ballots / per-judge scores / audit chain travel in a bundle.
        self.assertNotIn("ballots", bundle)
        self.assertNotIn("scores", bundle)
        for s in bundle["submissions"]:
            self.assertEqual(set(s), _SUBMISSION_KEYS)
        self.assertEqual(len(bundle["submissions"]), 3)
        self.assertEqual(len(bundle["tracks"]), 2)
        self.assertEqual(len(bundle["teams"]), 2)
        members = [m for t in bundle["teams"] for m in t["members"]]
        self.assertEqual({m["email"] for m in members}, {"a@example.org", "b@example.org"})
        self.assertTrue(all(m["role"] == "participant" for m in members))

    def test_export_organizer_forbidden_403(self):
        self.client.force_login(self.organizer_user)          # event role != site admin
        resp = self.client.get("/bundles/evt_b/export.json")
        self.assertEqual(resp.status_code, 403)

    def test_export_anonymous_401(self):
        self.assertEqual(self.client.get("/bundles/evt_b/export.json").status_code, 401)

    def test_export_unknown_event_404(self):
        self.client.force_login(self.admin)
        self.assertEqual(self.client.get("/bundles/evt_missing/export.json").status_code, 404)

    # -- export.csv --------------------------------------------------------
    def test_export_csv_ok_unpublished_omits_rank_score(self):
        self.client.force_login(self.admin)
        resp = self.client.get("/bundles/evt_b/export.csv")
        self.assertEqual(resp.status_code, 200)
        self.assertIn("text/csv", resp["Content-Type"])
        self.assertEqual(resp["Content-Disposition"],
                         'attachment; filename="evt_b-all-stages.csv"')
        lines = resp.content.decode("utf-8").splitlines()
        self.assertEqual(lines[0], "submission_ext_id,title,team,track,state")
        self.assertNotIn("rank", lines[0])
        self.assertNotIn("score", lines[0])
        self.assertEqual(len(lines), 4)                        # header + 3 submissions

    def test_export_csv_gate(self):
        self.assertEqual(self.client.get("/bundles/evt_b/export.csv").status_code, 401)
        self.client.force_login(self.organizer_user)
        self.assertEqual(self.client.get("/bundles/evt_b/export.csv").status_code, 403)
    # -- import ------------------------------------------------------------
    def _post_import(self, body):
        """POST an import; `body` is a doc (dumped to JSON) or a raw string."""
        raw = body if isinstance(body, str) else json.dumps(body)
        return self.client.post("/bundles/import", data=raw,
                                content_type="application/json")

    def test_round_trip_import_reconstructs_counts(self):
        doc = self._export_doc()
        self._free_ext_ids()                                   # release ext_ids, keep users
        resp = self._post_import(doc)
        self.assertEqual(resp.status_code, 201)
        result = resp.json()
        self.assertEqual(result["event_ext_id"], "evt_b")
        self.assertEqual(result["counts"], self.EXPECT)
        self.assertEqual(result["skipped_users"], [])
        # Reconstructed graph is really in the DB, and results_published was RESET to False.
        ev = Event.objects.get(ext_id="evt_b")
        self.assertFalse(ev.results_published)
        self.assertEqual(ev.submissions.count(), 3)
        self.assertEqual(ev.tracks.count(), 2)
        self.assertEqual(TeamMember.objects.filter(team__event=ev).count(), 2)

    def test_import_skips_unknown_users(self):
        doc = self._export_doc()
        self._free_ext_ids(drop_user_b=True)                   # b@example.org no longer exists
        result = self._post_import(doc).json()
        self.assertEqual(result["skipped_users"], ["b@example.org"])
        self.assertEqual(result["counts"]["team_members"], 1)
        self.assertEqual(result["counts"]["memberships"], 1)
        self.assertEqual(result["counts"]["teams"], 2)         # both teams still reconstructed
        self.assertEqual(result["counts"]["submissions"], 3)
    def test_import_tampered_bundle_400(self):
        doc = self._export_doc()
        doc["bundle"]["event"]["name"] = "Doctored"            # signed field, not re-signed
        self.assertEqual(self._post_import(doc).status_code, 400)

    def test_import_existing_ext_id_conflict_409(self):
        doc = self._export_doc()                               # event still present -> conflict
        self.assertEqual(self._post_import(doc).status_code, 409)

    def test_import_missing_signature_400(self):
        doc = self._export_doc()
        del doc["signature"]
        self.assertEqual(self._post_import(doc).status_code, 400)

    def test_import_bad_json_400(self):
        self.client.force_login(self.admin)
        self.assertEqual(self._post_import("not json at all").status_code, 400)

    def test_import_anonymous_401(self):
        self.assertEqual(self._post_import({}).status_code, 401)

    def test_import_organizer_403(self):
        self.client.force_login(self.organizer_user)
        self.assertEqual(self._post_import({}).status_code, 403)




