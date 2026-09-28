# src/webhooks/admin.py
"""Admin registrations for outbound webhooks -- both models inspect-only (ReadOnlyModelAdmin).

Endpoints and their attempt-bearing, audit-linked deliveries are created and managed through the
organizer console (webhooks.views), not the admin, so the admin is a read-only window: an operator
can inspect rows but cannot add/edit/delete them here. The HMAC secret is never shown in full --
list_display carries only a masked last-4 hint, and there is no change form that could reveal it."""
from django.contrib import admin

from portal.admin_mixins import ReadOnlyModelAdmin

from .models import WebhookDelivery, WebhookEndpoint


@admin.register(WebhookEndpoint)
class WebhookEndpointAdmin(ReadOnlyModelAdmin):
    list_display = ("ext_id", "event", "url", "active", "secret_hint", "created_at")
    list_filter = ("active",)
    search_fields = ("ext_id", "url", "event__ext_id", "event__name")

    @admin.display(description="secret")
    def secret_hint(self, obj):
        return "****%s" % (obj.secret[-4:] if obj.secret else "")


@admin.register(WebhookDelivery)
class WebhookDeliveryAdmin(ReadOnlyModelAdmin):
    list_display = ("ext_id", "endpoint", "event_type", "status", "attempts",
                    "response_code", "last_attempt_at")
    list_filter = ("status", "event_type")
    search_fields = ("ext_id", "endpoint__ext_id", "endpoint__url", "event_type")
