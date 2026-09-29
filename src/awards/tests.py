# src/awards/tests.py
"""DB-backed tests for prizes/awards (run: python src/manage.py test awards).

They pin the behaviour that matters:
  * an organizer can create a prize and assign a winner; a cross-event submission is refused;
  * the PUBLIC podium is hidden (neutral state, no ranking) until results are published, then shows
    the top-N derived from the frozen signed result -- with a lower-ranked project truncated out;
  * a podium-targeting prize with no explicit winner resolves its winner live from that frozen
    podium, while an assigned winner overrides it;
  * organizer routes reject anonymous (login redirect) and authenticated non-organizers (403);
  * no per-judge score, judge identity, or PII appears on the public page.

ROOT_URLCONF is overridden to this module (which includes awards.urls) so the routes resolve even
before the project include wires them. CACHES is LocMemCache (as in api/tests) so nothing touches
the absent test cache table.
"""
from datetime import timedelta

from django.test import TestCase, override_settings
from django.urls import include, path, reverse
from django.utils import timezone

from accounts.models import AppUser
from audit import receipts
from audit.models import AuditEvent
from events.models import Event, EventMembership, Team, Track
from judging.models import Ballot, JudgeAssignment, RubricWeight
from normalize import engine, results
from submissions.models import Submission

from awards import services
from awards.models import Prize

_LOCMEM = {"default": {"BACKEND": "django.core.cache.backends.locmem.LocMemCache",
                       "LOCATION": "awards-tests"}}

# A self-contained urlconf so the awards routes resolve independently of the project include.
urlpatterns = [path("", include("awards.urls"))]

_SENSITIVE = ("@t.demo", "functionality", "quality", "innovation", "jdg_", "Org", "J0", "J1", "J2")


def _seed_event(prefix, *, publish):
    """A CLOSED single-track event with four SUBMITTED projects (raw means a>b>c>d) scored by three
    equal-weight judges (biases 0/+1/-1), an organizer, and one judge kept aside as a non-organizer.
    Optionally publishes official signed results. Returns a dict of the useful objects."""
    ev = Event.objects.create(ext_id="evt_%s" % prefix, name=prefix, state=Event.CLOSED,
                              submissions_close=timezone.now() - timedelta(days=1))
    trk = Track.objects.create(ext_id="trk_%s" % prefix, event=ev, name="Track %s" % prefix)
    tm = Team.objects.create(ext_id="tm_%s" % prefix, event=ev, name="Team %s" % prefix)
    for c in engine.CRITERIA:
        RubricWeight.objects.create(event=ev, criterion=c, weight=1.0)
    base = {"a": 4, "b": 3, "c": 2, "d": 1}
    subs = {}
    for sid in ("a", "b", "c", "d"):
        subs[sid] = Submission.objects.create(
            ext_id="prj_%s_%s" % (prefix, sid), event=ev, team=tm, track=trk,
            title="Title %s %s" % (prefix, sid), state=Submission.SUBMITTED)
    judges = []
    for n, bias in enumerate((0, 1, -1)):
        u = AppUser.objects.create_user(email="%s_judge%d@t.demo" % (prefix, n),
                                        display_name="%sJ%d" % (prefix, n))
        m = EventMembership.objects.create(user=u, event=ev, role=EventMembership.JUDGE,
                                           ext_id="jdg_%s%d" % (prefix, n))
        judges.append(u)
        for sid, sub in subs.items():
            asg = JudgeAssignment.objects.create(judge=m, submission=sub)
            v = base[sid] + bias
            Ballot.objects.create(assignment=asg, functionality=v, quality=v, innovation=v)
    org = AppUser.objects.create_user(email="%s_org@t.demo" % prefix, display_name="%sOrg" % prefix)
    EventMembership.objects.create(user=org, event=ev, role=EventMembership.ORGANIZER)
    if publish:
        results.publish_results(ev, key=receipts.generate_private_key(), note="Final.",
                                n_boot=120, seed=0)
    return {"event": ev, "track": trk, "team": tm, "subs": subs, "org": org, "judge": judges[0]}

@override_settings(CACHES=_LOCMEM, ROOT_URLCONF=__name__)
class AwardsTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.pub = _seed_event("aw", publish=True)
        cls.np = _seed_event("np", publish=False)

    # --- service layer ---------------------------------------------------------------------
    def test_create_prize_mints_ext_id_and_audits(self):
        ev = self.pub["event"]
        prize = services.create_prize(ev, name="Grand Prize", position=1, actor=self.pub["org"])
        self.assertTrue(prize.ext_id.startswith("prz_"))
        self.assertEqual(prize.event_id, ev.id)
        self.assertTrue(AuditEvent.objects.filter(
            event_type="prize.created", object_id=prize.ext_id).exists())

    def test_assign_winner_and_cross_event_refused(self):
        ev = self.pub["event"]
        prize = services.create_prize(ev, name="Best Overall", position=1, actor=self.pub["org"])
        services.assign_winner(prize, self.pub["subs"]["a"], actor=self.pub["org"])
        prize.refresh_from_db()
        self.assertEqual(prize.awarded_submission_id, self.pub["subs"]["a"].id)
        self.assertTrue(AuditEvent.objects.filter(
            event_type="prize.winner_assigned", object_id=prize.ext_id).exists())
        with self.assertRaises(ValueError):
            services.assign_winner(prize, self.np["subs"]["a"], actor=self.pub["org"])

    def test_create_prize_validation(self):
        ev = self.pub["event"]
        for bad in (dict(name="  ", position=1), dict(name="x", position=-1),
                    dict(name="x", position="nope"),
                    dict(name="x", position=1, track=self.np["track"])):
            with self.assertRaises(ValueError):
                services.create_prize(ev, **bad)

    def test_clear_and_remove(self):
        ev = self.pub["event"]
        prize = services.create_prize(ev, name="Special", position=0, actor=self.pub["org"])
        services.assign_winner(prize, self.pub["subs"]["c"], actor=self.pub["org"])
        services.clear_winner(prize, actor=self.pub["org"])
        prize.refresh_from_db()
        self.assertIsNone(prize.awarded_submission_id)
        ext = prize.ext_id
        services.remove_prize(prize, actor=self.pub["org"])
        self.assertFalse(Prize.objects.filter(ext_id=ext).exists())
        self.assertTrue(AuditEvent.objects.filter(
            event_type="prize.removed", object_id=ext).exists())

    def test_public_awards_unpublished_is_neutral(self):
        self.assertEqual(services.public_awards(self.np["event"]), {"published": False})

    def test_public_awards_derives_from_frozen_result(self):
        ev = self.pub["event"]
        services.create_prize(ev, name="Champion", position=1, actor=self.pub["org"])
        data = services.public_awards(ev)
        self.assertTrue(data["published"])
        self.assertEqual([r["place"] for r in data["podium"]], [1, 2, 3])
        shown = {r["submission"] for r in data["podium"]}
        self.assertNotIn(self.pub["subs"]["d"].ext_id, shown)      # 4th project truncated
        self.assertEqual(data["podium"][0]["submission"], self.pub["subs"]["a"].ext_id)
        champ = data["prizes"][0]
        self.assertEqual(champ["winner"]["submission"], data["podium"][0]["submission"])
        self.assertEqual(champ["winner_source"], "derived")

    def test_assigned_winner_overrides_derivation(self):
        ev = self.pub["event"]
        prize = services.create_prize(ev, name="Runner Pick", position=1, actor=self.pub["org"])
        services.assign_winner(prize, self.pub["subs"]["c"], actor=self.pub["org"])
        pv = services.public_awards(ev)["prizes"][0]
        self.assertEqual(pv["winner"]["submission"], self.pub["subs"]["c"].ext_id)
        self.assertEqual(pv["winner_source"], "assigned")

    # --- HTTP surface ----------------------------------------------------------------------
    def test_public_podium_page_hidden_until_published(self):
        resp = self.client.get(reverse("awards:podium", args=[self.np["event"].ext_id]))
        self.assertEqual(resp.status_code, 200)
        self.assertFalse(resp.context["published"])
        self.assertIn("not yet published", resp.content.decode("utf-8"))

    def test_public_podium_page_shows_topN_and_no_pii(self):
        ev = self.pub["event"]
        services.create_prize(ev, name="Champion", position=1, actor=self.pub["org"])
        resp = self.client.get(reverse("awards:podium", args=[ev.ext_id]))
        self.assertEqual(resp.status_code, 200)
        self.assertTrue(resp.context["published"])
        self.assertEqual(len(resp.context["podium"]), 3)
        text = resp.content.decode("utf-8")
        self.assertIn(self.pub["subs"]["a"].ext_id, text)          # top project shown
        self.assertNotIn(self.pub["subs"]["d"].ext_id, text)       # 4th project truncated
        for token in _SENSITIVE:
            self.assertNotIn(token, text, "leaked sensitive token %r" % token)

    def test_manage_requires_organizer(self):
        url = reverse("awards:manage", args=[self.pub["event"].ext_id])
        self.assertEqual(self.client.get(url).status_code, 302)     # anonymous -> login redirect
        self.client.force_login(self.pub["judge"])                  # authenticated non-organizer
        self.assertEqual(self.client.get(url).status_code, 403)
        self.client.force_login(self.pub["org"])
        self.assertEqual(self.client.get(url).status_code, 200)

    def test_create_prize_via_view(self):
        ev = self.pub["event"]
        self.client.force_login(self.pub["org"])
        resp = self.client.post(reverse("awards:manage", args=[ev.ext_id]),
                                {"name": "Best Rookie", "position": "2", "track": ""})
        self.assertEqual(resp.status_code, 302)
        self.assertTrue(Prize.objects.filter(event=ev, name="Best Rookie", position=2).exists())

    def test_create_via_view_rejects_blank_name(self):
        ev = self.pub["event"]
        self.client.force_login(self.pub["org"])
        resp = self.client.post(reverse("awards:manage", args=[ev.ext_id]),
                                {"name": "   ", "position": "1"})
        self.assertEqual(resp.status_code, 400)
        self.assertFalse(Prize.objects.filter(event=ev).exists())

    def test_assign_and_clear_via_view(self):
        ev = self.pub["event"]
        prize = services.create_prize(ev, name="Judges' Pick", position=0, actor=self.pub["org"])
        self.client.force_login(self.pub["org"])
        resp = self.client.post(reverse("awards:assign", args=[ev.ext_id, prize.ext_id]),
                                {"submission": self.pub["subs"]["b"].ext_id})
        self.assertEqual(resp.status_code, 302)
        prize.refresh_from_db()
        self.assertEqual(prize.awarded_submission_id, self.pub["subs"]["b"].id)
        self.assertEqual(
            self.client.post(reverse("awards:clear", args=[ev.ext_id, prize.ext_id])).status_code,
            302)
        prize.refresh_from_db()
        self.assertIsNone(prize.awarded_submission_id)

    def test_non_organizer_cannot_post_actions(self):
        ev = self.pub["event"]
        prize = services.create_prize(ev, name="Locked", position=1, actor=self.pub["org"])
        self.client.force_login(self.pub["judge"])
        resp = self.client.post(reverse("awards:assign", args=[ev.ext_id, prize.ext_id]),
                                {"submission": self.pub["subs"]["a"].ext_id})
        self.assertEqual(resp.status_code, 403)
        prize.refresh_from_db()
        self.assertIsNone(prize.awarded_submission_id)


@override_settings(CACHES=_LOCMEM, ROOT_URLCONF=__name__)
class AwardsTopupViewTests(TestCase):
    """The organizer-only review top-up planner view. It reads the LIVE (unsigned) standings, so it
    needs no published result; it must stay organizer-gated, mark itself uncacheable, and leak no
    per-judge score, judge identity, or PII."""
    @classmethod
    def setUpTestData(cls):
        cls.d = _seed_event("tp", publish=False)
        services.create_prize(cls.d["event"], name="Grand", position=1, actor=cls.d["org"])

    def _url(self):
        return reverse("awards:topup", args=[self.d["event"].ext_id])

    def test_organizer_sees_plan_private_and_no_pii(self):
        self.client.force_login(self.d["org"])
        resp = self.client.get(self._url())
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp["Cache-Control"], "private, no-store")   # per-organizer, never cached
        self.assertIn("Cookie", resp["Vary"])
        self.assertEqual(resp.context["n_ballots"], 12)                # 4 projects x 3 judges
        self.assertEqual([ln["position"] for ln in resp.context["lines"]], [1])
        text = resp.content.decode("utf-8")
        self.assertIn("Grand", text)                                   # the prize line is rendered
        for token in _SENSITIVE:
            self.assertNotIn(token, text, "leaked sensitive token %r" % token)

    def test_anonymous_redirected_to_login(self):
        self.assertEqual(self.client.get(self._url()).status_code, 302)

    def test_non_organizer_forbidden(self):
        self.client.force_login(self.d["judge"])
        self.assertEqual(self.client.get(self._url()).status_code, 403)


