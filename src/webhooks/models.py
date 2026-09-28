# src/webhooks/models.py
"""Outbound webhook data model (T4 feature), additive under its own `webhook_` tables.

An organizer registers signed outbound webhook endpoints for one event; the app POSTs event
notifications to them. HONEST SCOPE: this deployment has no async worker, so deliveries are
attempted SYNCHRONOUSLY and best-effort. Each attempt is recorded as a WebhookDelivery row
(status, response code, error, attempt count), and an organizer can manually retry a failed one.
There is no background queue and no automatic redelivery -- delivery is best-effort only, never
assured and never at-least-once; each attempt is recorded and an organizer can retry a failure.

Every mutating write co-commits a tamper-evident audit event in webhooks.services (never here).
The endpoint `secret` is a random hex string used only to HMAC-SHA256 the request body so a
receiver can authenticate the payload; it is shown to the organizer once at creation and masked
thereafter. Tables are prefixed `webhook_` and no existing table is touched.
"""
from django.conf import settings
from django.db import models

from events.models import Event


class WebhookEndpoint(models.Model):
    """One organizer-registered outbound endpoint for an event.

    `url` is validated by webhooks.ssrf before creation (and again immediately before each
    connection). `secret` HMAC-signs the delivered body. `active` toggles delivery without losing
    the recorded delivery history (deactivate rather than hard-delete, so audit-linked attempts
    survive)."""
    ext_id = models.CharField(max_length=64, unique=True)
    event = models.ForeignKey(Event, on_delete=models.CASCADE, related_name="webhook_endpoints")
    url = models.URLField(max_length=500)
    secret = models.CharField(max_length=64)
    active = models.BooleanField(default=True)
    created_by = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.SET_NULL,
                                   null=True, blank=True,
                                   related_name="webhook_endpoints_created")
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        db_table = "webhook_endpoint"
        constraints = [
            models.UniqueConstraint(fields=["event", "url"], name="uniq_webhook_event_url"),
        ]

    def __str__(self):
        return "%s -> %s" % (self.ext_id, self.url)


class WebhookDelivery(models.Model):
    """One recorded attempt-bearing delivery of one event to one endpoint.

    `payload` is the exact JSON body bytes (as text) that were signed and sent, so the recorded
    `signature` can be recomputed and audited. `status` starts `pending` and becomes `success`
    (2xx) or `failed`; `attempts` counts how many times delivery was tried (initial + manual
    retries). Ordered newest-first for the organizer console."""
    PENDING, SUCCESS, FAILED = "pending", "success", "failed"
    STATUSES = [(PENDING, "pending"), (SUCCESS, "success"), (FAILED, "failed")]

    ext_id = models.CharField(max_length=64, unique=True)
    endpoint = models.ForeignKey(WebhookEndpoint, on_delete=models.CASCADE,
                                 related_name="deliveries")
    event_type = models.CharField(max_length=64)
    payload = models.TextField()
    status = models.CharField(max_length=16, choices=STATUSES, default=PENDING)
    attempts = models.PositiveIntegerField(default=0)
    response_code = models.IntegerField(null=True, blank=True)
    error = models.TextField(blank=True, default="")
    signature = models.CharField(max_length=128, blank=True, default="")
    created_at = models.DateTimeField(auto_now_add=True)
    last_attempt_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        db_table = "webhook_delivery"
        ordering = ["-id"]

    def __str__(self):
        return "%s:%s->%s" % (self.ext_id, self.event_type, self.status)
