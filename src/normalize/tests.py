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

from normalize import diagnostics, engine, results, runs, services, signing, verify
from normalize.models import NormalizationRun, ResultPublication

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


class ResultPublicationTests(TestCase):
    """W1 -- publishing a signed run as the event's official, PUBLIC result.

    Reuses the perfectly-additive fixture (prj_a first) and asserts: a publish creates a versioned
    ResultPublication + a co-committed `results.published` audit event and flips
    Event.results_published; a second publish APPENDS v2 (never an update); the public results page
    is empty until an organizer publishes (the W1 access property) and serves the frozen run result
    after; the publish console is organizer-gated (401/403/200); an organizer POST publishes; and --
    the killer -- if the publication INSERT fails, the run, BOTH audit events, and the flag ALL roll
    back (no half-published event)."""

    @classmethod
    def setUpTestData(cls):
        cls.event = Event.objects.create(
            ext_id="evt_p", name="Publish Event", state=Event.CLOSED,
            submissions_close=timezone.now() - timezone.timedelta(days=1))
        track = Track.objects.create(ext_id="trk_p", event=cls.event, name="P")
        team = Team.objects.create(ext_id="tm_p", event=cls.event, name="Team")
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
            u = User.objects.create_user(email="pjudge%d@t.demo" % n, display_name="PJ%d" % n)
            m = EventMembership.objects.create(
                user=u, event=cls.event, role=EventMembership.JUDGE, ext_id="jdg_p%d" % n)
            for sid, sub in subs.items():
                a = JudgeAssignment.objects.create(judge=m, submission=sub)
                v = base[sid] + bias[n]
                Ballot.objects.create(assignment=a, functionality=v, quality=v, innovation=v)
        cls.org = User.objects.create_user(email="porg@t.demo", display_name="POrg")
        EventMembership.objects.create(user=cls.org, event=cls.event,
                                       role=EventMembership.ORGANIZER)
        cls.participant = User.objects.create_user(email="ppart@t.demo", display_name="PPart")
        EventMembership.objects.create(user=cls.participant, event=cls.event,
                                       role=EventMembership.PARTICIPANT)

    def _key(self):
        from audit import receipts
        return receipts.generate_private_key()

    def test_publish_creates_version_flips_flag_and_audits(self):
        key = self._key()
        head0 = current_head().seq
        self.assertFalse(Event.objects.get(pk=self.event.pk).results_published)
        pub, run = results.publish_results(self.event, key=key, note="n", n_boot=120, seed=0)
        self.assertEqual(pub.version, 1)
        self.assertEqual(pub.status, ResultPublication.FINAL)
        self.assertEqual(pub.run_ext_id, run.run_ext_id)
        # results.published co-committed AFTER normalization.published: head advanced by exactly two.
        self.assertEqual(current_head().seq, head0 + 2)
        ev = AuditEvent.objects.get(seq=pub.audit_seq)
        self.assertEqual(ev.event_type, "results.published")
        self.assertEqual(ev.payload["result_hash"], run.result_hash)
        self.assertEqual(ev.payload["version"], 1)
        self.assertTrue(Event.objects.get(pk=self.event.pk).results_published)
        data = results.current_results(self.event)
        self.assertTrue(data["published"])
        self.assertEqual(data["result"]["rows"][0]["submission"], "prj_a")
        self.assertEqual(data["result_hash"], run.result_hash)

    def test_second_publish_appends_next_version(self):
        key = self._key()
        p1, _ = results.publish_results(self.event, key=key, n_boot=120, seed=0)
        p2, _ = results.publish_results(self.event, key=key,
                                        status=ResultPublication.PROVISIONAL, n_boot=120, seed=0)
        self.assertEqual((p1.version, p2.version), (1, 2))
        self.assertEqual(results.current_publication(self.event).version, 2)
        self.assertEqual(len(results.publication_history(self.event)), 2)

    def test_public_results_private_until_published(self):
        c = Client()
        before = c.get("/normalize/results")
        self.assertEqual(before.status_code, 200)              # public route, never gated
        self.assertTemplateUsed(before, "normalize/results.html")
        self.assertFalse(c.get("/normalize/results.json").json()["published"])
        results.publish_results(self.event, key=self._key(), n_boot=120, seed=0)
        after = c.get("/normalize/results.json").json()
        self.assertTrue(after["published"])
        self.assertEqual(after["result"]["rows"][0]["submission"], "prj_a")

    def test_publish_console_is_organizer_gated(self):
        anon = Client()
        self.assertEqual(anon.get("/normalize/results/publish").status_code, 401)
        part = Client()
        part.force_login(self.participant)
        self.assertEqual(part.get("/normalize/results/publish").status_code, 403)
        org = Client()
        org.force_login(self.org)
        ok = org.get("/normalize/results/publish")
        self.assertEqual(ok.status_code, 200)
        self.assertTemplateUsed(ok, "normalize/results_publish.html")

    def test_organizer_post_publishes(self):
        from unittest import mock
        org = Client()
        org.force_login(self.org)
        with mock.patch("normalize.views.keys.ensure_private_key",
                        return_value=(self._key(), False)):
            resp = org.post("/normalize/results/publish", {"status": "final", "note": "Final."})
        self.assertEqual(resp.status_code, 302)
        self.assertEqual(ResultPublication.objects.filter(event_ext_id="evt_p").count(), 1)
        self.assertTrue(Event.objects.get(pk=self.event.pk).results_published)

    def test_publication_insert_failure_rolls_back_everything(self):
        from unittest import mock
        key = self._key()
        head0 = current_head().seq
        runs0 = NormalizationRun.objects.count()
        with mock.patch.object(ResultPublication.objects, "create",
                               side_effect=RuntimeError("boom")):
            with self.assertRaises(RuntimeError):
                results.publish_results(self.event, key=key, n_boot=120, seed=0)
        self.assertEqual(NormalizationRun.objects.count(), runs0)      # run rolled back
        self.assertEqual(ResultPublication.objects.count(), 0)         # no publication
        self.assertEqual(current_head().seq, head0)                    # both audit events rolled back
        self.assertFalse(Event.objects.get(pk=self.event.pk).results_published)


class DiagnosticsEngineTests(SimpleTestCase):
    """Pure (no DB) tests of normalize.diagnostics.report on hand-built panels. Each targets ONE
    diagnostic with a case whose answer is unambiguous - a surprising ballot that must top the
    residual flags, a single judge whose removal flips the winner, a bridge judge whose removal
    splits the comparison graph, a constant-scoring judge - plus the honest-scope keys, the empty
    panel, no-false-positive on clean data, and determinism. Thresholds are asserted to be the
    pre-registered module constants, never re-derived from the data."""

    def test_scope_and_prereg_thresholds(self):
        rep = diagnostics.report([4.0, 3.0, 4.0, 3.0], ["j0", "j0", "j1", "j1"],
                                 ["s0", "s1", "s0", "s1"], lam=1.0)
        self.assertEqual(rep["scope"], "review diagnostics, not fraud detection")
        self.assertEqual(rep["thresholds"]["residual_z"], diagnostics.RESIDUAL_Z)
        self.assertEqual(rep["thresholds"]["influence_rank_shift"], diagnostics.INFLUENCE_RANK_SHIFT)
        self.assertEqual(rep["thresholds"]["lowdisc_min_ballots"], diagnostics.LOWDISC_MIN_BALLOTS)
        for key in ("residuals", "ballot_influence", "judge_influence", "coverage"):
            self.assertIn(key, rep)

    def test_empty_panel(self):
        self.assertEqual(diagnostics.report([], [], [])["n_ballots"], 0)

    def test_surprising_ballot_tops_residual_flags(self):
        # 6 subs x 5 judges, exactly additive; one ballot pushed +12 must be the top-|z| flag.
        subs, judges = ["s%d" % i for i in range(6)], ["j%d" % i for i in range(5)]
        q = {s: 6.0 - i for i, s in enumerate(subs)}
        b = {j: (i - 2) * 0.3 for i, j in enumerate(judges)}   # -0.6..0.6, mean 0
        y, jk, sk = [], [], []
        for s in subs:
            for j in judges:
                y.append(q[s] + b[j]); jk.append(j); sk.append(s)
        y[0] += 12.0                                            # (s0, j0) outlier
        res = diagnostics.report(y, jk, sk, lam=0.01)["residuals"]
        self.assertGreaterEqual(res["max_abs_z"], diagnostics.RESIDUAL_Z)
        self.assertEqual((res["flagged"][0]["submission"], res["flagged"][0]["judge"]), ("s0", "j0"))

    def test_clean_additive_panel_flags_nothing(self):
        subs, judges = ["s0", "s1", "s2"], ["j0", "j1", "j2"]
        q, b = {"s0": 4.0, "s1": 3.0, "s2": 2.0}, {"j0": 0.0, "j1": 1.0, "j2": -1.0}
        y, jk, sk = [], [], []
        for s in subs:
            for j in judges:
                y.append(q[s] + b[j]); jk.append(j); sk.append(s)
        res = diagnostics.report(y, jk, sk, lam=0.01)["residuals"]
        self.assertEqual(res["flagged"], [])
        self.assertLess(res["max_abs_z"], diagnostics.RESIDUAL_Z)

    def test_leave_one_judge_out_can_flip_winner(self):
        # Two close projects; two judges mildly favour beta, one judge strongly favours alpha.
        # With everyone alpha leads (its within-judge margin dominates); drop jx and beta wins.
        jk, sk, y = [], [], []

        def add(j, s, v):
            jk.append(j); sk.append(s); y.append(v)
        for j in ("j0", "j1"):
            add(j, "alpha", 2.0); add(j, "beta", 3.0)          # mild: beta > alpha by 1
        add("jx", "alpha", 5.0); add("jx", "beta", 1.0)        # strong: alpha > beta by 4
        rep = diagnostics.report(y, jk, sk, lam=1.0)
        self.assertEqual(rep["winner"], "alpha")
        ji = rep["judge_influence"]
        self.assertGreaterEqual(ji["winner_changes"], 1)
        jx = [r for r in ji["judges"] if r["judge"] == "jx"][0]
        self.assertTrue(jx["winner_changed"] and jx["flagged"])

    def test_articulation_judge_detected(self):
        # Two clusters joined ONLY through jx; dropping jx splits the graph 1 -> 2 components.
        jk, sk, y = [], [], []

        def add(j, s, v):
            jk.append(j); sk.append(s); y.append(v)
        for j in ("ja0", "ja1"):
            add(j, "a0", 4.0); add(j, "a1", 3.0)
        for j in ("jb0", "jb1"):
            add(j, "b0", 4.0); add(j, "b1", 3.0)
        add("jx", "a0", 4.0); add("jx", "b0", 4.0)             # the only bridge
        cov = diagnostics.report(y, jk, sk, lam=1.0)["coverage"]
        self.assertEqual(cov["n_components"], 1)
        self.assertEqual([a["judge"] for a in cov["articulation_judges"]], ["jx"])

    def test_low_discrimination_flagged_with_vectors(self):
        # jflat gives one identical vector to 4 projects (no separation); jvar varies.
        subs = ["s0", "s1", "s2", "s3"]
        jk, sk, y, vectors = [], [], [], []

        def add(j, s, f, q, i):
            jk.append(j); sk.append(s); y.append((f + q + i) / 3.0); vectors.append((f, q, i))
        for s in subs:
            add("jflat", s, 3, 3, 3)                           # constant -> zero separation
        for f, s in zip((5, 4, 3, 2), subs):
            add("jvar", s, f, f, f)                            # varies
        rep = diagnostics.report(y, jk, sk, vectors=vectors, lam=1.0)
        low = rep["low_discrimination"]
        self.assertEqual([j["judge"] for j in low], ["jflat"])
        self.assertEqual((low[0]["n_ballots"], low[0]["distinct_vectors"]), (4, 1))
        self.assertIsNotNone(rep["rubric_use"])

    def test_report_deterministic(self):
        import json
        subs, judges = ["s0", "s1", "s2", "s3", "s4"], ["j0", "j1", "j2"]
        y, jk, sk = [], [], []
        for i, s in enumerate(subs):
            for k, j in enumerate(judges):
                y.append(5 - i + 0.1 * k); jk.append(j); sk.append(s)
        a = json.dumps(diagnostics.report(y, jk, sk, lam=1.0), sort_keys=True)
        b = json.dumps(diagnostics.report(y, jk, sk, lam=1.0), sort_keys=True)
        self.assertEqual(a, b)


class ReviewDiagnosticsViewTests(TestCase):
    """DB-backed tests for the organizer-only review-diagnostics routes (P4). Reuses the
    perfectly-additive 3-judge x 3-submission fixture (prj_a first) so coverage is a single
    connected component with no articulation judge, then asserts the same 401/403/200 gate shape
    as the leaderboard on BOTH /normalize/diagnostics and diagnostics.json (routes that are NOT the
    checker's five), and that the organizer JSON carries the honest scope, the pre-registered
    thresholds, every panel section, and the expected clean-panel coverage."""

    @classmethod
    def setUpTestData(cls):
        cls.event = Event.objects.create(
            ext_id="evt_d", name="Diag Event", state=Event.CLOSED,
            submissions_close=timezone.now() - timezone.timedelta(days=1))
        track = Track.objects.create(ext_id="trk_d", event=cls.event, name="D")
        team = Team.objects.create(ext_id="tm_d", event=cls.event, name="Team")
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
            u = User.objects.create_user(email="djudge%d@t.demo" % n, display_name="DJ%d" % n)
            m = EventMembership.objects.create(
                user=u, event=cls.event, role=EventMembership.JUDGE, ext_id="jdg_d%d" % n)
            for sid, sub in subs.items():
                a = JudgeAssignment.objects.create(judge=m, submission=sub)
                v = base[sid] + bias[n]
                Ballot.objects.create(assignment=a, functionality=v, quality=v, innovation=v)
        cls.org = User.objects.create_user(email="dorg@t.demo", display_name="DOrg")
        EventMembership.objects.create(user=cls.org, event=cls.event,
                                       role=EventMembership.ORGANIZER)
        cls.participant = User.objects.create_user(email="dpart@t.demo", display_name="DPart")
        EventMembership.objects.create(user=cls.participant, event=cls.event,
                                       role=EventMembership.PARTICIPANT)

    def test_gate_anonymous_401(self):
        c = Client()
        self.assertEqual(c.get("/normalize/diagnostics").status_code, 401)
        self.assertEqual(c.get("/normalize/diagnostics.json").status_code, 401)

    def test_gate_participant_403(self):
        c = Client()
        c.force_login(self.participant)
        self.assertEqual(c.get("/normalize/diagnostics").status_code, 403)
        self.assertEqual(c.get("/normalize/diagnostics.json").status_code, 403)

    def test_gate_organizer_200_html(self):
        c = Client()
        c.force_login(self.org)
        html = c.get("/normalize/diagnostics")
        self.assertEqual(html.status_code, 200)
        self.assertTemplateUsed(html, "normalize/diagnostics.html")

    def test_organizer_json_shape_and_clean_coverage(self):
        c = Client()
        c.force_login(self.org)
        data = c.get("/normalize/diagnostics.json").json()
        self.assertEqual(data["scope"], "review diagnostics, not fraud detection")
        for key in ("thresholds", "residuals", "ballot_influence", "judge_influence",
                    "coverage", "low_discrimination", "rubric_use"):
            self.assertIn(key, data)
        self.assertEqual(data["n_ballots"], 9)
        self.assertEqual(data["winner"], "prj_a")
        self.assertEqual(data["coverage"]["n_components"], 1)
        self.assertEqual(data["coverage"]["articulation_judges"], [])
        self.assertEqual(data["ballot_influence"]["winner_changes"], 0)   # no single ballot flips it


class ReleaseBundleTests(TestCase):
    """The unified, offline-verifiable RELEASE bundle (#37): one directory that binds the published
    ranking -> a signed normalization run -> a `normalization.published` event ON the audit chain ->
    a signed audit checkpoint. Reuses the perfectly-additive 3x3 fixture (prj_a first), signs the run
    AND the checkpoint with ONE test key, and asserts: `release_bundle` writes all nine files and the
    bundle self-verifies; then a tamper matrix -- a flipped ranking.csv, an edited result.json, a
    truncated audit prefix, and a FORGED (unsigned) audit_seq -- each makes verification FAIL, and the
    CSV / audit_seq tampers fail EXACTLY the new cross-links while both sub-verifiers still pass."""

    FILES = ("run.json", "inputs.json", "result.json", "public-key.pem", "audit-prefix.jsonl",
             "checkpoint.json", "ranking.csv", "release.json", "verification-instructions.txt")

    @classmethod
    def setUpTestData(cls):
        cls.event = Event.objects.create(
            ext_id="evt_rel", name="Release Event", state=Event.CLOSED,
            submissions_close=timezone.now() - timezone.timedelta(days=1))
        track = Track.objects.create(ext_id="trk_rel", event=cls.event, name="R")
        team = Team.objects.create(ext_id="tm_rel", event=cls.event, name="Team")
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
            u = User.objects.create_user(email="reljudge%d@t.demo" % n, display_name="RelJ%d" % n)
            m = EventMembership.objects.create(
                user=u, event=cls.event, role=EventMembership.JUDGE, ext_id="jdg_rel%d" % n)
            for sid, sub in subs.items():
                a = JudgeAssignment.objects.create(judge=m, submission=sub)
                v = base[sid] + bias[n]
                Ballot.objects.create(assignment=a, functionality=v, quality=v, innovation=v)

    def setUp(self):
        from audit import receipts
        self.key = receipts.generate_private_key()
        self.pub, self.run = results.publish_results(
            self.event, key=self.key, note="Final.", n_boot=120, seed=0)

    def _export(self, d):
        from io import StringIO
        from unittest import mock
        from django.core.management import call_command
        with mock.patch("normalize.management.commands.release_bundle.keys.ensure_private_key",
                        return_value=(self.key, False)):
            call_command("release_bundle", d, stdout=StringIO())

    @staticmethod
    def _named(checks, needle):
        return [(n, ok, det) for n, ok, det in checks if needle in n]

    def test_bundle_self_verifies_with_all_files(self):
        import os
        import tempfile
        from normalize import release
        with tempfile.TemporaryDirectory() as d:
            self._export(d)
            for fname in self.FILES:
                self.assertTrue(os.path.exists(os.path.join(d, fname)), "missing %s" % fname)
            ok, checks = release.verify_release(d)
            self.assertTrue(ok, checks)
            self.assertTrue(all(passed for _n, passed, _det in checks))
            for needle in ("share one signer", "committed in the audit chain",
                           "within the signed checkpoint", "ranking.csv matches"):
                got = self._named(checks, needle)
                self.assertEqual(len(got), 1, needle)
                self.assertTrue(got[0][1], got)

    def test_tampered_ranking_csv_fails_only_that_crosslink(self):
        import os
        import tempfile
        from normalize import release
        with tempfile.TemporaryDirectory() as d:
            self._export(d)
            p = os.path.join(d, "ranking.csv")
            with open(p, encoding="utf-8") as fh:
                text = fh.read()
            with open(p, "w", encoding="utf-8") as fh:
                fh.write(text.replace("prj_a", "prj_X", 1))
            ok, checks = release.verify_release(d)
            self.assertFalse(ok)
            self.assertFalse(self._named(checks, "ranking.csv matches")[0][1])
            self.assertTrue(all(ok for n, ok, _d in checks if n.startswith("audit/")))
            self.assertTrue(all(ok for n, ok, _d in checks if n.startswith("run/")))

    def test_forged_audit_seq_fails_crosslink_though_signature_valid(self):
        import json
        import os
        import tempfile
        from normalize import release
        with tempfile.TemporaryDirectory() as d:
            self._export(d)
            p = os.path.join(d, "run.json")
            with open(p, encoding="utf-8") as fh:
                data = json.load(fh)
            data["audit_seq"] = 999999            # unsigned back-pointer -> repoint at a phantom event
            with open(p, "w", encoding="utf-8") as fh:
                json.dump(data, fh)
            ok, checks = release.verify_release(d)
            self.assertFalse(ok)
            # the run's SIGNATURE still verifies (audit_seq is not in RUN_FIELDS) ...
            self.assertTrue(all(ok for n, ok, _d in checks if n.startswith("run/")))
            # ... but the chain-commit cross-link rejects the forged seq.
            self.assertFalse(self._named(checks, "committed in the audit chain")[0][1])

    def test_tampered_result_json_fails_run_verifier(self):
        import json
        import os
        import tempfile
        from normalize import release
        with tempfile.TemporaryDirectory() as d:
            self._export(d)
            p = os.path.join(d, "result.json")
            with open(p, encoding="utf-8") as fh:
                data = json.load(fh)
            data["rows"][0]["q"] = data["rows"][0]["q"] + 1.0
            with open(p, "w", encoding="utf-8") as fh:
                json.dump(data, fh)
            self.assertFalse(release.verify_release(d)[0])

    def test_truncated_audit_prefix_fails_audit_verifier(self):
        import os
        import tempfile
        from normalize import release
        with tempfile.TemporaryDirectory() as d:
            self._export(d)
            p = os.path.join(d, "audit-prefix.jsonl")
            with open(p, encoding="utf-8") as fh:
                lines = [ln for ln in fh.read().splitlines() if ln.strip()]
            with open(p, "w", encoding="utf-8") as fh:
                fh.write("\n".join(lines[:-1]) + "\n")   # drop the signed head row
            self.assertFalse(release.verify_release(d)[0])


class ReleaseVerifyPureTests(SimpleTestCase):
    """Pure (no DB) tests of the canonical ranking CSV -- the exact bytes the exporter writes and the
    offline verifier regenerates. Both ends call `ranking_csv`, so byte-identity holds by construction;
    these pin the format (LF terminators, csv-quoted titles, compact floats) so a later change cannot
    silently alter it, and lock the cell formatter's bool / None / float rules."""

    def test_cell_formatter_rules(self):
        from normalize import release
        self.assertEqual(release._fmt(True), "true")
        self.assertEqual(release._fmt(False), "false")
        self.assertEqual(release._fmt(None), "")
        self.assertEqual(release._fmt(4.0), "4")        # compact float: no trailing ".0"
        self.assertEqual(release._fmt(1.5), "1.5")
        self.assertEqual(release._fmt(3), "3")

    def test_ranking_csv_is_canonical(self):
        from normalize import release
        result = {"rows": [
            {"rank": 1, "submission": "prj_a", "title": "Hello, World", "track": "trk_1",
             "q": 1.5, "raw_mean": 4.0, "delta": 0.5, "rank_lo": 1, "rank_median": 1.0,
             "rank_hi": 2, "n_ballots": 3, "tied_with_next": False, "component": 0}]}
        header = ("rank,submission,title,track,q,raw_mean,delta,rank_lo,rank_median,"
                  "rank_hi,n_ballots,tied_with_next,component\n")
        row = '1,prj_a,"Hello, World",trk_1,1.5,4,0.5,1,1,2,3,false,0\n'
        self.assertEqual(release.ranking_csv(result), header + row)   # comma-title gets csv-quoted
        self.assertEqual(release.ranking_csv({"rows": []}), header)   # header-only when empty
        self.assertEqual(release.ranking_csv(result), release.ranking_csv(result))  # deterministic

