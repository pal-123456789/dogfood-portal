# src/webhooks/services.py
"""Outbound-webhook service layer: registration, signing, synchronous best-effort delivery, and
manual retry -- each mutating write co-committing a tamper-evident audit event in ONE transaction
(the same contract the events/voting/judging services follow).

HONEST SCOPE: there is no async worker. `deliver` performs the network POST synchronously and
records the attempt; delivery is best-effort with a durable attempt log and organizer-driven
retry, NOT an assured or at-least-once queue. The only function that performs network I/O is
`_http_post`, which never raises to its caller, re-validates the URL immediately before connecting,
and refuses to follow redirects.
"""
import hashlib
import hmac
import json
import secrets
import urllib.error
import urllib.request
import uuid
from urllib.parse import urlsplit

from django.db import transaction
from django.utils import timezone

from audit import service as audit_service
from events.models import EventMembership

from . import ssrf
from .models import WebhookDelivery, WebhookEndpoint

# Short so a slow or hung endpoint cannot stall the request thread for long (best-effort, sync).
DELIVERY_TIMEOUT = 5


def _ext_id(prefix):
    """Opaque id in the project-wide shape ("<prefix>_<uuid16>"); disjoint from fixture ids."""
    return "%s_%s" % (prefix, uuid.uuid4().hex[:16])


def new_secret():
    """A fresh random hex secret for HMAC-signing this endpoint's deliveries."""
    return secrets.token_hex(32)


def is_event_organizer(user, event):
    """True iff `user` holds an organizer membership for THIS event (event-scoped; a local mirror
    of voting.services.is_organizer so this app depends on no sibling feature app)."""
    return bool(user and user.is_authenticated and EventMembership.objects.filter(
        user=user, event=event, role=EventMembership.ORGANIZER).exists())


def _host_of(url):
    return urlsplit(url).hostname or ""


def register_endpoint(actor, event, url, *, now=None):
    """Validate `url` (raises ssrf.SsrfError -> the view maps to 422), then create an active
    endpoint with a fresh secret, co-committing a `webhook.registered` audit event."""
    now = now or timezone.now()
    ssrf.validate_url(url)  # SsrfError propagates to the caller
    with transaction.atomic():
        endpoint = WebhookEndpoint.objects.create(
            ext_id=_ext_id("whk"), event=event, url=url, secret=new_secret(),
            created_by=actor if getattr(actor, "pk", None) else None)
        audit_service.record_event(
            event_type="webhook.registered", object_type="webhook_endpoint",
            object_id=endpoint.ext_id, actor_user_id=getattr(actor, "pk", "") or "",
            occurred_at=now.isoformat(),
            payload={"event": event.ext_id, "url_host": _host_of(url)})
    return endpoint


def delete_endpoint(actor, endpoint, *, now=None):
    """Deactivate an endpoint (soft-delete: `active=False`) so its recorded, audit-linked delivery
    history survives, co-committing a `webhook.deleted` audit event."""
    now = now or timezone.now()
    with transaction.atomic():
        endpoint.active = False
        endpoint.save(update_fields=["active"])
        audit_service.record_event(
            event_type="webhook.deleted", object_type="webhook_endpoint",
            object_id=endpoint.ext_id, actor_user_id=getattr(actor, "pk", "") or "",
            occurred_at=now.isoformat(),
            payload={"event": endpoint.event.ext_id, "url_host": _host_of(endpoint.url)})
    return endpoint


def _canonical_body(payload_dict):
    """Deterministic JSON body bytes (sorted keys, compact separators) -- signed AND sent, so the
    recorded signature is reproducible from the recorded payload."""
    return json.dumps(payload_dict, sort_keys=True, separators=(",", ":")).encode("utf-8")


def sign_body(secret, timestamp, body_bytes):
    """HMAC-SHA256 over "<timestamp>." + body, hex, prefixed "sha256=". The timestamp is bound into
    the MAC (and sent as X-Dogfood-Timestamp) so a captured body cannot be replayed under a new
    timestamp header. Header scheme: X-Dogfood-Event, X-Dogfood-Timestamp, X-Dogfood-Signature."""
    mac = hmac.new(secret.encode("utf-8"),
                   ("%s." % timestamp).encode("utf-8") + body_bytes, hashlib.sha256)
    return "sha256=" + mac.hexdigest()


def _http_post(url, headers, body_bytes, timeout):
    """The ONLY function that performs network I/O. Returns (status_code|None, error|None) and
    NEVER raises. Re-validates the URL right before connecting (the resolve-then-connect window is
    small but real -- a name could re-resolve inside between registration and now) and refuses to
    follow redirects, returning any 3xx as an error so a redirect cannot bounce to an internal host.
    Kept tiny so tests monkeypatch it and no real socket is opened anywhere in the suite."""
    try:
        ssrf.validate_url(url)
    except ssrf.SsrfError as exc:
        return None, "ssrf: %s" % exc.reason

    class _NoRedirect(urllib.request.HTTPRedirectHandler):
        def redirect_request(self, req, fp, code, msg, hdrs, newurl):
            return None  # do not follow -> urllib raises the 3xx as an HTTPError

    req = urllib.request.Request(url, data=body_bytes, headers=headers, method="POST")
    opener = urllib.request.build_opener(_NoRedirect)
    try:
        with opener.open(req, timeout=timeout) as resp:
            return resp.getcode(), None
    except urllib.error.HTTPError as exc:
        if 300 <= exc.code < 400:
            return exc.code, "redirect not followed (HTTP %s)" % exc.code
        return exc.code, "HTTP %s" % exc.code
    except Exception as exc:  # timeout, connection refused, DNS, TLS, anything -- never propagate
        return None, "%s: %s" % (type(exc).__name__, exc)


def deliver(endpoint, event_type, payload_dict, *, delivery=None, now=None):
    """Attempt one synchronous delivery of `event_type`/`payload_dict` to `endpoint`, recording the
    attempt. Builds the canonical body, signs it, calls `_http_post` (OUTSIDE the transaction, so a
    DB transaction is never held open across network I/O), then co-commits the WebhookDelivery row
    (attempts+1, last_attempt_at, status, response_code, error, signature) with a
    `webhook.delivered` (2xx) or `webhook.delivery_failed` audit event in ONE transaction. Pass an
    existing `delivery` to update it (used by retry). A network error is recorded, never raised."""
    now = now or timezone.now()
    body = _canonical_body(payload_dict)
    ts = str(int(now.timestamp()))
    signature = sign_body(endpoint.secret, ts, body)
    headers = {"Content-Type": "application/json", "User-Agent": "dogfood-webhooks/1",
               "X-Dogfood-Event": event_type, "X-Dogfood-Timestamp": ts,
               "X-Dogfood-Signature": signature}
    code, error = _http_post(endpoint.url, headers, body, DELIVERY_TIMEOUT)
    ok = code is not None and 200 <= code < 300
    with transaction.atomic():
        if delivery is None:
            delivery = WebhookDelivery(ext_id=_ext_id("whd"), endpoint=endpoint,
                                       event_type=event_type, payload=body.decode("utf-8"))
        delivery.attempts = (delivery.attempts or 0) + 1
        delivery.last_attempt_at = now
        delivery.signature = signature
        delivery.response_code = code
        delivery.error = "" if ok else (error or "delivery failed")
        delivery.status = WebhookDelivery.SUCCESS if ok else WebhookDelivery.FAILED
        delivery.save()
        audit_service.record_event(
            event_type="webhook.delivered" if ok else "webhook.delivery_failed",
            object_type="webhook_delivery", object_id=delivery.ext_id,
            occurred_at=now.isoformat(),
            payload={"event": endpoint.event.ext_id, "endpoint": endpoint.ext_id,
                     "event_type": event_type, "status_code": code, "attempt": delivery.attempts})
    return delivery


def deliver_event(actor, event, event_type, payload_dict, *, now=None):
    """Deliver `event_type` to every ACTIVE endpoint of `event`, best-effort. Returns summary
    counts {sent, success, failed}. Each per-endpoint attempt is audited by `deliver`."""
    summary = {"sent": 0, "success": 0, "failed": 0}
    for ep in WebhookEndpoint.objects.filter(event=event, active=True):
        d = deliver(ep, event_type, payload_dict, now=now)
        summary["sent"] += 1
        summary["success" if d.status == WebhookDelivery.SUCCESS else "failed"] += 1
    return summary


def retry_delivery(actor, delivery, *, now=None):
    """Re-attempt a recorded delivery on its own endpoint/payload, co-committing a
    `webhook.delivery_retried` audit event, then running `deliver` semantics (which appends the
    fresh outcome event). The stored payload is already canonical, so re-parsing round-trips it."""
    now = now or timezone.now()
    try:
        payload_dict = json.loads(delivery.payload or "{}")
    except (ValueError, TypeError):
        payload_dict = {}
    with transaction.atomic():
        audit_service.record_event(
            event_type="webhook.delivery_retried", object_type="webhook_delivery",
            object_id=delivery.ext_id, actor_user_id=getattr(actor, "pk", "") or "",
            occurred_at=now.isoformat(),
            payload={"event": delivery.endpoint.event.ext_id, "endpoint": delivery.endpoint.ext_id,
                     "prior_attempts": delivery.attempts})
    return deliver(delivery.endpoint, delivery.event_type, payload_dict, delivery=delivery, now=now)


