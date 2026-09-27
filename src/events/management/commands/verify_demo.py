# src/events/management/commands/verify_demo.py
"""verify_demo — assert the demo seed is not just present but COHERENT (runbook §7).

Three jobs in one command: the README "Verify readiness" step, the dev-loop guard after
every reset, and boot step 6.5 (fail loud + exit non-zero BEFORE a half-seeded portal
starts serving). Gated on DOGFOOD_DEMO: production ships no demo fixture, so it skips
clean instead of failing a real deployment's boot.
"""
from django.conf import settings
from django.core.management.base import BaseCommand, CommandError
from django.utils import timezone

from accounts.models import DemoSession
from events.models import BootstrapState, Event, EventMembership, Team, Track
from judging.models import Ballot, BallotRevision
from submissions.models import Submission

EXPECT = dict(tracks=8, judges=30, teams=40, projects=41, ballots=126, ballot_revisions=126)
DEMO_TOKENS = ("org_7f2a", "jdg_a_91bc", "jdg_b_44de", "prt_2e88")


class Command(BaseCommand):
    help = "Assert the demo seed matches the fixture invariants (runbook §7)."

    def handle(self, *args, **opts):
        if not getattr(settings, "DOGFOOD_DEMO", False):
            self.stdout.write("verify_demo = skipped (DOGFOOD_DEMO off; not a demo stack)")
            return

        fails = []

        def check(label, ok, detail=""):
            self.stdout.write("  %s %s%s" % (
                "PASS" if ok else "FAIL", label, "" if ok else "  <- " + detail))
            if not ok:
                fails.append(label)

        # 1) single event, CLOSED, deadline in the past
        event = Event.objects.filter(ext_id="evt_01").first()
        check("event evt_01 present", event is not None)
        if event is None:
            raise CommandError("verify_demo: no evt_01 -- seed did not run")
        check("exactly 1 event", Event.objects.count() == 1,
              "got %d" % Event.objects.count())
        check("evt_01 CLOSED", event.state == Event.CLOSED, "state=%s" % event.state)
        check("deadline in past", event.submissions_close < timezone.now(),
              "close=%s" % event.submissions_close)
        check("not accepting submissions",
              not event.accepting_submissions(timezone.now()))

        # 2) exact counts (runbook §7 / §4h fixture inventory)
        got = dict(
            tracks=Track.objects.filter(event=event).count(),
            judges=EventMembership.objects.filter(
                event=event, role=EventMembership.JUDGE).count(),
            teams=Team.objects.filter(event=event).count(),
            projects=Submission.objects.filter(event=event).count(),
            ballots=Ballot.objects.filter(assignment__submission__event=event).count(),
            ballot_revisions=BallotRevision.objects.filter(
                ballot__assignment__submission__event=event).count(),
        )
        for k, v in EXPECT.items():
            check("count %s == %d" % (k, v), got[k] == v, "got %d" % got[k])

        # every seeded ballot has exactly its append-only v1 (no orphan ballot, no stray
        # higher version at seed time) -- proves the revision history was wired, not faked.
        seed_revs = BallotRevision.objects.filter(
            ballot__assignment__submission__event=event)
        check("all seed revisions are v1",
              seed_revs.exclude(version=1).count() == 0,
              "non-v1=%d" % seed_revs.exclude(version=1).count())
        ballots_without_v1 = (Ballot.objects
                              .filter(assignment__submission__event=event)
                              .exclude(revisions__version=1).count())
        check("every ballot has a v1 revision", ballots_without_v1 == 0,
              "missing=%d" % ballots_without_v1)

        # 3) the four demo cookies resolve to the intended role (the #1 landmine)
        sess = {s.token: s.user for s in DemoSession.objects.select_related("user")}
        for tok in DEMO_TOKENS:
            check("cookie %s resolves" % tok, tok in sess, "missing DemoSession")

        def has_role(user, role):
            return user is not None and EventMembership.objects.filter(
                user=user, event=event, role=role).exists()

        org, ja, jb, pa = (sess.get(t) for t in DEMO_TOKENS)
        check("org_7f2a is organizer", has_role(org, EventMembership.ORGANIZER))
        check("jdg_a_91bc is judge", has_role(ja, EventMembership.JUDGE))
        check("jdg_b_44de is judge", has_role(jb, EventMembership.JUDGE))
        check("jdg_a maps to jdg_01",
              ja is not None and EventMembership.objects.filter(
                  user=ja, event=event, role=EventMembership.JUDGE,
                  ext_id="jdg_01").exists())
        check("participant-a is participant", has_role(pa, EventMembership.PARTICIPANT))
        check("participant-a is NOT judge/organizer",
              pa is not None and not has_role(pa, EventMembership.JUDGE)
              and not has_role(pa, EventMembership.ORGANIZER))
        a_subs = set(Ballot.objects.filter(assignment__judge__user=ja).values_list(
            "assignment__submission_id", flat=True)) if ja else set()
        b_subs = set(Ballot.objects.filter(assignment__judge__user=jb).values_list(
            "assignment__submission_id", flat=True)) if jb else set()
        check("judge_a/judge_b ownership distinguishable",
              bool(a_subs) and a_subs != b_subs)

        # 4) normalization guards (the fixture's deliberate hard cases)
        zv = Ballot.objects.filter(assignment__judge__ext_id="jdg_07")
        zv_vals = {(b.functionality, b.quality, b.innovation) for b in zv}
        check("jdg_07 zero-variance 3x(4,4,4)",
              zv.count() == 3 and zv_vals == {(4, 4, 4)},
              "n=%d vals=%s" % (zv.count(), zv_vals))
        dups = Submission.objects.filter(
            ext_id__in=["prj_07", "prj_41"]).select_related("team", "track")
        dup_ok = (dups.count() == 2
                  and {d.title for d in dups} == {"Dry Harbour"}
                  and {d.team.ext_id for d in dups} == {"tm_07"}
                  and {d.track.ext_id for d in dups} == {"trk_03"})
        check("planted duplicate prj_07/prj_41 present", dup_ok)
        comp = self._component_count(event)
        check("ballot graph is ONE component", comp == 1, "component_count=%d" % comp)

        # 5) idempotency marker
        check("BootstrapState('demo-v1') present",
              BootstrapState.objects.filter(key="demo-v1").exists())

        if fails:
            raise CommandError(
                "verify_demo FAILED (%d): %s" % (len(fails), ", ".join(fails)))
        self.stdout.write("verify_demo = OK (all §7 invariants hold)")

    def _component_count(self, event):
        """Connected components over the judge<->submission graph drawn by ballots."""
        parent = {}

        def find(x):
            parent.setdefault(x, x)
            root = x
            while parent[root] != root:
                root = parent[root]
            while parent[x] != root:
                parent[x], x = root, parent[x]
            return root

        def union(a, b):
            ra, rb = find(a), find(b)
            if ra != rb:
                parent[ra] = rb

        edges = Ballot.objects.filter(
            assignment__submission__event=event).values_list(
            "assignment__judge_id", "assignment__submission_id")
        for jid, sid in edges:
            union(("J", jid), ("S", sid))
        return len({find(n) for n in parent})
