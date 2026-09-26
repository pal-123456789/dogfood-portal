# src/events/management/commands/dogfood_import.py
"""F3 importer. Seeds the portal from the fixture OBJECT
`{event, tracks, judges, teams, projects, scores}` (NOT a top-level array — the F1 stub's
array assumption is replaced here, and ONLY here: the arg signature and entrypoint call are
unchanged). Idempotent: keyed on natural ids (ext_id / email / token), wrapped in one
transaction, and `--if-empty` short-circuits when an event already exists, so the acceptance
suite running twice cannot double-seed. Prints the four demo Cookie headers on success.
"""
import json
import os

from django.contrib.auth import get_user_model
from django.core.management.base import BaseCommand, CommandError
from django.db import transaction
from django.utils.dateparse import parse_datetime

from accounts.models import DemoSession
from events.models import (BootstrapState, Event, EventMembership, Team,
                           TeamMember, Track)
from judging.models import Ballot, JudgeAssignment, RubricWeight
from submissions.models import Submission

User = get_user_model()

SEED_VERSION = "demo-v1"
CRITERIA = ("functionality", "quality", "innovation")
ORG_DEMO_EMAIL = "organizer@dogfood.demo"

# Runbook §4b identity matrix: token -> (kind, selector, label). organizer-demo is
# synthesized because the fixture ships no organizer; judges resolve by ext_id, the
# participant by fixture email.
DEMO_TOKENS = [
    ("org_7f2a",    "email",  ORG_DEMO_EMAIL,       "organizer-demo"),
    ("jdg_a_91bc",  "judge",  "jdg_01",             "judge_a"),
    ("jdg_b_44de",  "judge",  "jdg_02",             "judge_b"),
    ("prt_2e88",    "email",  "priya1@example.org", "participant-a"),
]


class Command(BaseCommand):
    help = "Seed the portal from a fixture object. F3: real, idempotent importer."

    def add_arguments(self, parser):
        # Signature frozen at F1 so entrypoint step 6 never needs editing.
        parser.add_argument("path")
        parser.add_argument("--if-empty", action="store_true",
                            help="seed only when the portal has no events yet")
        parser.add_argument("--dry-run", action="store_true",
                            help="report what would be imported; never exit non-zero")

    def handle(self, *args, **opts):
        path = opts["path"]
        if not os.path.isfile(path):
            self.stdout.write("import = no fixture at %s (nothing seeded)" % path)
            return
        with open(path, encoding="utf-8") as fh:
            data = json.load(fh)
        if not isinstance(data, dict):
            self.stdout.write("import = FIXTURE IS NOT A JSON OBJECT (%s)" % path)
            raise CommandError(
                "%s must hold a JSON object "
                "{event, tracks, judges, teams, projects, scores}" % path)

        if opts["dry_run"]:
            self._report(data)
            return

        if opts["if_empty"] and Event.objects.exists():
            self.stdout.write("import = skipped (--if-empty; an event already exists)")
            return

        with transaction.atomic():
            counts = self._seed(data)
        for line in counts:
            self.stdout.write(line)

    # -- seeding ------------------------------------------------------------
    def _seed(self, data):
        ev = data["event"]
        event, _ = Event.objects.get_or_create(
            ext_id=ev["id"],
            defaults=dict(
                name=ev["name"],
                # CLOSED on purpose: submissions_close is in the past AND state != OPEN,
                # so accepting_submissions() is False -> check 3's late POST gets 4xx.
                state=Event.CLOSED,
                submissions_close=parse_datetime(ev["submissions_close"]),
                results_published=True,
            ),
        )

        track_by_ext = {}
        for t in data["tracks"]:
            obj, _ = Track.objects.get_or_create(
                ext_id=t["id"], defaults=dict(event=event, name=t["name"]))
            track_by_ext[t["id"]] = obj

        judge_membership_by_ext = {}
        for j in data["judges"]:
            u = self._ensure_user(j["email"], display_name=j.get("name", ""))
            m, _ = EventMembership.objects.get_or_create(
                user=u, event=event, role=EventMembership.JUDGE,
                defaults=dict(ext_id=j["id"]))
            if m.ext_id != j["id"]:
                m.ext_id = j["id"]
                m.save(update_fields=["ext_id"])
            judge_membership_by_ext[j["id"]] = m

        team_by_ext = {}
        for t in data["teams"]:
            team, _ = Team.objects.get_or_create(
                ext_id=t["id"], defaults=dict(event=event, name=t["name"]))
            team_by_ext[t["id"]] = team
            for email in t["members"]:
                u = self._ensure_user(email)
                TeamMember.objects.get_or_create(team=team, user=u)
                EventMembership.objects.get_or_create(
                    user=u, event=event, role=EventMembership.PARTICIPANT)

        submission_by_ext = {}
        for p in data["projects"]:            # fixture order -> ascending id -> gallery order
            sub, _ = Submission.objects.get_or_create(
                ext_id=p["id"],
                defaults=dict(
                    event=event,
                    team=team_by_ext[p["team"]],
                    track=track_by_ext[p["track"]],
                    title=p["title"],
                    summary=p.get("summary", ""),
                    repo_url=p.get("repo_url", ""),
                    state=Submission.SUBMITTED,
                    submitted_at=parse_datetime(p["submitted_at"]) if p.get("submitted_at") else None,
                ),
            )
            submission_by_ext[p["id"]] = sub

        for s in data["scores"]:
            assignment, _ = JudgeAssignment.objects.get_or_create(
                judge=judge_membership_by_ext[s["judge"]],
                submission=submission_by_ext[s["project"]])
            c = s["criteria"]
            Ballot.objects.get_or_create(
                assignment=assignment,
                defaults=dict(
                    functionality=c["functionality"],
                    quality=c["quality"],
                    innovation=c["innovation"],
                    comment=s.get("comment", ""),
                ),
            )

        for crit in CRITERIA:                 # equal weights; normalizer reads these, never hard-codes
            RubricWeight.objects.get_or_create(
                event=event, criterion=crit, defaults=dict(weight=1.0))

        org_user = self._ensure_user(ORG_DEMO_EMAIL, display_name="Organizer (demo)")
        EventMembership.objects.get_or_create(
            user=org_user, event=event, role=EventMembership.ORGANIZER)

        self._resolve_demo_sessions(judge_membership_by_ext)
        BootstrapState.objects.get_or_create(
            key=SEED_VERSION, defaults=dict(version=SEED_VERSION))

        return self._summary(event)

    # -- helpers ------------------------------------------------------------
    def _ensure_user(self, email, display_name=""):
        email = User.objects.normalize_email(email).strip()
        u = User.objects.filter(email__iexact=email).first()   # honor Lower(email) ci-unique
        if u:
            if display_name and not u.display_name:
                u.display_name = display_name
                u.save(update_fields=["display_name"])
            return u
        return User.objects.create_user(email=email, password=None, display_name=display_name)

    def _resolve_demo_sessions(self, judge_membership_by_ext):
        for token, kind, selector, label in DEMO_TOKENS:
            if kind == "judge":
                user = judge_membership_by_ext[selector].user
            else:
                user = User.objects.get(email__iexact=selector)
            DemoSession.objects.update_or_create(
                token=token, defaults=dict(user=user, label=label))

    def _summary(self, event):
        lines = [
            "import = ok (event %s, state=%s)" % (event.ext_id, event.state),
            "  tracks    = %d" % Track.objects.filter(event=event).count(),
            "  judges    = %d" % EventMembership.objects.filter(
                event=event, role=EventMembership.JUDGE).count(),
            "  teams     = %d" % Team.objects.filter(event=event).count(),
            "  projects  = %d" % Submission.objects.filter(event=event).count(),
            "  ballots   = %d" % Ballot.objects.filter(
                assignment__submission__event=event).count(),
            "demo auth headers (attach verbatim as a request Cookie):",
        ]
        for token, _kind, _sel, label in DEMO_TOKENS:
            lines.append("  %-14s Cookie: session=%s" % (label, token))
        return lines

    def _report(self, data):
        self.stdout.write("import DRY-RUN (JSON object shape):")
        self.stdout.write("  event     = %s" % data.get("event", {}).get("id"))
        for k in ("tracks", "judges", "teams", "projects", "scores"):
            self.stdout.write("  %-9s = %d" % (k, len(data.get(k, []))))
