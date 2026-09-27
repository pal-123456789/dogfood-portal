# src/normalize/tests.py
"""Tests for the normalization app.

Two layers. EngineTests exercises the pure-numpy estimator with no DB (SimpleTestCase) - the
mathematical invariants that make the ranking defensible: weighted composite, the singular
lambda=0 guard, connected-component counting, the per-component mean(b)=0 gauge, a lambda pick
that stays inside the grid, and determinism. NormalizeServiceTests wires a tiny connected event
through the ORM and asserts the service output plus the organizer-only access gate (401/403/200)
- the same gate shape as the CSV export, on routes that are NOT the checker's five.
"""
import numpy as np

from django.contrib.auth import get_user_model
from django.test import Client, SimpleTestCase, TestCase
from django.utils import timezone

from audit.models import AuditEvent
from audit.service import current_head
from events.models import Event, EventMembership, Team, Track
from judging.models import Ballot, JudgeAssignment, RubricWeight
from submissions.models import Submission

from normalize import engine, runs, services, signing, verify
from normalize.models import NormalizationRun

User = get_user_model()


class EngineTests(SimpleTestCase):
    def test_composite_weighted(self):
        raw = {"functionality": 5, "quality": 3, "innovation": 1}
        equal = {c: 1.0 for c in engine.CRITERIA}
        self.assertAlmostEqual(engine.composite(raw, equal), 3.0)
        heavy = {"functionality": 2.0, "quality": 1.0, "innovation": 1.0}
        self.assertAlmostEqual(engine.composite(raw, heavy), (2 * 5 + 3 + 1) / 4.0)

    def test_composite_zero_weights_raises(self):
        with self.assertRaises(ValueError):
            engine.composite({c: 3 for c in engine.CRITERIA}, {c: 0 for c in engine.CRITERIA})

    def test_fit_rejects_nonpositive_lambda(self):
        with self.assertRaises(ValueError):
            engine.fit([3.0, 4.0], ["j1", "j2"], ["s1", "s1"], 0.0)

    def test_components_split_then_bridge(self):
        jk, sk = ["j1", "j1", "j2", "j2"], ["s1", "s2", "s3", "s4"]
        self.assertEqual(engine.connected_components(jk, sk)[2], 2)
        self.assertEqual(engine.connected_components(jk + ["j1"], sk + ["s3"])[2], 1)

    def test_gauge_zero_per_component(self):
        rng = np.random.default_rng(0)
        jk = ["j%d" % j for j in range(4) for _ in range(4)]
        sk = ["s%d" % ((j + k) % 5) for j in range(4) for k in range(4)]
        y = list(rng.uniform(1, 5, size=len(jk)))
        _, b = engine.fit(y, jk, sk, 1.0)
        _, cbj, _ = engine.connected_components(jk, sk)
        self.assertLess(engine.component_gauge_error(b, cbj), 1e-9)

    def test_select_lambda_in_grid_and_positive(self):
        rng = np.random.default_rng(1)
        jk = ["j%d" % j for j in range(6) for _ in range(6)]
        sk = ["s%d" % ((j * 3 + k) % 8) for j in range(6) for k in range(6)]
        y = list(rng.uniform(1, 5, size=len(jk)))
        lam, _ = engine.select_lambda(y, jk, sk, repeats=2)
        self.assertIn(lam, engine.LAMBDA_GRID)
        self.assertGreater(lam, 0)

    def test_rank_report_deterministic(self):
        rng = np.random.default_rng(2)
        jk = ["j%d" % j for j in range(5) for _ in range(5)]
        sk = ["s%d" % ((j + 2 * k) % 7) for j in range(5) for k in range(5)]
        y = list(rng.uniform(1, 5, size=len(jk)))
        r1 = engine.rank_report(y, jk, sk, lam=1.0, n_boot=200, seed=0)
        r2 = engine.rank_report(y, jk, sk, lam=1.0, n_boot=200, seed=0)
        self.assertTrue(np.allclose(r1["q"], r2["q"]))

class NormalizeServiceTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.event = Event.objects.create(
            ext_id="evt_t", name="Test Event", state=Event.CLOSED,
            submissions_close=timezone.now() - timezone.timedelta(days=1))
        track = Track.objects.create(ext_id="trk_t", event=cls.event, name="T")
        team = Team.objects.create(ext_id="tm_t", event=cls.event, name="Team")
        for c in engine.CRITERIA:
            RubricWeight.objects.create(event=cls.event, criterion=c, weight=1.0)
        subs = {}
        for sid in ("prj_a", "prj_b", "prj_c"):
            subs[sid] = Submission.objects.create(
                ext_id=sid, event=cls.event, team=team, track=track,
                title="Title %s" % sid, state=Submission.SUBMITTED)
        # Perfectly additive design: quality prj_a>prj_b>prj_c, judge biases +/-1 about 0.
        base = {"prj_a": 4, "prj_b": 3, "prj_c": 2}
        bias = [0, 1, -1]
        for n in range(3):
            u = User.objects.create_user(email="judge%d@t.demo" % n, display_name="J%d" % n)
            m = EventMembership.objects.create(
                user=u, event=cls.event, role=EventMembership.JUDGE, ext_id="jdg_%d" % n)
            for sid, sub in subs.items():
                a = JudgeAssignment.objects.create(judge=m, submission=sub)
                v = base[sid] + bias[n]
                Ballot.objects.create(assignment=a, functionality=v, quality=v, innovation=v)
        cls.org = User.objects.create_user(email="org@t.demo", display_name="Org")
        EventMembership.objects.create(user=cls.org, event=cls.event,
                                       role=EventMembership.ORGANIZER)
        cls.participant = User.objects.create_user(email="part@t.demo", display_name="Part")
        EventMembership.objects.create(user=cls.participant, event=cls.event,
                                       role=EventMembership.PARTICIPANT)

    def test_observed_shape(self):
        y, jk, sk = services.observed(self.event)
        self.assertEqual(len(y), 9)
        self.assertEqual(set(sk), {"prj_a", "prj_b", "prj_c"})

    def test_leaderboard_invariants(self):
        data = services.leaderboard(self.event, n_boot=200)
        self.assertEqual(data["n_components"], 1)
        self.assertLess(data["gauge_error"], 1e-9)
        self.assertIn(data["lambda"], engine.LAMBDA_GRID)
        self.assertGreater(data["lambda"], 0)
        qs = [r["q"] for r in data["rows"]]
        self.assertEqual(qs, sorted(qs, reverse=True))       # ranked by quality, best first
        self.assertEqual(data["rows"][0]["submission"], "prj_a")

    def test_leaderboard_deterministic(self):
        a = services.leaderboard(self.event, n_boot=200)
        b = services.leaderboard(self.event, n_boot=200)
        self.assertEqual([r["submission"] for r in a["rows"]],
                         [r["submission"] for r in b["rows"]])
        self.assertEqual([r["q"] for r in a["rows"]], [r["q"] for r in b["rows"]])

    def test_gate_anonymous_401(self):
        c = Client()
        self.assertEqual(c.get("/normalize/").status_code, 401)
        self.assertEqual(c.get("/normalize/leaderboard.json").status_code, 401)

    def test_gate_participant_403(self):
        c = Client()
        c.force_login(self.participant)
        self.assertEqual(c.get("/normalize/").status_code, 403)
        self.assertEqual(c.get("/normalize/leaderboard.json").status_code, 403)

    def test_gate_organizer_200(self):
        c = Client()
        c.force_login(self.org)
        html = c.get("/normalize/")
        self.assertEqual(html.status_code, 200)
        self.assertTemplateUsed(html, "normalize/leaderboard.html")
        js = c.get("/normalize/leaderboard.json")
        self.assertEqual(js.status_code, 200)
        self.assertEqual(js.json()["rows"][0]["submission"], "prj_a")


class NormalizationRunTests(TestCase):
    """DB-backed tests for the signed, reproducible run (P2). Reuses the perfectly-additive
    fixture shape so the ranking is unambiguous, then asserts: a published run persists with a
    signature that verifies; a `normalization.published` event is co-committed on the audit chain
    (seq advances by exactly one, payload cross-links the run); the exported bundle verifies
    offline; tampering any bundle file fails verification; a same-seed rebuild reproduces the same
    inputs/result hashes; and -- the killer -- if the run INSERT fails, the atomic co-commit rolls
    the audit event back too (no run-without-record, no record-without-run)."""

    @classmethod
    def setUpTestData(cls):
        cls.event = Event.objects.create(
            ext_id="evt_r", name="Run Event", state=Event.CLOSED,
            submissions_close=timezone.now() - timezone.timedelta(days=1))
        track = Track.objects.create(ext_id="trk_r", event=cls.event, name="R")
        team = Team.objects.create(ext_id="tm_r", event=cls.event, name="Team")
        for c in engine.CRITERIA:
            RubricWeight.objects.create(event=cls.event, criterion=c, weight=1.0)
        subs = {}
        for sid in ("prj_a", "prj_b", "prj_c"):
            subs[sid] = Submission.objects.create(
                ext_id=sid, event=cls.event, team=team, track=track,
                title="Title %s" % sid, state=Submission.SUBMITTED)
        base = {"prj_a": 4, "prj_b": 3, "prj_c": 2}
        bias = [0, 1, -1]
        for n in range(3):
            u = User.objects.create_user(email="rjudge%d@t.demo" % n, display_name="RJ%d" % n)
            m = EventMembership.objects.create(
                user=u, event=cls.event, role=EventMembership.JUDGE, ext_id="jdg_r%d" % n)
            for sid, sub in subs.items():
                a = JudgeAssignment.objects.create(judge=m, submission=sub)
                v = base[sid] + bias[n]
                Ballot.objects.create(assignment=a, functionality=v, quality=v, innovation=v)

    def _key(self):
        from audit import receipts
        return receipts.generate_private_key()

    def test_publish_creates_signed_run_and_audit_event(self):
        key = self._key()
        head0 = current_head().seq
        run = runs.publish_run(self.event, key=key, n_boot=120, seed=0)
        self.assertTrue(NormalizationRun.objects.filter(pk=run.pk).exists())
        self.assertTrue(signing.verify_run(
            key.public_key(), signature=run.signature, engine_version=run.engine_version,
            instance_id=run.instance_id, event_ext_id=run.event_ext_id,
            run_ext_id=run.run_ext_id, inputs_hash=run.inputs_hash,
            result_hash=run.result_hash, created_at=run.created_at))
        self.assertEqual(current_head().seq, head0 + 1)      # chain advanced by exactly one
        ev = AuditEvent.objects.get(seq=run.audit_seq)
        self.assertEqual(ev.event_type, "normalization.published")
        self.assertEqual(ev.object_id, run.run_ext_id)
        self.assertEqual(ev.payload["result_hash"], run.result_hash)
        self.assertEqual(run.result["rows"][0]["submission"], "prj_a")

    def test_result_hash_stable_across_builds_same_seed(self):
        key = self._key()
        b1 = runs.build_run(self.event, key=key, n_boot=120, seed=0)
        b2 = runs.build_run(self.event, key=key, n_boot=120, seed=0)
        self.assertEqual(b1["inputs_hash"], b2["inputs_hash"])
        self.assertEqual(b1["result_hash"], b2["result_hash"])
        self.assertNotEqual(b1["run_ext_id"], b2["run_ext_id"])   # id/signature still unique

    def test_export_bundle_verifies_offline(self):
        import tempfile
        key = self._key()
        run = runs.publish_run(self.event, key=key, n_boot=120, seed=0)
        with tempfile.TemporaryDirectory() as d:
            runs.export_bundle(run, key.public_key(), d)
            ok, checks = verify.verify_bundle(d)
            self.assertTrue(ok, checks)
            self.assertTrue(all(passed for _n, passed, _d in checks))

    def test_tampered_inputs_fail_verification(self):
        import json
        import os
        import tempfile
        key = self._key()
        run = runs.publish_run(self.event, key=key, n_boot=120, seed=0)
        with tempfile.TemporaryDirectory() as d:
            runs.export_bundle(run, key.public_key(), d)
            p = os.path.join(d, "inputs.json")
            with open(p, encoding="utf-8") as fh:
                data = json.load(fh)
            b0 = data["ballots"][0]
            b0["functionality"] = 1 if b0["functionality"] != 1 else 5
            with open(p, "w", encoding="utf-8") as fh:
                json.dump(data, fh)
            ok, _checks = verify.verify_bundle(d)
            self.assertFalse(ok)

    def test_run_insert_failure_rolls_back_audit_event(self):
        from unittest import mock
        key = self._key()
        head0 = current_head().seq
        count0 = NormalizationRun.objects.count()
        with mock.patch.object(NormalizationRun.objects, "create",
                               side_effect=RuntimeError("boom")):
            with self.assertRaises(RuntimeError):
                runs.publish_run(self.event, key=key, n_boot=120, seed=0)
        self.assertEqual(NormalizationRun.objects.count(), count0)   # no orphan run
        self.assertEqual(current_head().seq, head0)                  # and no orphan chain record

