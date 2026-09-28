# src/webhooks/tests.py
"""DB-backed tests for outbound webhooks (run: python src/manage.py test webhooks).

They drive the real service + view + URL stack: the organizer/participant/anonymous gates on
registration, the SSRF 422 with no row written, a monkeypatched delivery (success + failure),
manual retry flipping a failed delivery to success, and the tamper-evident audit events each write
co-commits. NO real network is used anywhere -- services._http_post is monkeypatched and every URL
is an IP literal, so ssrf.validate_url never performs DNS.

setUpModule points DOGFOOD_AUDIT_KEY at a throwaway temp path (mirrors records/tests.py) so the
audit spine is hermetic. CACHES is overridden to LocMemCache so the write rate limiter exercises a
working cache instead of the absent `dogfood_cache` table under `manage.py test`.
"""
import os
import shutil
import tempfile
from unittest import mock

from django.contrib.auth import get_user_model
from django.test import TestCase, override_settings
from django.utils import timezone

from audit.models import AuditEvent
from events.models import Event, EventMembership
from webhooks import services
from webhooks.models import WebhookDelivery, WebhookEndpoint

User = get_user_model()

_KEYDIR = None
_PREV_KEY = None


def setUpModule():
    global _KEYDIR, _PREV_KEY
    _KEYDIR = tempfile.mkdtemp(prefix="webhooks_key_")
    _PREV_KEY = os.environ.get("DOGFOOD_AUDIT_KEY")
    os.environ["DOGFOOD_AUDIT_KEY"] = os.path.join(_KEYDIR, "audit_ed25519_key.pem")


def tearDownModule():
    if _PREV_KEY is None:
        os.environ.pop("DOGFOOD_AUDIT_KEY", None)
    else:
        os.environ["DOGFOOD_AUDIT_KEY"] = _PREV_KEY
    if _KEYDIR:
        shutil.rmtree(_KEYDIR, ignore_errors=True)


_PUBLIC_URL = "http://93.184.216.34/hook"   # public IP literal -> no DNS in ssrf.validate_url
_PAYLOAD = {"event": "evt_w", "event_type": "ping", "n": 1}

@override_settings(CACHES={"default": {
    "BACKEND": "django.core.cache.backends.locmem.LocMemCache"}})
class WebhookTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.event = Event.objects.create(
            ext_id="evt_w", name="Winter Hack", state=Event.OPEN,
            submissions_close=timezone.now())
        cls.org_user = User.objects.create_user(
            email="org@example.org", password="pw", display_name="Org")
        EventMembership.objects.create(
            user=cls.org_user, event=cls.event, role=EventMembership.ORGANIZER)
        cls.part_user = User.objects.create_user(
            email="part@example.org", password="pw", display_name="Part")
        EventMembership.objects.create(
            user=cls.part_user, event=cls.event, role=EventMembership.PARTICIPANT)
        cls.endpoint = services.register_endpoint(cls.org_user, cls.event, _PUBLIC_URL)

    def _endpoints_url(self):
        return "/webhooks/%s/endpoints" % self.event.ext_id

    # -- registration gates ---------------------------------------------------
    def test_register_as_organizer_creates_row_with_secret(self):
        self.client.force_login(self.org_user)
        before = WebhookEndpoint.objects.count()
        resp = self.client.post(self._endpoints_url(), {"url": "http://93.184.216.34/second"})
        self.assertIn(resp.status_code, (200, 201, 302))
        self.assertEqual(WebhookEndpoint.objects.count(), before + 1)
        ep = WebhookEndpoint.objects.get(url="http://93.184.216.34/second")
        self.assertTrue(ep.secret)
        self.assertTrue(AuditEvent.objects.filter(event_type="webhook.registered").exists())

    def test_register_as_participant_403(self):
        self.client.force_login(self.part_user)
        resp = self.client.post(self._endpoints_url(), {"url": "http://93.184.216.34/x"})
        self.assertEqual(resp.status_code, 403)

    def test_register_anonymous_redirects_to_login(self):
        resp = self.client.post(self._endpoints_url(), {"url": "http://93.184.216.34/x"})
        self.assertEqual(resp.status_code, 302)

    def test_register_ssrf_blocked_url_422_no_row(self):
        self.client.force_login(self.org_user)
        before = WebhookEndpoint.objects.count()
        resp = self.client.post(self._endpoints_url(), {"url": "http://127.0.0.1/x"})
        self.assertEqual(resp.status_code, 422)
        self.assertEqual(WebhookEndpoint.objects.count(), before)
    # -- delivery + retry (network monkeypatched) -----------------------------
    def test_deliver_success_records_signature_and_audit(self):
        now = timezone.now()
        with mock.patch.object(services, "_http_post", return_value=(200, None)):
            summary = services.deliver_event(self.org_user, self.event, "ping", _PAYLOAD, now=now)
        self.assertEqual(summary, {"sent": 1, "success": 1, "failed": 0})
        d = WebhookDelivery.objects.get(endpoint=self.endpoint)
        self.assertEqual(d.status, WebhookDelivery.SUCCESS)
        self.assertEqual(d.response_code, 200)
        self.assertEqual(d.attempts, 1)
        ts = str(int(now.timestamp()))
        expected = services.sign_body(self.endpoint.secret, ts, services._canonical_body(_PAYLOAD))
        self.assertEqual(d.signature, expected)
        self.assertTrue(d.signature.startswith("sha256="))
        self.assertTrue(AuditEvent.objects.filter(event_type="webhook.delivered").exists())

    def test_deliver_failure_records_failed(self):
        with mock.patch.object(services, "_http_post", return_value=(500, "boom")):
            summary = services.deliver_event(self.org_user, self.event, "ping", _PAYLOAD)
        self.assertEqual(summary, {"sent": 1, "success": 0, "failed": 1})
        d = WebhookDelivery.objects.get(endpoint=self.endpoint)
        self.assertEqual(d.status, WebhookDelivery.FAILED)
        self.assertEqual(d.response_code, 500)
        self.assertEqual(d.error, "boom")
        self.assertTrue(AuditEvent.objects.filter(event_type="webhook.delivery_failed").exists())

    def test_retry_flips_failed_to_success(self):
        with mock.patch.object(services, "_http_post", return_value=(500, "boom")):
            services.deliver_event(self.org_user, self.event, "ping", _PAYLOAD)
        d = WebhookDelivery.objects.get(endpoint=self.endpoint)
        self.assertEqual(d.status, WebhookDelivery.FAILED)
        self.assertEqual(d.attempts, 1)
        with mock.patch.object(services, "_http_post", return_value=(200, None)):
            services.retry_delivery(self.org_user, d)
        d.refresh_from_db()
        self.assertEqual(d.status, WebhookDelivery.SUCCESS)
        self.assertEqual(d.attempts, 2)
        self.assertEqual(d.error, "")
        self.assertTrue(AuditEvent.objects.filter(event_type="webhook.delivery_retried").exists())

    def test_delete_endpoint_deactivates_and_audits(self):
        self.client.force_login(self.org_user)
        resp = self.client.post(
            "/webhooks/%s/endpoints/%s/delete" % (self.event.ext_id, self.endpoint.ext_id))
        self.assertEqual(resp.status_code, 302)
        self.endpoint.refresh_from_db()
        self.assertFalse(self.endpoint.active)
        self.assertTrue(AuditEvent.objects.filter(event_type="webhook.deleted").exists())


