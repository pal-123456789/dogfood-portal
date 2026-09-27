# src/events/tests.py
"""DB-backed tests for the organizer event/team/track UI (run via `manage.py test`).

These drive the real service+view+URL stack: auth gating (anon -> login, non-organizer -> 403),
atomic+audited creates, and the state hinge that actually opens submissions. The acceptance
checker never touches /events/, so these are pure feature tests -- none of the five flat routes
or base.html are exercised here.
"""
from django.contrib.auth import get_user_model
from django.test import TestCase
from django.utils import timezone

from audit.models import AuditEvent
from events.models import Event, EventMembership, Team, Track

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
