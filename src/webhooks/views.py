# src/webhooks/views.py
"""Outbound-webhooks HTTP surface, all under the /webhooks/ prefix (wired by portal/urls.py by the
orchestrator) and additive to the checker's five flat routes. Every action is ORGANIZER-of-this-
event only, mirroring voting.views: anonymous is sent to log in, an authenticated non-organizer
(including a participant) gets a plain-text 403. The atomic write + audit invariants live in
webhooks.services. CSP is `script-src 'self'`, so the template carries inline <style> only.
"""
from django.conf import settings
from django.contrib.auth.views import redirect_to_login
from django.http import HttpResponse, JsonResponse
from django.shortcuts import get_object_or_404, redirect, render
from django.urls import reverse
from django.utils import timezone
from django.views.decorators.http import require_http_methods

from portal import ratelimit
from events.models import Event

from . import services, ssrf
from .models import WebhookDelivery, WebhookEndpoint

_NOTICES = {"deleted": "Endpoint deactivated. Its recorded delivery history is retained.",
            "retried": "Delivery re-attempted; see the latest attempt below."}
# Session flash carrying a just-created endpoint's secret, revealed exactly once on the next GET.
_FLASH = "webhooks_reveal"


def _plain(msg, status):
    return HttpResponse(msg, status=status, content_type="text/plain")


def _organizer_or_response(request, event_ext_id):
    """Resolve event + enforce organizer-of-this-event. Returns (event, None) or (None, response).
    Anonymous -> login redirect; authenticated non-organizer -> plain 403 (so a participant 403s)."""
    if not (request.user and request.user.is_authenticated):
        return None, redirect_to_login(request.get_full_path(), settings.LOGIN_URL)
    event = get_object_or_404(Event, ext_id=event_ext_id)
    if not services.is_event_organizer(request.user, event):
        return None, _plain("organizers only", 403)
    return event, None


def _ctx(event, *, reveal=None, notice="", error="", form=None, delivered=None):
    return {
        "event": event,
        "endpoints": list(WebhookEndpoint.objects.filter(event=event).order_by("-id")),
        "deliveries": list(WebhookDelivery.objects.filter(endpoint__event=event)
                           .select_related("endpoint")[:50]),
        "reveal": reveal, "notice": notice, "error": error, "form": form or {},
        "delivered": delivered,
        "rate_limit": settings.DOGFOOD_RATE_LIMITS.get("webhook_write", "60/h"),
    }


@require_http_methods(["GET", "POST"])
def endpoints(request, event_ext_id):
    """GET: organizer console (endpoints + recent deliveries). POST: register a new endpoint.
    A blocked/invalid URL re-renders the form with an error and status 422. POST is rate-limited
    under DOGFOOD_RATE_LIMITS['webhook_write'] (keyed on event+user; the demo shim is exempt)."""
    event, resp = _organizer_or_response(request, event_ext_id)
    if resp is not None:
        return resp

    if request.method == "POST":
        if not getattr(request, "demo_shim", False):
            allowed, retry = ratelimit.hit(
                "webhook_write:%s:%s" % (event.ext_id, request.user.pk),
                settings.DOGFOOD_RATE_LIMITS.get("webhook_write", "60/h"))
            if not allowed:
                r = _plain("rate limited", 429)
                r["Retry-After"] = str(retry)
                return r
        url = (request.POST.get("url") or "").strip()
        try:
            endpoint = services.register_endpoint(request.user, event, url)
        except ssrf.SsrfError as exc:
            return render(request, "webhooks/endpoints.html",
                          _ctx(event, error="URL rejected: %s" % exc.reason, form=request.POST),
                          status=422)
        request.session[_FLASH] = {"ext_id": endpoint.ext_id, "secret": endpoint.secret}
        return redirect("%s?created=%s" % (
            reverse("webhooks:endpoints", args=[event.ext_id]), endpoint.ext_id))

    reveal = None
    flash = request.session.pop(_FLASH, None)
    if flash and flash.get("ext_id") == request.GET.get("created"):
        reveal = flash
    delivered = _parse_delivered(request.GET.get("delivered"))
    return render(request, "webhooks/endpoints.html",
                  _ctx(event, reveal=reveal, delivered=delivered,
                       notice=_NOTICES.get(request.GET.get("ok", ""), "")))


def _parse_delivered(raw):
    """Decode the `sent.success.failed` counts carried back after a dispatch, or None."""
    if not raw:
        return None
    try:
        sent, success, failed = (int(x) for x in raw.split("."))
    except (ValueError, TypeError):
        return None
    return {"sent": sent, "success": success, "failed": failed}


@require_http_methods(["POST"])
def delete_endpoint(request, event_ext_id, endpoint_ext_id):
    """Organizer-only: deactivate one endpoint (soft-delete, history retained)."""
    event, resp = _organizer_or_response(request, event_ext_id)
    if resp is not None:
        return resp
    endpoint = get_object_or_404(WebhookEndpoint, ext_id=endpoint_ext_id, event=event)
    services.delete_endpoint(request.user, endpoint)
    return redirect("%s?ok=deleted" % reverse("webhooks:endpoints", args=[event.ext_id]))


@require_http_methods(["POST"])
def deliver(request, event_ext_id):
    """Organizer-only: dispatch one event_type (default "ping") to every active endpoint,
    synchronously and best-effort. Returns the JSON summary for a JSON/format=json caller, else
    redirects back to the console carrying the counts."""
    event, resp = _organizer_or_response(request, event_ext_id)
    if resp is not None:
        return resp
    event_type = (request.POST.get("event_type") or "ping").strip() or "ping"
    summary = services.deliver_event(
        request.user, event, event_type,
        {"event": event.ext_id, "event_type": event_type, "at": timezone.now().isoformat()})
    if request.content_type == "application/json" or request.GET.get("format") == "json":
        return JsonResponse(summary)
    return redirect("%s?delivered=%d.%d.%d" % (
        reverse("webhooks:endpoints", args=[event.ext_id]),
        summary["sent"], summary["success"], summary["failed"]))


@require_http_methods(["POST"])
def retry(request, event_ext_id, delivery_ext_id):
    """Organizer-only: re-attempt one recorded delivery on its own endpoint/payload."""
    event, resp = _organizer_or_response(request, event_ext_id)
    if resp is not None:
        return resp
    delivery = get_object_or_404(WebhookDelivery, ext_id=delivery_ext_id,
                                 endpoint__event=event)
    services.retry_delivery(request.user, delivery)
    return redirect("%s?ok=retried" % reverse("webhooks:endpoints", args=[event.ext_id]))
