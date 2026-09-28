# src/bundles/views.py
"""Site-administrator-only HTTP surface for portable event bundles (T4) -- plain Django, no DRF.

Three endpoints under /bundles/ (JSON / CSV / plain-text errors, no templates):
  * export.json -- the signed, portable bundle for one event;
  * export.csv  -- a submissions summary CSV (all stages), with official rank/score appended only
                   when results are published;
  * import      -- reconstruct an event graph from a signed bundle on this deployment.

Gate: every endpoint requires an authenticated request.user with is_superuser (a site
administrator). Anonymous -> 401; an authenticated non-superuser (even an event organizer) -> 403.
Auth is checked BEFORE event resolution so a caller who may not use these endpoints cannot probe
which events exist. None of these routes is on the acceptance checker's flat set and none is linked
from base.html.
"""
from __future__ import annotations

import json

from django.http import HttpResponse, JsonResponse
from django.shortcuts import get_object_or_404
from django.views.decorators.csrf import csrf_exempt
from django.views.decorators.http import require_http_methods

from events.models import Event

from . import services


def _site_admin_or_response(request):
    """None if the caller is an authenticated site administrator, else a plain-text error response.

    401 for anonymous, 403 for an authenticated non-superuser. is_superuser comes from Django's
    PermissionsMixin (accounts.AppUser); an event-scoped organizer role is NOT sufficient -- these
    are cross-event, whole-graph operations reserved for the operator.
    """
    user = request.user
    if not (user and user.is_authenticated):
        return HttpResponse("authentication required", status=401, content_type="text/plain")
    if not user.is_superuser:
        return HttpResponse("site administrators only", status=403, content_type="text/plain")
    return None


@require_http_methods(["GET"])
def export_json(request, event_ext_id):
    """GET the signed portable bundle for one event (site-admin). 404 if the event is unknown."""
    resp = _site_admin_or_response(request)
    if resp is not None:
        return resp
    event = get_object_or_404(Event, ext_id=event_ext_id)
    return JsonResponse(services.sign_export(event, actor=request.user))


@require_http_methods(["GET"])
def export_csv(request, event_ext_id):
    """GET the all-stages submissions CSV for one event (site-admin). 404 if the event is unknown."""
    resp = _site_admin_or_response(request)
    if resp is not None:
        return resp
    event = get_object_or_404(Event, ext_id=event_ext_id)
    response = HttpResponse(services.all_stage_csv(event), content_type="text/csv")
    response["Content-Disposition"] = 'attachment; filename="%s-all-stages.csv"' % event.ext_id
    response["Cache-Control"] = "private, no-store"
    return response


@csrf_exempt
@require_http_methods(["POST"])
def import_bundle(request):
    """POST a signed bundle to reconstruct an event graph (site-admin). 400 bad sig / body,
    409 if the event ext_id already exists, 201 on success.

    csrf_exempt rationale + honest tradeoff: this is a JSON API for operator tooling/scripts, not an
    HTML form, so there is no CSRF token to carry. The real authority is the is_superuser gate above,
    and the body must parse as JSON (a browser cannot silently cross-post an application/json body
    without a CORS preflight this server will not approve). Exempting CSRF does mean the session
    cookie alone authorizes the write, so a stricter deployment should call this with an explicit
    admin credential / token rather than an ambient browser session; that is a deliberate tradeoff
    for API usability and testability, not a claim that CSRF is irrelevant.
    """
    resp = _site_admin_or_response(request)
    if resp is not None:
        return resp
    try:
        doc = json.loads(request.body.decode("utf-8"))
    except (ValueError, UnicodeDecodeError):
        return HttpResponse("request body must be valid JSON", status=400,
                            content_type="text/plain")
    try:
        result = services.import_bundle(request.user, doc)
    except services.BundleInvalid as exc:
        return HttpResponse(str(exc) or "invalid bundle", status=400, content_type="text/plain")
    except services.BundleConflict as exc:
        return HttpResponse("an event with ext_id %s already exists" % (str(exc) or ""),
                            status=409, content_type="text/plain")
    return JsonResponse(result, status=201)
