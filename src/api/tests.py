# src/api/tests.py
"""DB-backed integration tests for the read-only public API (/api/v1/).

Run: python src/manage.py test api

They pin the properties that make the API safe to expose publicly:
  * two-event isolation -- a response for event A never contains event B's rows;
  * DRAFT and WITHDRAWN submissions are absent from the public list;
  * an unpublished event's results endpoint returns {"published": false} with no ranking rows,
    and a published event returns the FROZEN signed run;
  * no per-judge score, judge identity, or user PII appears in ANY payload.

CACHES is overridden to LocMemCache (as in accounts/events/submissions tests) because the test
DB has no `dogfood_cache` table; without it the API's anon throttle would touch a missing table.
"""
from datetime import timedelta

from django.test import TestCase, override_settings
from django.utils import timezone

from accounts.models import AppUser
from apitokens import services as token_services
from apitokens.models import ApiToken
from audit import receipts
from events.models import Event, EventMembership, Team, Track
from judging.models import Ballot, JudgeAssignment, RubricWeight
from normalize import engine, results
from submissions.models import Submission

_LOCMEM = {"default": {"BACKEND": "django.core.cache.backends.locmem.LocMemCache",
                       "LOCATION": "api-tests"}}

# Tokens that would betray a leak of per-judge scores, judge identities, or user PII. Note that
# the aggregate count key "n_ballots" is intentionally NOT here: it is a public count, not a
# ballot. A real leak would surface as a criterion key or a judge/email token below.
_SENSITIVE = ("@t.demo", "email", "display_name", "functionality", "quality", "innovation",
              "jdg_")


def _seed_event(prefix, *, publish):
    """Mirror of normalize.tests._seed_additive_event: a CLOSED 3x3 additive event (three
    equal-weight judges biased 0/+1/-1) plus an organizer, with a DRAFT and a WITHDRAWN
    submission added so the public-exclusion property can be checked. Optionally publishes
    official results via results.publish_results. Returns the Event."""
    ev = Event.objects.create(ext_id="evt_%s" % prefix, name=prefix, state=Event.CLOSED,
                              submissions_close=timezone.now() - timedelta(days=1))
    trk = Track.objects.create(ext_id="trk_%s" % prefix, event=ev, name=prefix)
    tm = Team.objects.create(ext_id="tm_%s" % prefix, event=ev, name="Team %s" % prefix)
    for c in engine.CRITERIA:
        RubricWeight.objects.create(event=ev, criterion=c, weight=1.0)
    subs = {}
    for sid in ("a", "b", "c"):
        subs[sid] = Submission.objects.create(
            ext_id="prj_%s_%s" % (prefix, sid), event=ev, team=tm, track=trk,
            title="Title %s %s" % (prefix, sid), state=Submission.SUBMITTED)
    Submission.objects.create(ext_id="prj_%s_draft" % prefix, event=ev, team=tm, track=trk,
                              title="Draft %s" % prefix, state=Submission.DRAFT)
    Submission.objects.create(ext_id="prj_%s_wd" % prefix, event=ev, team=tm, track=trk,
                              title="Withdrawn %s" % prefix, state=Submission.WITHDRAWN)
    base = {"a": 4, "b": 3, "c": 2}
    for n, bias in enumerate((0, 1, -1)):
        u = AppUser.objects.create_user(email="%s_judge%d@t.demo" % (prefix, n),
                                        display_name="%sJ%d" % (prefix, n))
        m = EventMembership.objects.create(user=u, event=ev, role=EventMembership.JUDGE,
                                           ext_id="jdg_%s%d" % (prefix, n))
        for sid, sub in subs.items():
            a = JudgeAssignment.objects.create(judge=m, submission=sub)
            v = base[sid] + bias
            Ballot.objects.create(assignment=a, functionality=v, quality=v, innovation=v)
    org = AppUser.objects.create_user(email="%s_org@t.demo" % prefix, display_name="%sOrg" % prefix)
    EventMembership.objects.create(user=org, event=ev, role=EventMembership.ORGANIZER)
    if publish:
        results.publish_results(ev, key=receipts.generate_private_key(),
                                note="Final.", n_boot=120, seed=0)
    return ev


_EVENT_KEYS = {"ext_id", "name", "state", "submissions_close", "results_published", "created_at"}


@override_settings(CACHES=_LOCMEM)
class PublicApiTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.event_a = _seed_event("aa", publish=True)
        cls.event_b = _seed_event("bb", publish=False)

    def _body(self, url):
        resp = self.client.get(url)
        self.assertEqual(resp.status_code, 200, url)
        return resp

    def _page(self, url):
        return self._body(url).json()["results"]

    def _assert_no_sensitive(self, resp):
        text = resp.content.decode("utf-8")
        for token in _SENSITIVE:
            self.assertNotIn(token, text, "leaked sensitive token %r" % token)

    def test_events_list_is_public_and_pii_free(self):
        resp = self._body("/api/v1/events/")
        rows = resp.json()["results"]
        self.assertEqual({r["ext_id"] for r in rows}, {"evt_aa", "evt_bb"})
        for r in rows:
            self.assertEqual(set(r), _EVENT_KEYS)      # no memberships / no PII keys
        self._assert_no_sensitive(resp)

    def test_event_detail_public_keys_only(self):
        resp = self._body("/api/v1/events/evt_aa/")
        self.assertEqual(set(resp.json()), _EVENT_KEYS)
        self.assertEqual(resp.json()["ext_id"], "evt_aa")

    def test_two_event_isolation_for_submissions(self):
        a_ids = {s["ext_id"] for s in self._page("/api/v1/events/evt_aa/submissions/")}
        b_ids = {s["ext_id"] for s in self._page("/api/v1/events/evt_bb/submissions/")}
        self.assertEqual(a_ids, {"prj_aa_a", "prj_aa_b", "prj_aa_c"})
        self.assertEqual(b_ids, {"prj_bb_a", "prj_bb_b", "prj_bb_c"})
        self.assertEqual(a_ids & b_ids, set())

    def test_draft_and_withdrawn_absent_from_public_list(self):
        subs = self._page("/api/v1/events/evt_aa/submissions/")
        ids = {s["ext_id"] for s in subs}
        self.assertNotIn("prj_aa_draft", ids)
        self.assertNotIn("prj_aa_wd", ids)
        self.assertTrue(all(s["state"] == Submission.SUBMITTED for s in subs))
        for s in subs:
            self.assertEqual(s["event"], "evt_aa")

    def test_tracks_and_teams_are_event_scoped(self):
        self.assertEqual({t["ext_id"] for t in self._page("/api/v1/events/evt_aa/tracks/")},
                         {"trk_aa"})
        self.assertEqual({t["ext_id"] for t in self._page("/api/v1/events/evt_bb/tracks/")},
                         {"trk_bb"})
        self.assertEqual({t["ext_id"] for t in self._page("/api/v1/events/evt_aa/teams/")},
                         {"tm_aa"})

    def test_unpublished_event_results_published_false_no_rows(self):
        resp = self._body("/api/v1/events/evt_bb/results/")
        data = resp.json()
        self.assertEqual(data, {"published": False})
        self.assertNotIn("result", data)
        self.assertNotIn("rows", resp.content.decode("utf-8"))

    def test_published_results_are_frozen_and_safe(self):
        resp = self._body("/api/v1/events/evt_aa/results/")
        data = resp.json()
        self.assertTrue(data["published"])
        self.assertEqual(data["event"], "evt_aa")
        self.assertTrue(data["result_hash"])
        self.assertGreaterEqual(len(data["result"]["rows"]), 1)
        self._assert_no_sensitive(resp)          # rows carry no judge id or per-criterion score

    def test_no_sensitive_tokens_across_all_endpoints(self):
        for url in ("/api/v1/events/", "/api/v1/events/evt_aa/",
                    "/api/v1/events/evt_aa/tracks/", "/api/v1/events/evt_aa/teams/",
                    "/api/v1/events/evt_aa/submissions/", "/api/v1/events/evt_aa/results/"):
            self._assert_no_sensitive(self._body(url))

    def test_unknown_event_is_404(self):
        self.assertEqual(
            self.client.get("/api/v1/events/evt_missing/submissions/").status_code, 404)
        self.assertEqual(
            self.client.get("/api/v1/events/evt_missing/results/").status_code, 404)


@override_settings(CACHES=_LOCMEM)
class MeEndpointAndBearerAuthTests(TestCase):
    """Pins the ONE authenticated endpoint (/api/v1/me/) and proves the global Bearer auth class
    leaves the public endpoints untouched: a valid token returns only the caller's own token
    metadata + memberships (no email/display_name); missing/malformed/unknown/revoked tokens are
    401; and a valid token does not change a public endpoint's bytes."""

    @classmethod
    def setUpTestData(cls):
        cls.event = Event.objects.create(ext_id="evt_me", name="me", state=Event.CLOSED,
                                         submissions_close=timezone.now() - timedelta(days=1))
        cls.user = AppUser.objects.create_user(email="me_judge@t.demo", display_name="MeJudge")
        EventMembership.objects.create(user=cls.user, event=cls.event,
                                       role=EventMembership.JUDGE, ext_id="jdg_me0")
        cls.token, cls.raw = token_services.create_token(cls.user, "CI read-only")

    def _me(self, raw):
        return self.client.get("/api/v1/me/", HTTP_AUTHORIZATION="Bearer " + raw)

    def test_valid_bearer_returns_own_identity_no_pii(self):
        resp = self._me(self.raw)
        self.assertEqual(resp.status_code, 200)
        data = resp.json()
        self.assertTrue(data["authenticated"])
        self.assertEqual(data["token"]["name"], "CI read-only")
        self.assertEqual(data["token"]["prefix"], self.token.prefix)
        self.assertEqual({m["ext_id"] for m in data["memberships"]}, {"jdg_me0"})
        self.assertEqual(data["memberships"][0]["event"], "evt_me")
        self.assertEqual(data["memberships"][0]["role"], "judge")
        text = resp.content.decode("utf-8")
        self.assertNotIn("me_judge@t.demo", text)        # no email
        self.assertNotIn("MeJudge", text)                 # no display_name
        self.assertEqual(resp["Cache-Control"], "private, no-store")

    def test_valid_bearer_stamps_last_used_at(self):
        self.assertIsNone(ApiToken.objects.get(pk=self.token.pk).last_used_at)
        self._me(self.raw)
        self.assertIsNotNone(ApiToken.objects.get(pk=self.token.pk).last_used_at)

    def test_missing_bearer_is_401(self):
        self.assertEqual(self.client.get("/api/v1/me/").status_code, 401)

    def test_malformed_bearer_is_401(self):
        self.assertEqual(
            self.client.get("/api/v1/me/", HTTP_AUTHORIZATION="Bearer").status_code, 401)
        self.assertEqual(
            self.client.get("/api/v1/me/", HTTP_AUTHORIZATION="Bearer a b").status_code, 401)

    def test_unknown_token_is_401(self):
        self.assertEqual(self._me("dgf_not_a_real_token").status_code, 401)

    def test_revoked_token_is_401(self):
        self.assertTrue(token_services.revoke_token(self.user, self.token.ext_id))
        self.assertEqual(self._me(self.raw).status_code, 401)

    def test_valid_bearer_does_not_change_public_endpoint(self):
        anon = self.client.get("/api/v1/events/")
        authed = self.client.get("/api/v1/events/", HTTP_AUTHORIZATION="Bearer " + self.raw)
        self.assertEqual(anon.status_code, 200)
        self.assertEqual(authed.status_code, 200)
        self.assertEqual(anon.content, authed.content)   # public payload is byte-identical

    def test_malformed_bearer_on_public_endpoint_is_401_but_no_header_stays_public(self):
        # A present-but-broken Authorization header is rejected everywhere (standard DRF behavior);
        # a request with NO Authorization header stays fully public.
        self.assertEqual(
            self.client.get("/api/v1/events/", HTTP_AUTHORIZATION="Bearer bad token").status_code,
            401)
        self.assertEqual(self.client.get("/api/v1/events/").status_code, 200)
