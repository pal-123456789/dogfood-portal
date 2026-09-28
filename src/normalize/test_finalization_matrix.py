# src/normalize/test_finalization_matrix.py
"""Finalized-result mutation matrix: prove the PUBLISHED, signed result is immutable against
later changes to its effective inputs, while the organizer LIVE recompute correctly moves.

THE FINALIZATION BOUNDARY (the invariant every test below pins):
  * normalize.results.current_results(event) -> the FROZEN, signed run's stored `result` verbatim.
    It reads only two append-only tables -- ResultPublication (highest `version`) and the
    NormalizationRun it names -- and NEVER recomputes. None of the judging/submission inputs are
    on that read path, so it cannot move when live data changes.
  * normalize.services.leaderboard(event) -> the LIVE organizer recompute. It reads Ballot +
    RubricWeight straight from the DB, so it MAY move when live data changes.
So for each mutation the frozen published result stays byte/rank/hash-identical while the live
leaderboard reflects the change where that is meaningful. That divergence is the proof the
published artifact is immutable -- not that nothing recomputes.

REAL PUBLISH/FREEZE PATH USED (not faked): normalize.results.publish_results(event, key=...),
which in ONE transaction builds+signs+persists a NormalizationRun (normalize.runs.publish_run),
appends the next-version ResultPublication, co-commits the audit events, and flips
Event.results_published. current_results then serves that frozen run's stored `result`.

PUBLICATION IS SUPERSEDABLE, EACH PUBLISHED RUN IS IMMUTABLE (asserted, not assumed): the
"current official result" is the HIGHEST-version ResultPublication, so a later publish_results
APPENDS v2 and current_results returns it -- the only thing that changes current_results. The
v1 NormalizationRun row is never updated or deleted; test_supersession_* proves it is still
byte-identical after v2 exists. No in-place mutation of a published result is possible.

HONESTY: no finalization bug was found -- current_results reads only the two append-only publish
tables, and none of mutations 1-7 write to them, so the frozen result is immutable by construction.

DB-backed (TestCase): run with `manage.py test normalize`. It is NOT collected by CI's
`pytest tests/`, which is correct for a DB-backed test. Self-contained: its own seed helper; it
neither imports from nor edits normalize/tests.py.
"""
from __future__ import annotations

import copy

from django.contrib.auth import get_user_model
from django.test import TestCase
from django.utils import timezone

from events.models import Event, EventMembership, Team, Track
from judging.models import Ballot, BallotRevision, JudgeAssignment, RubricWeight
from judging.services import current_weights, record_ballot, set_rubric_weights
from submissions.models import Submission

from normalize import engine, results, services, signing
from normalize.models import NormalizationRun

User = get_user_model()

# Kept small: the published run's hash is compared only against ITSELF re-read from the DB, and the
# live "moved" assertions read only bootstrap-independent fields (raw_mean / n_ballots / n_judges).
N_BOOT = 120
LIVE_BOOT = 40

SUBS = ("prj_fm1", "prj_fm2", "prj_fm3", "prj_fm4")
LEVEL = {"prj_fm1": 5, "prj_fm2": 4, "prj_fm3": 3, "prj_fm4": 2}  # cleanly separated quality
JUDGE_BIAS = (0, 1, -1, 0)                                        # jdg_fm0..jdg_fm3 severity offsets


def _clamp(x):
    """Keep a synthesized score inside the model's 1..5 bound (the DB CheckConstraint + service)."""
    return max(1, min(5, int(x)))


def _seed_matrix_event(prefix):
    """Self-contained seed (does NOT touch normalize/tests.py).

    One CLOSED event; one track; one team; four submissions with separated quality; four judges
    who each score every submission (dense -> one connected component, distinct ranks 1..4); one
    extra UNASSIGNED 'spare' judge (a real JUDGE membership with no ballots yet, for the add/reassign
    mutations); and one organizer. Ballots are written through the REAL writer judging.record_ballot,
    so each carries a v1 BallotRevision -- the append-only history the matrix probes. Per-criterion
    scores differ (quality = functionality - 1) so a rubric reweight visibly moves the live composite.
    Returns a handles dict; callers persist only immutable lookups and re-fetch rows they mutate.
    """
    ev = Event.objects.create(ext_id="evt_%s" % prefix, name=prefix, state=Event.CLOSED,
                              submissions_close=timezone.now() - timezone.timedelta(days=1))
    trk = Track.objects.create(ext_id="trk_%s" % prefix, event=ev, name=prefix)
    tm = Team.objects.create(ext_id="tm_%s" % prefix, event=ev, name="Team")
    for c in engine.CRITERIA:
        RubricWeight.objects.create(event=ev, criterion=c, weight=1.0)
    subs = {}
    for sid in SUBS:
        subs[sid] = Submission.objects.create(ext_id=sid, event=ev, team=tm, track=trk,
                                              title="Title %s" % sid, state=Submission.SUBMITTED)
    for n, bias in enumerate(JUDGE_BIAS):
        u = User.objects.create_user(email="%sj%d@t.demo" % (prefix, n),
                                     display_name="%sJ%d" % (prefix, n))
        m = EventMembership.objects.create(user=u, event=ev, role=EventMembership.JUDGE,
                                           ext_id="jdg_%s%d" % (prefix, n))
        for sid in SUBS:
            lvl = LEVEL[sid]
            record_ballot(m, subs[sid], functionality=_clamp(lvl + bias),
                          quality=_clamp(lvl + bias - 1), innovation=_clamp(lvl + bias))
    su = User.objects.create_user(email="%sspare@t.demo" % prefix, display_name="%sSpare" % prefix)
    EventMembership.objects.create(user=su, event=ev, role=EventMembership.JUDGE,
                                   ext_id="jdg_%sspare" % prefix)
    ou = User.objects.create_user(email="%sorg@t.demo" % prefix, display_name="%sOrg" % prefix)
    EventMembership.objects.create(user=ou, event=ev, role=EventMembership.ORGANIZER)
    return {"event": ev, "subs": subs, "org_user": ou}

class FinalizationMutationMatrixTests(TestCase):
    """One published, signed run; then one mutation per method. Every method asserts the frozen
    current_results is byte/rank/hash-identical to the publish-time snapshot; where meaningful it
    also asserts the LIVE leaderboard moved. Each test re-fetches any row it mutates from the DB so
    in-memory class fixtures never leak between methods; TestCase rolls back every method anyway."""

    @classmethod
    def setUpTestData(cls):
        seed = _seed_matrix_event("fm")
        cls.event = seed["event"]
        cls.subs = seed["subs"]
        cls.org_user = seed["org_user"]

        # REAL publish/freeze path -- build + sign + persist a run and designate it official (v1).
        cls.key = signing.generate_private_key()
        # NB: attribute is v1_run, NOT run -- `run` is TestCase.run (the method unittest calls to
        # execute each test); assigning cls.run would shadow it and every method would raise
        # "TypeError: 'NormalizationRun' object is not callable".
        cls.pub, cls.v1_run = results.publish_results(
            cls.event, key=cls.key, note="Final.", n_boot=N_BOOT, seed=0)
        cls.v1_run_ext_id = cls.v1_run.run_ext_id
        cls.v1_result_hash = cls.v1_run.result_hash

        # Deep snapshot of the frozen published result (rows + order + q + result_hash).
        frozen = results.current_results(cls.event)
        assert frozen["published"] is True
        cls.frozen = copy.deepcopy(frozen)
        cls.frozen_result = copy.deepcopy(frozen["result"])
        cls.frozen_hash = frozen["result_hash"]
        cls.frozen_order = [r["submission"] for r in frozen["result"]["rows"]]
        cls.frozen_rank_q = {r["submission"]: (r["rank"], r["q"], r["raw_mean"])
                             for r in frozen["result"]["rows"]}
        cls.frozen_n_ballots = frozen["result"]["n_ballots"]

        # Live baseline -- only bootstrap-INDEPENDENT fields (safe to compare exactly).
        live0 = services.leaderboard(cls.event, n_boot=LIVE_BOOT, seed=0)
        cls.live0_rawmean = {r["submission"]: r["raw_mean"] for r in live0["rows"]}
        cls.live0_n_ballots = live0["n_ballots"]
        cls.live0_n_judges = live0["n_judges"]

    # --- helpers -------------------------------------------------------------------------------
    def _assert_frozen_unchanged(self):
        """current_results is still v1 and byte/rank/hash-identical to the publish-time snapshot."""
        cur = results.current_results(self.event)
        self.assertTrue(cur["published"])
        self.assertEqual(cur["version"], 1)                       # not superseded
        self.assertEqual(cur["run_ext_id"], self.v1_run_ext_id)
        self.assertEqual(cur["result_hash"], self.frozen_hash)    # signed projection unchanged
        self.assertEqual(cur["result"], self.frozen_result)       # whole dict byte-identical
        self.assertEqual([r["submission"] for r in cur["result"]["rows"]], self.frozen_order)
        self.assertEqual({r["submission"]: (r["rank"], r["q"], r["raw_mean"])
                          for r in cur["result"]["rows"]}, self.frozen_rank_q)

    def _live(self):
        return services.leaderboard(self.event, n_boot=LIVE_BOOT, seed=0)

    def _member(self, ext_id):
        return EventMembership.objects.get(event=self.event, ext_id=ext_id)

    def _sub(self, ext_id):
        return Submission.objects.get(event=self.event, ext_id=ext_id)

    # --- baseline ------------------------------------------------------------------------------
    def test_00_baseline_frozen_matches_live_at_publish(self):
        """Anchor: at publish time frozen and live agree on the boot-independent facts, so every
        later divergence a mutation produces is real, not a publish-time artifact."""
        self.assertEqual(self.frozen_n_ballots, 16)
        self.assertEqual(self.live0_n_ballots, 16)
        self.assertEqual(self.live0_n_judges, 4)
        self.assertEqual(sorted(rk for rk, _, _ in self.frozen_rank_q.values()), [1, 2, 3, 4])
        # raw_mean is bootstrap- and lambda-independent, so frozen == live exactly at publish.
        self.assertEqual({s: rq[2] for s, rq in self.frozen_rank_q.items()}, self.live0_rawmean)

    # --- mutation 1: add a brand-new ballot for an existing submission ------------------------
    def test_01_add_new_ballot(self):
        """The spare judge scores an existing submission through record_ballot. Live gains a ballot;
        the frozen published run is untouched."""
        spare = self._member("jdg_fmspare")
        record_ballot(spare, self._sub("prj_fm1"), functionality=3, quality=3, innovation=3)
        # live moved: one more ballot in the recompute
        self.assertEqual(self._live()["n_ballots"], self.live0_n_ballots + 1)
        # a real new ballot exists for the previously-unassigned spare judge
        self.assertTrue(Ballot.objects.filter(
            assignment__judge=spare, assignment__submission__ext_id="prj_fm1").exists())
        self._assert_frozen_unchanged()

    # --- mutation 2: edit a score -> a NEW append-only ballot revision -------------------------
    def test_02_edit_score_appends_revision(self):
        """Re-scoring via record_ballot appends BallotRevision v2 (history is append-only, never an
        overwrite) and moves that submission's live raw_mean. Frozen run untouched."""
        j0, sub = self._member("jdg_fm0"), self._sub("prj_fm1")
        ballot = Ballot.objects.get(assignment__judge=j0, assignment__submission=sub)
        self.assertEqual(ballot.revisions.count(), 1)             # seeded v1
        record_ballot(j0, sub, functionality=1, quality=1, innovation=1)
        ballot.refresh_from_db()
        self.assertEqual(ballot.functionality, 1)                 # denormalized current-score moved
        self.assertEqual(sorted(r.version for r in ballot.revisions.all()), [1, 2])  # appended
        # live moved: the edited submission's raw_mean changed; ballot COUNT is unchanged (an edit)
        live = self._live()
        self.assertEqual(live["n_ballots"], self.live0_n_ballots)
        self.assertNotEqual({r["submission"]: r["raw_mean"] for r in live["rows"]}["prj_fm1"],
                            self.live0_rawmean["prj_fm1"])
        self._assert_frozen_unchanged()

    # --- mutation 3: change a JudgeAssignment --------------------------------------------------
    def test_03_change_judge_assignment(self):
        """Repoint a scored assignment's judge FK to the spare judge (a real change to a
        JudgeAssignment row). The ballot is now attributed to a fifth judge in the LIVE composite, so
        n_judges rises 4 -> 5; the frozen run pinned the original attribution and does not move."""
        spare = self._member("jdg_fmspare")
        assignment = JudgeAssignment.objects.get(
            judge=self._member("jdg_fm0"), submission=self._sub("prj_fm1"))
        assignment.judge = spare
        assignment.save(update_fields=["judge"])
        live = self._live()
        self.assertEqual(live["n_judges"], self.live0_n_judges + 1)   # live moved: 4 -> 5
        self.assertEqual(live["n_ballots"], self.live0_n_ballots)     # relabel, not a new ballot
        self._assert_frozen_unchanged()

    # --- mutation 4: change judge membership ---------------------------------------------------
    def test_04_change_judge_membership(self):
        """Change a judge membership's ext_id so two memberships collapse to ONE judge key in the
        LIVE composite (a deliberately visible membership mutation -- e.g. merging a duplicate judge
        account). The engine keys judges by membership ext_id, so n_judges falls 4 -> 3. The frozen
        run pinned the original judge identities in its `inputs`, so it does not move."""
        j0 = self._member("jdg_fm0")
        merged_ext_id = self._member("jdg_fm1").ext_id
        j0.ext_id = merged_ext_id
        j0.save(update_fields=["ext_id"])
        live = self._live()
        self.assertEqual(live["n_judges"], self.live0_n_judges - 1)   # live moved: 4 -> 3
        self.assertEqual(live["n_ballots"], self.live0_n_ballots)     # same ballots, fewer keys
        self._assert_frozen_unchanged()

    # --- mutation 5: change a RubricWeight -----------------------------------------------------
    def test_05_change_rubric_weight(self):
        """Reweight the rubric through the real organizer service. Because quality != functionality
        in every ballot, the weighted composite (hence live raw_mean and q) moves. The published run
        pinned the exact weights it consumed, so the frozen result does not move."""
        set_rubric_weights(self.org_user, self.event,
                           weights={"functionality": 5.0, "quality": 1.0, "innovation": 1.0})
        self.assertEqual(current_weights(self.event)["functionality"], 5.0)   # config did change
        live_rawmean = {r["submission"]: r["raw_mean"] for r in self._live()["rows"]}
        self.assertNotEqual(live_rawmean, self.live0_rawmean)                  # live moved
        self.assertNotEqual(live_rawmean["prj_fm1"], self.live0_rawmean["prj_fm1"])
        self._assert_frozen_unchanged()

    # --- mutation 6: withdraw a scored submission ----------------------------------------------
    def test_06_withdraw_scored_submission(self):
        """Withdrawal is a SOFT, gallery-only state change: it removes the project from the public
        gallery but is NOT a scoring input (services.observed filters ballots by event, never by
        submission.state). So the frozen result still contains the submission with the same rank/q,
        AND the live leaderboard still scores it with the same ballot count -- neither scoring view
        moves. This documents that withdrawal touches visibility, not the ranking."""
        sub = self._sub("prj_fm3")
        sub.state = Submission.WITHDRAWN
        sub.save(update_fields=["state"])
        self.assertEqual(self._sub("prj_fm3").state, Submission.WITHDRAWN)     # state did change
        live = self._live()
        self.assertIn("prj_fm3", {r["submission"] for r in live["rows"]})      # live still scores it
        self.assertEqual(live["n_ballots"], self.live0_n_ballots)              # its ballots still count
        # frozen still contains it at its original rank/q (checked in full by the helper)
        self.assertIn("prj_fm3", self.frozen_rank_q)
        self._assert_frozen_unchanged()
    # --- mutation 7: repoint the mutable current-revision pointer ------------------------------
    def test_07_repoint_current_revision_pointer(self):
        """The 'current revision' is not a stored FK the frozen result could follow: readers use the
        Ballot row's DENORMALIZED current-score fields (functionality/quality/innovation), and the
        history's current version is derived as MAX(BallotRevision.version). The Ballot row is the one
        mutable 'current-score pointer'. Here we repoint it DIRECTLY with a queryset .update() --
        bypassing record_ballot, so NO new revision is appended (an out-of-band overwrite of the
        current pointer). The LIVE leaderboard, which reads that row, moves; the frozen published run,
        which stored its own snapshot, does not."""
        j0, sub = self._member("jdg_fm0"), self._sub("prj_fm1")
        ballot = Ballot.objects.get(assignment__judge=j0, assignment__submission=sub)
        revisions_before = BallotRevision.objects.filter(ballot=ballot).count()
        Ballot.objects.filter(pk=ballot.pk).update(functionality=1, quality=1, innovation=1)
        # pointer repointed with no appended history row (bypassed the append-only writer)
        self.assertEqual(BallotRevision.objects.filter(ballot=ballot).count(), revisions_before)
        live_rawmean = {r["submission"]: r["raw_mean"] for r in self._live()["rows"]}
        self.assertNotEqual(live_rawmean["prj_fm1"], self.live0_rawmean["prj_fm1"])   # live moved
        self._assert_frozen_unchanged()

    # --- supersession: the ONLY thing that changes current_results -----------------------------
    def test_08_supersession_only_a_new_publish_changes_current_results(self):
        """Building + publishing a NEW run is the sole way current_results changes, and it does so by
        APPEND (v2), never by mutating v1. Documents the ACTUAL, asserted behavior: publication is
        SUPERSEDABLE (current = highest version) while each published run is IMMUTABLE (append-only)."""
        # A live-input mutation alone never supersedes: current_results stays v1.
        record_ballot(self._member("jdg_fmspare"), self._sub("prj_fm1"),
                      functionality=3, quality=3, innovation=3)
        mid = results.current_results(self.event)
        self.assertEqual(mid["version"], 1)
        self.assertEqual(mid["result_hash"], self.frozen_hash)

        # Publishing again is what moves the official pointer -- to a new, higher version.
        pub2, run2 = results.publish_results(
            self.event, key=signing.generate_private_key(), note="Superseding.",
            n_boot=N_BOOT, seed=0)
        cur = results.current_results(self.event)
        self.assertEqual(cur["version"], 2)                             # supersede-by-append
        self.assertEqual(cur["run_ext_id"], run2.run_ext_id)
        self.assertNotEqual(cur["result_hash"], self.frozen_hash)       # new inputs -> new signed result
        self.assertEqual(cur["result"]["n_ballots"], self.frozen_n_ballots + 1)

        # The ORIGINAL v1 run row is immutable: still present and byte-identical after v2 exists.
        v1 = NormalizationRun.objects.get(run_ext_id=self.v1_run_ext_id)
        self.assertEqual(v1.result_hash, self.v1_result_hash)
        self.assertEqual(v1.result, self.frozen_result)

        # Publication history is append-only -- both versions retained, newest first.
        self.assertEqual([p.version for p in results.publication_history(self.event)], [2, 1])

