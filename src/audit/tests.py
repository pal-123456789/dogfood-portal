# src/audit/tests.py
"""DB-backed tests for the tamper-evident audit chain (needs Postgres: select_for_update + jsonb).

Run by `manage.py test audit`. Complements the DB-free suites in tests/ (hashchain/receipts/verify):
here we prove the ORM append is atomic with the business write, that the two audited runtime
mutations emit exactly one chained row each, and that an exported bundle survives the offline
verifier while any tamper is caught.
"""
import io
import json
import tempfile
from datetime import timedelta
from pathlib import Path
from unittest import mock

from django.contrib.auth import get_user_model
from django.core.management import call_command
from django.core.management.base import CommandError
from django.test import TestCase
from django.utils import timezone

from audit import service, verify
from audit.hashchain import verify_chain
from audit.models import AuditEvent
from events.models import Event, EventMembership, Team, TeamMember, Track
from judging import services as judging_services
from judging.models import Ballot, JudgeAssignment
from submissions import services as sub_services
from submissions.models import Submission

User = get_user_model()


class AuditChainTests(TestCase):
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

    def test_append_builds_a_valid_contiguous_chain(self):
        for i in range(1, 4):
            service.record_event(event_type="t.evt", object_type="t", object_id="o%d" % i,
                                 payload={"i": i})
        rows = service.chain_rows()
        self.assertEqual([r["seq"] for r in rows], [1, 2, 3])
        self.assertEqual(rows[0]["prev_hash"], "0" * 64)
        head = service.current_head()
        self.assertEqual(head.seq, 3)
        self.assertEqual(head.row_hash, rows[-1]["row_hash"])
        self.assertEqual(verify_chain(rows), (True, None, "ok"))

    def test_record_ballot_writes_ballot_and_audit_in_one_chain(self):
        mem, sub = self._judgeable()
        judging_services.record_ballot(mem, sub, functionality=5, quality=4, innovation=3)
        self.assertEqual(Ballot.objects.count(), 1)
        ev = AuditEvent.objects.get(event_type="ballot.recorded")
        self.assertEqual(ev.payload["submission"], "prj_t")
        self.assertEqual(ev.payload["functionality"], 5)
        self.assertEqual(ev.actor_membership_id, "jdg_t")
        self.assertTrue(verify_chain(service.chain_rows())[0])

    def test_business_write_rolls_back_when_audit_append_raises(self):
        mem, sub = self._judgeable()
        # AuditEvent row is created, THEN head.save raises -> the whole nested atomic unwinds,
        # so neither the audit row nor the ballot (nor the assignment) survives.
        with mock.patch("audit.models.AuditHead.save", side_effect=RuntimeError("boom")):
            with self.assertRaises(RuntimeError):
                judging_services.record_ballot(mem, sub, functionality=5, quality=4, innovation=3)
        self.assertEqual(Ballot.objects.count(), 0)
        self.assertEqual(JudgeAssignment.objects.count(), 0)
        self.assertEqual(AuditEvent.objects.count(), 0)
        self.assertEqual(service.current_head().seq, 0)

    def test_create_submission_is_audited_and_a_late_post_is_not(self):
        u = User.objects.create_user(email="p@x.com", password=None)
        ev = Event.objects.create(ext_id="evt_o", name="O", state=Event.OPEN,
                                  submissions_close=timezone.now() + timedelta(days=1))
        trk = Track.objects.create(ext_id="trk_o", event=ev, name="T")
        tm = Team.objects.create(ext_id="tm_o", event=ev, name="T")
        TeamMember.objects.create(team=tm, user=u)
        EventMembership.objects.create(user=u, event=ev, role=EventMembership.PARTICIPANT)
        sub = sub_services.create_submission(u, ev, team=tm, track=trk, title="Hi")
        self.assertTrue(AuditEvent.objects.filter(
            event_type="submission.created", object_id=sub.ext_id).exists())
        before = AuditEvent.objects.count()
        ev.state = Event.CLOSED
        ev.submissions_close = timezone.now() - timedelta(days=1)
        ev.save()
        with self.assertRaises(sub_services.SubmissionsClosed):
            sub_services.create_submission(u, ev, team=tm, track=trk, title="Late")
        self.assertEqual(AuditEvent.objects.count(), before)  # rejected write left no trail

    def test_export_bundle_verifies_offline_and_detects_tamper(self):
        for i in range(1, 3):
            service.record_event(event_type="t", object_type="t", object_id="o%d" % i,
                                 payload={"i": i})
        d = Path(tempfile.mkdtemp(prefix="auditbundle_"))
        call_command("audit_export", str(d), key=str(d / "key.pem"), stdout=io.StringIO())
        ok, checks = verify.verify_bundle(d)
        self.assertTrue(ok, checks)
        p = d / "audit-prefix.jsonl"
        lines = p.read_text(encoding="utf-8").splitlines()
        row = json.loads(lines[0]); row["payload"] = {"i": 999}
        lines[0] = json.dumps(row)
        p.write_text("\n".join(lines) + "\n", encoding="utf-8")
        self.assertFalse(verify.verify_bundle(d)[0])

    def test_audit_verify_command_detects_in_db_tamper(self):
        service.record_event(event_type="t", object_type="t", object_id="o", payload={"a": 1})
        call_command("audit_verify", stdout=io.StringIO())          # clean chain: no raise
        AuditEvent.objects.filter(seq=1).update(payload={"a": 2})    # edit a stored row in place
        with self.assertRaises(CommandError):
            call_command("audit_verify", stdout=io.StringIO())
