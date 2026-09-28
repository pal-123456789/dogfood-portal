# src/api/test_certificate.py
"""DB-backed tests for the verifiable results certificate endpoint
    GET /api/v1/events/<ext_id>/certificate/
(run: python src/manage.py test api).

They pin what makes the certificate safe to expose publicly:
  * a PUBLISHED event returns a certificate whose result_hash + signer fingerprint match the signed
    publication, with a non-empty ranking of the public columns only;
  * an unpublished (or unknown) event returns 404 -- there is no signed run to certify;
  * NO PII appears anywhere (no emails, judge/display names, ballots, or per-judge scores);
  * the certificate's result_hash equals the publication's signed run result_hash (it restates the
    frozen signed run, never a recompute);
  * the endpoint is GET-only.

CACHES is overridden to LocMemCache (as in the other app test suites) because the test DB has no
`dogfood_cache` table; without it the API's anon throttle would touch a missing table.
"""
from datetime import timedelta

from django.test import TestCase, override_settings
from django.utils import timezone

from accounts.models import AppUser
from audit import receipts
from events.models import Event, EventMembership, Team, Track
from judging.models import Ballot, JudgeAssignment, RubricWeight
from normalize import engine, results
from normalize.models import NormalizationRun, ResultPublication
from submissions.models import Submission

_LOCMEM = {"default": {"BACKEND": "django.core.cache.backends.locmem.LocMemCache",
                       "LOCATION": "cert-api-tests"}}

# Same PII tokens the api/normalize suites guard; a leak would surface as one of these. The public
# aggregate "n_ballots" is intentionally absent (it is a count, not a ballot) and does not appear in
# the certificate anyway.
_SENSITIVE = ("@t.demo", "email", "display_name", "functionality", "quality", "innovation", "jdg_")


def _seed_event(prefix, *, publish):
    """A CLOSED 3x3 additive event (three equal-weight judges biased 0/+1/-1); optionally publishes
    official results with an ephemeral key. Mirrors api/tests._seed_event."""
    ev = Event.objects.create(ext_id="evt_%s" % prefix, name="Event %s" % prefix,
                              state=Event.CLOSED,
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
    if publish:
        results.publish_results(ev, key=receipts.generate_private_key(),
                                note="Final.", n_boot=120, seed=0)
    return ev


@override_settings(CACHES=_LOCMEM)
class CertificateApiTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.event_pub = _seed_event("cp", publish=True)
        cls.event_unpub = _seed_event("cu", publish=False)

    def _get(self, url, status=200):
        resp = self.client.get(url)
        self.assertEqual(resp.status_code, status, url)
        return resp

    def test_published_event_returns_certificate(self):
        data = self._get("/api/v1/events/evt_cp/certificate/").json()
        self.assertEqual(data["kind"], "dogfood.results-certificate.v1")
        self.assertEqual(data["event"]["ext_id"], "evt_cp")
        self.assertEqual(data["publication"]["status"], ResultPublication.FINAL)
        self.assertEqual(data["publication"]["version"], 1)
        self.assertTrue(data["result_hash"])
        self.assertTrue(data["signer_fingerprint"])
        self.assertEqual(data["engine_version"], engine.ENGINE_VERSION)
        self.assertGreaterEqual(len(data["ranking"]), 1)
        self.assertEqual(set(data["ranking"][0]), {"rank", "submission", "title", "q"})

    def test_certificate_result_hash_equals_signed_publication(self):
        data = self._get("/api/v1/events/evt_cp/certificate/").json()
        pub = (ResultPublication.objects.filter(event_ext_id="evt_cp")
               .order_by("-version").first())
        run = NormalizationRun.objects.get(run_ext_id=pub.run_ext_id)
        self.assertEqual(data["result_hash"], run.result_hash)
        self.assertEqual(data["signer_fingerprint"], run.fingerprint)
        self.assertEqual(data["signed_material"]["result_hash"], run.result_hash)
        self.assertEqual(data["signature"]["value"], run.signature)

    def test_certificate_has_no_pii(self):
        resp = self._get("/api/v1/events/evt_cp/certificate/")
        text = resp.content.decode("utf-8")
        for token in _SENSITIVE:
            self.assertNotIn(token, text, "leaked sensitive token %r" % token)

    def test_unpublished_event_has_no_certificate_404(self):
        self._get("/api/v1/events/evt_cu/certificate/", status=404)

    def test_unknown_event_is_404(self):
        self._get("/api/v1/events/evt_missing/certificate/", status=404)

    def test_certificate_is_get_only(self):
        self.assertEqual(
            self.client.post("/api/v1/events/evt_cp/certificate/").status_code, 405)
