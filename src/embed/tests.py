# src/embed/tests.py
"""DB-backed tests for the T4 embeddable gallery widget (run via `manage.py test`).

The orchestrator wires the two views at the top level (`/embed.js`, `/embed/<ext_id>`), so
these drive them by literal PATH -- exactly how an external site and its injected iframe reach
them. They assert the loader's Content-Type, that the widget page is frameable cross-origin
(no `X-Frame-Options: DENY`, `Content-Security-Policy: frame-ancestors *`) while a normal page
like /projects keeps the site-wide DENY, and that only SUBMITTED projects surface.

None of the five flat checker routes or base.html are exercised here; /embed.js and
/embed/<ext_id> live outside the checker contract.
"""
from datetime import timedelta

from django.test import TestCase
from django.utils import timezone

from events.models import Event, Team, Track
from submissions.models import Submission


class EmbedWidgetTests(TestCase):
    def setUp(self):
        self.event = Event.objects.create(
            ext_id="evt_embed", name="Embed Fest", state=Event.OPEN,
            submissions_close=timezone.now() + timedelta(days=1))
        self.track = Track.objects.create(
            ext_id="trk_embed", event=self.event, name="AI Track")
        self.team = Team.objects.create(
            ext_id="tm_embed", event=self.event, name="Falcons")
        self.submitted = Submission.objects.create(
            ext_id="prj_embed", event=self.event, team=self.team, track=self.track,
            title="Aurora Nightlight", summary="A calm ambient lamp.",
            state=Submission.SUBMITTED)
        # A withdrawn project must never surface in the public widget.
        self.withdrawn = Submission.objects.create(
            ext_id="prj_wd", event=self.event, team=self.team, track=self.track,
            title="Hidden Withdrawn Project", state=Submission.WITHDRAWN)

    # -- loader script -------------------------------------------------------
    def test_embed_js_ok_and_javascript_content_type(self):
        resp = self.client.get("/embed.js")
        self.assertEqual(resp.status_code, 200)
        self.assertTrue(resp["Content-Type"].startswith("application/javascript"))

    # -- widget page: frameable, SUBMITTED-only ------------------------------
    def test_embed_gallery_ok_frameable_and_lists_submitted(self):
        resp = self.client.get("/embed/%s" % self.event.ext_id)
        self.assertEqual(resp.status_code, 200)
        self.assertContains(resp, "Aurora Nightlight")          # the submitted project's title
        self.assertContains(resp, "AI Track")                   # track name
        self.assertContains(resp, "Falcons")                    # team name (allowed; not PII)
        self.assertNotContains(resp, "Hidden Withdrawn Project")  # withdrawn excluded
        # Frameable cross-origin: the site-wide DENY is dropped for THIS response...
        self.assertNotEqual(resp.get("X-Frame-Options"), "DENY")
        # ...and replaced by an explicit frame-ancestors policy.
        self.assertIn("frame-ancestors *", resp["Content-Security-Policy"])

    # -- global default untouched -------------------------------------------
    def test_normal_page_still_denies_framing(self):
        resp = self.client.get("/projects")
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp["X-Frame-Options"], "DENY")

    # -- unknown event -------------------------------------------------------
    def test_unknown_event_404(self):
        self.assertEqual(self.client.get("/embed/evt_missing").status_code, 404)
