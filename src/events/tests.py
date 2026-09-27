# src/events/tests.py
"""DB-backed tests for the organizer event/team/track UI (run via `manage.py test`).

These drive the real service+view+URL stack: auth gating (anon -> login, non-organizer -> 403),
atomic+audited creates, and the state hinge that actually opens submissions. The acceptance
checker never touches /events/, so these are pure feature tests -- none of the five flat routes
or base.html are exercised here.
"""
from datetime import timedelta
from io import StringIO

from django.conf import settings
from django.contrib.admin.sites import AdminSite
from django.contrib.auth import get_user_model
from django.core.management import call_command
from django.core.management.base import CommandError
from django.test import RequestFactory, TestCase, override_settings
from django.utils import timezone

from audit.models import AuditEvent
from events.models import Event, EventMembership, Invite, Team, Track
from submissions.models import Submission

User = get_user_model()
FUTURE = "2099-12-31T23:59"          # naive datetime-local shape; the service makes it aware


class EventUITests(TestCase):
    def setUp(self):
        self.org = User.objects.create_user(
            email="org@example.org", password="pw", display_name="Org")
        self.other = User.objects.create_user(email="other@example.org", password="pw")

    def _make_event(self, name="E"):
        self.client.force_login(self.org)
        self.client.post("/events/new", {"name": name, "submissions_close": FUTURE})
        return Event.objects.get(name=name)

    # -- dashboard / auth ----------------------------------------------------
    def test_dashboard_anonymous_redirects_to_login(self):
        resp = self.client.get("/events/")
        self.assertEqual(resp.status_code, 302)
        self.assertTrue(resp.url.startswith("/accounts/login/"))

    def test_dashboard_authenticated_ok(self):
        self.client.force_login(self.org)
        resp = self.client.get("/events/")
        self.assertEqual(resp.status_code, 200)
        self.assertContains(resp, "New event")

    # -- create event --------------------------------------------------------
    def test_create_event_makes_creator_organizer_and_audits(self):
        self.client.force_login(self.org)
        before = AuditEvent.objects.count()
        resp = self.client.post(
            "/events/new", {"name": "Spring Hack", "submissions_close": FUTURE})
        event = Event.objects.get(name="Spring Hack")
        self.assertEqual(resp.status_code, 302)
        self.assertEqual(resp.url, "/events/%s?ok=created" % event.ext_id)
        self.assertEqual(event.state, Event.SETUP)
        self.assertTrue(event.ext_id.startswith("evt_"))
        self.assertTrue(EventMembership.objects.filter(
            user=self.org, event=event, role=EventMembership.ORGANIZER).exists())
        self.assertTrue(AuditEvent.objects.filter(
            event_type="event.created", object_id=event.ext_id).exists())
        self.assertEqual(AuditEvent.objects.count(), before + 1)

    def test_create_event_rejects_blank_name(self):
        self.client.force_login(self.org)
        resp = self.client.post("/events/new", {"name": "  ", "submissions_close": FUTURE})
        self.assertEqual(resp.status_code, 400)
        self.assertFalse(Event.objects.exists())

    def test_create_event_rejects_bad_date(self):
        self.client.force_login(self.org)
        resp = self.client.post("/events/new", {"name": "X", "submissions_close": "not-a-date"})
        self.assertEqual(resp.status_code, 400)
        self.assertFalse(Event.objects.exists())

    def test_create_event_anonymous_redirects_and_writes_nothing(self):
        resp = self.client.post("/events/new", {"name": "X", "submissions_close": FUTURE})
        self.assertEqual(resp.status_code, 302)
        self.assertFalse(Event.objects.exists())

    # -- detail auth ---------------------------------------------------------
    def test_detail_organizer_ok_nonorganizer_403_anon_login(self):
        event = self._make_event()
        self.assertEqual(self.client.get("/events/%s" % event.ext_id).status_code, 200)
        self.client.force_login(self.other)
        self.assertEqual(self.client.get("/events/%s" % event.ext_id).status_code, 403)
        self.client.logout()
        r = self.client.get("/events/%s" % event.ext_id)
        self.assertEqual(r.status_code, 302)
        self.assertTrue(r.url.startswith("/accounts/login/"))

    def test_detail_unknown_event_404(self):
        self.client.force_login(self.org)
        self.assertEqual(self.client.get("/events/evt_missing").status_code, 404)

    # -- tracks / teams ------------------------------------------------------
    def test_add_track_and_team_as_organizer(self):
        event = self._make_event()
        r1 = self.client.post("/events/%s/tracks/new" % event.ext_id, {"name": "AI"})
        self.assertEqual(r1.status_code, 302)
        self.assertTrue(Track.objects.filter(event=event, name="AI").exists())
        r2 = self.client.post("/events/%s/teams/new" % event.ext_id, {"name": "Falcons"})
        self.assertEqual(r2.status_code, 302)
        self.assertTrue(Team.objects.filter(event=event, name="Falcons").exists())
        self.assertTrue(AuditEvent.objects.filter(event_type="track.created").exists())
        self.assertTrue(AuditEvent.objects.filter(event_type="team.created").exists())

    def test_add_track_non_organizer_403_writes_nothing(self):
        event = self._make_event()
        self.client.force_login(self.other)
        resp = self.client.post("/events/%s/tracks/new" % event.ext_id, {"name": "AI"})
        self.assertEqual(resp.status_code, 403)
        self.assertFalse(Track.objects.filter(event=event).exists())

    def test_blank_track_name_400_writes_nothing(self):
        event = self._make_event()
        resp = self.client.post("/events/%s/tracks/new" % event.ext_id, {"name": "  "})
        self.assertEqual(resp.status_code, 400)
        self.assertFalse(Track.objects.filter(event=event).exists())

    # -- state hinge ---------------------------------------------------------
    def test_set_state_open_starts_accepting_submissions(self):
        event = self._make_event()
        self.assertFalse(event.accepting_submissions(timezone.now()))   # SETUP
        resp = self.client.post("/events/%s/state" % event.ext_id, {"state": Event.OPEN})
        self.assertEqual(resp.status_code, 302)
        event.refresh_from_db()
        self.assertEqual(event.state, Event.OPEN)
        self.assertTrue(event.accepting_submissions(timezone.now()))
        self.assertTrue(AuditEvent.objects.filter(event_type="event.state_changed").exists())

    def test_set_state_invalid_400_leaves_state(self):
        event = self._make_event()
        resp = self.client.post("/events/%s/state" % event.ext_id, {"state": "bogus"})
        self.assertEqual(resp.status_code, 400)
        event.refresh_from_db()
        self.assertEqual(event.state, Event.SETUP)

    def test_set_state_non_organizer_403(self):
        event = self._make_event()
        self.client.force_login(self.other)
        resp = self.client.post("/events/%s/state" % event.ext_id, {"state": Event.OPEN})
        self.assertEqual(resp.status_code, 403)
        event.refresh_from_db()
        self.assertEqual(event.state, Event.SETUP)

    def test_created_event_never_displaces_current_event(self):
        """The checker's _current_event() is the oldest by id; a UI-created event has a higher
        id, so seeding order (and thus the checker's target) is unaffected."""
        seed = Event.objects.create(
            ext_id="evt_seed", name="Seed", submissions_close=timezone.now())
        made = self._make_event(name="Later")
        self.assertLess(seed.id, made.id)
        self.assertEqual(Event.objects.order_by("id").first().ext_id, "evt_seed")


class ParentCascadeAdminDeleteGuardTests(TestCase):
    """Deleting a PARENT row in admin (Event / EventMembership / Team) is refused when its cascade
    would reach a SCORED assignment, so scored Ballot/BallotRevision history cannot be destroyed
    through the admin UI. Unscored parents stay deletable and the bulk delete action is dropped.
    This mirrors JudgeAssignmentAdmin's direct-row guard (THREAT-MODEL A7/A8) via the shared
    portal.admin_mixins.ScoredCascadeDeleteGuard. Raw-DB deletion is out of scope (A8 boundary).
    """

    def setUp(self):
        from judging import services
        # A scored branch: judge -> assignment -> Ballot beneath a team's submission in this event.
        self.event = Event.objects.create(
            ext_id="evt_g", name="G", state=Event.CLOSED,
            submissions_close=timezone.now() - timedelta(days=1))
        self.track = Track.objects.create(ext_id="trk_g", event=self.event, name="G")
        self.team = Team.objects.create(ext_id="tm_g", event=self.event, name="G")
        self.sub = Submission.objects.create(
            ext_id="prj_g", event=self.event, team=self.team, track=self.track,
            title="G", state=Submission.SUBMITTED)
        judge_user = User.objects.create_user(email="jg@example.org", password="pw")
        self.judge = EventMembership.objects.create(
            user=judge_user, event=self.event, role=EventMembership.JUDGE, ext_id="mem_jg")
        services.record_ballot(self.judge, self.sub, functionality=4, quality=4, innovation=4)

        # A clean branch: an event / membership / team with no ballot anywhere below.
        self.clean_event = Event.objects.create(
            ext_id="evt_clean", name="C", submissions_close=timezone.now())
        clean_user = User.objects.create_user(email="cu@example.org", password="pw")
        self.clean_mem = EventMembership.objects.create(
            user=clean_user, event=self.clean_event, role=EventMembership.JUDGE, ext_id="mem_c")
        self.clean_team = Team.objects.create(
            ext_id="tm_c", event=self.clean_event, name="C")

        su = User.objects.create_user(email="su@example.org", password="pw")
        su.is_staff = su.is_superuser = True
        su.save()
        self.req = RequestFactory().get("/admin/")
        self.req.user = su

    def test_event_admin_refuses_delete_when_scored_below(self):
        from events.admin import EventAdmin
        ma = EventAdmin(Event, AdminSite())
        self.assertFalse(ma.has_delete_permission(self.req, self.event))
        self.assertTrue(ma.has_delete_permission(self.req, self.clean_event))
        self.assertNotIn("delete_selected", ma.get_actions(self.req))

    def test_membership_admin_refuses_delete_of_scored_judge(self):
        from events.admin import EventMembershipAdmin
        ma = EventMembershipAdmin(EventMembership, AdminSite())
        self.assertFalse(ma.has_delete_permission(self.req, self.judge))
        self.assertTrue(ma.has_delete_permission(self.req, self.clean_mem))
        self.assertNotIn("delete_selected", ma.get_actions(self.req))

    def test_team_admin_refuses_delete_when_scored_submission_below(self):
        from events.admin import TeamAdmin
        ma = TeamAdmin(Team, AdminSite())
        self.assertFalse(ma.has_delete_permission(self.req, self.team))
        self.assertTrue(ma.has_delete_permission(self.req, self.clean_team))
        self.assertNotIn("delete_selected", ma.get_actions(self.req))


class InviteUITests(TestCase):
    """DB-backed feature tests for signed single-use invitations (§20): minting (organizer-gated,
    audited), the invitee redeem flow (confirm -> join), single-use enforcement, signature/expiry
    rejection, the per-user redeem rate limit, and the invite_verify operator command.

    Signing reuses the one /state Ed25519 key via audit.keys.ensure_private_key, so create_invite
    and redeem share a key path -- these assert BEHAVIOUR (round-trip, tamper rejection), while the
    golden fingerprint and wire format are pinned DB-free in tests/test_invite_signing.py.
    """

    def setUp(self):
        self.org = User.objects.create_user(
            email="org@example.org", password="pw", display_name="Org")
        self.guest = User.objects.create_user(email="guest@example.org", password="pw")
        self.client.force_login(self.org)
        self.client.post("/events/new", {"name": "E", "submissions_close": FUTURE})
        self.event = Event.objects.get(name="E")

    def _create_invite(self, role=EventMembership.JUDGE, expires_at=""):
        """POST the organizer create-invite form; returns the response (caller asserts on it)."""
        self.client.force_login(self.org)
        return self.client.post(
            "/events/%s/invites/new" % self.event.ext_id,
            {"role": role, "expires_at": expires_at})

    # -- minting -------------------------------------------------------------
    def test_organizer_creates_invite_signed_and_audited(self):
        before = AuditEvent.objects.count()
        resp = self._create_invite()
        self.assertEqual(resp.status_code, 302)
        self.assertEqual(resp.url, "/events/%s?ok=invite" % self.event.ext_id)
        inv = Invite.objects.get(event=self.event)
        self.assertTrue(inv.ext_id.startswith("inv_"))
        self.assertEqual(inv.role, EventMembership.JUDGE)
        self.assertEqual(len(inv.signature), 128)          # 64 raw bytes -> 128 hex chars
        self.assertIsNone(inv.redeemed_at)
        self.assertTrue(AuditEvent.objects.filter(
            event_type="invite.created", object_id=inv.ext_id).exists())
        self.assertEqual(AuditEvent.objects.count(), before + 1)

    def test_create_invite_non_organizer_403_writes_nothing(self):
        self.client.force_login(self.guest)
        resp = self.client.post(
            "/events/%s/invites/new" % self.event.ext_id, {"role": EventMembership.JUDGE})
        self.assertEqual(resp.status_code, 403)
        self.assertFalse(Invite.objects.exists())

    def test_create_invite_rejects_unknown_role(self):
        # Only judge/participant may be granted -- a shared link can never escalate to organizer.
        resp = self._create_invite(role=EventMembership.ORGANIZER)
        self.assertEqual(resp.status_code, 400)
        self.assertFalse(Invite.objects.exists())

    def test_detail_lists_the_invite_link(self):
        self._create_invite()
        inv = Invite.objects.get(event=self.event)
        resp = self.client.get("/events/%s" % self.event.ext_id)
        self.assertEqual(resp.status_code, 200)
        self.assertContains(resp, inv.ext_id)      # the shareable redeem path is shown...
        self.assertContains(resp, "?sig=")         # ...with its signature query string
        self.assertContains(resp, "active")        # and a not-yet-redeemed status

    # -- redeem: happy path + single use -------------------------------------
    def test_redeem_confirm_then_join_creates_membership_and_audits(self):
        self._create_invite()
        inv = Invite.objects.get(event=self.event)
        self.client.force_login(self.guest)
        get = self.client.get("/events/invite/%s?sig=%s" % (inv.ext_id, inv.signature))
        self.assertEqual(get.status_code, 200)
        self.assertContains(get, "Join event")
        before = AuditEvent.objects.count()
        resp = self.client.post("/events/invite/%s" % inv.ext_id, {"sig": inv.signature})
        self.assertEqual(resp.status_code, 302)
        self.assertEqual(resp.url, "/events/invite/%s?joined=1" % inv.ext_id)
        self.assertTrue(EventMembership.objects.filter(
            user=self.guest, event=self.event, role=EventMembership.JUDGE).exists())
        inv.refresh_from_db()
        self.assertIsNotNone(inv.redeemed_at)
        self.assertEqual(inv.redeemed_by_id, self.guest.pk)
        self.assertTrue(AuditEvent.objects.filter(
            event_type="invite.redeemed", object_id=inv.ext_id).exists())
        self.assertEqual(AuditEvent.objects.count(), before + 1)
        joined = self.client.get("/events/invite/%s?joined=1" % inv.ext_id)
        self.assertContains(joined, "joined")

    def test_second_redeem_is_rejected_single_use(self):
        self._create_invite()
        inv = Invite.objects.get(event=self.event)
        self.client.force_login(self.guest)
        first = self.client.post("/events/invite/%s" % inv.ext_id, {"sig": inv.signature})
        self.assertEqual(first.status_code, 302)
        # The same link a second time is refused (409) and grants no second membership.
        again = self.client.post("/events/invite/%s" % inv.ext_id, {"sig": inv.signature})
        self.assertEqual(again.status_code, 409)
        self.assertEqual(EventMembership.objects.filter(
            user=self.guest, event=self.event, role=EventMembership.JUDGE).count(), 1)

    # -- redeem: rejection paths ---------------------------------------------
    def test_redeem_with_bad_signature_400_writes_nothing(self):
        self._create_invite()
        inv = Invite.objects.get(event=self.event)
        self.client.force_login(self.guest)
        for bad in ("00" * 64, "not-hex", ""):     # wrong sig / malformed hex / empty
            resp = self.client.post("/events/invite/%s" % inv.ext_id, {"sig": bad})
            self.assertEqual(resp.status_code, 400)
        inv.refresh_from_db()
        self.assertIsNone(inv.redeemed_at)
        self.assertFalse(EventMembership.objects.filter(
            user=self.guest, event=self.event).exists())

    def test_redeem_expired_invite_is_refused(self):
        # parse_close accepts any parseable datetime; a past expiry then fails is_expired(now).
        self._create_invite(expires_at="2000-01-01T00:00")
        inv = Invite.objects.get(event=self.event)
        self.assertIsNotNone(inv.expires_at)
        self.client.force_login(self.guest)
        get = self.client.get("/events/invite/%s?sig=%s" % (inv.ext_id, inv.signature))
        self.assertContains(get, "expired")        # the confirm page shows the expired state
        post = self.client.post("/events/invite/%s" % inv.ext_id, {"sig": inv.signature})
        self.assertEqual(post.status_code, 400)     # signature is valid, but expiry rejects it
        self.assertFalse(EventMembership.objects.filter(
            user=self.guest, event=self.event).exists())

    # -- redeem: auth + not-found -------------------------------------------
    def test_redeem_anonymous_redirects_to_login(self):
        self._create_invite()
        inv = Invite.objects.get(event=self.event)
        self.client.logout()
        resp = self.client.get("/events/invite/%s?sig=%s" % (inv.ext_id, inv.signature))
        self.assertEqual(resp.status_code, 302)
        self.assertTrue(resp.url.startswith("/accounts/login/"))

    def test_redeem_unknown_invite_404(self):
        self.client.force_login(self.guest)
        self.assertEqual(self.client.get("/events/invite/inv_missing").status_code, 404)

    # -- rate limit ----------------------------------------------------------
    def test_redeem_is_rate_limited_per_user(self):
        # The limiter fails open when the cache is unavailable -- and the DB cache table is absent
        # under `manage.py test` -- so pin a LocMemCache and a 1/h ceiling. The second redeem (a
        # DIFFERENT link, same user) is then refused with 429 BEFORE the service runs.
        self._create_invite()
        self._create_invite()
        a, b = list(Invite.objects.filter(event=self.event).order_by("id"))
        self.client.force_login(self.guest)
        limits = dict(settings.DOGFOOD_RATE_LIMITS, invite_redeem="1/h")
        with override_settings(
                CACHES={"default": {"BACKEND": "django.core.cache.backends.locmem.LocMemCache"}},
                DOGFOOD_RATE_LIMITS=limits):
            first = self.client.post("/events/invite/%s" % a.ext_id, {"sig": a.signature})
            self.assertEqual(first.status_code, 302)
            second = self.client.post("/events/invite/%s" % b.ext_id, {"sig": b.signature})
        self.assertEqual(second.status_code, 429)
        self.assertIn("Retry-After", second)
        b.refresh_from_db()
        self.assertIsNone(b.redeemed_at)            # blocked before the service, so B is untouched

    # -- operator command ----------------------------------------------------
    def test_invite_verify_command_reports_verified_then_failed(self):
        self._create_invite()
        inv = Invite.objects.get(event=self.event)
        out = StringIO()
        call_command("invite_verify", inv.ext_id, stdout=out)
        self.assertIn("VERIFIED", out.getvalue())
        # Tamper the stored signature: the command must print FAILED and exit non-zero.
        inv.signature = "00" * 64
        inv.save(update_fields=["signature"])
        with self.assertRaises(CommandError):
            call_command("invite_verify", inv.ext_id, stdout=StringIO())
