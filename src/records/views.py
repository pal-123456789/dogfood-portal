# src/records/views.py
"""Signed, publicly verifiable participation records (T4) -- plain Django views + JsonResponse.

Four endpoints, no DRF, no templates (JSON/PEM only), all inside src/records/:
  * signing_key   -- the deployment's Ed25519 PUBLIC key as PEM (anonymous OK). The orchestrator
                     ALSO wires this exact function at /.well-known/dogfood-signing-key; it depends
                     on nothing app-local, so it works standalone from either mount.
  * judge_record       -- GET /records/judge?event=<ext_id>: the CALLING judge's own signed record.
  * participant_record -- GET /records/participant?event=<ext_id>: the CALLING participant's record.
  * verify             -- POST /records/verify: recompute + verify a record against OUR public key.

Gating follows normalize.views._gate's shape (401 anonymous, 403 wrong role). Records carry NO
scores/ballots and attest only a signature over participation facts (services.ATTESTATION) -- not
merit, not fraud detection. None of these routes is on the acceptance checker's flat set.
"""
from __future__ import annotations

import json

from django.http import HttpResponse, JsonResponse
from django.views.decorators.csrf import csrf_exempt
from django.views.decorators.http import require_http_methods

from audit import keys, receipts
from events.models import Event, EventMembership

from . import services


def signing_key(request):
    """Return the deployment's Ed25519 PUBLIC key as PEM (anonymous allowed, GET).

    Body is exactly audit.receipts.public_key_to_pem(audit.keys.ensure_private_key()[0].public_key()).
    The fingerprint a verifier must pin is exposed in the X-Signing-Key-Fingerprint header. This is
    the public half only -- the private key never leaves /state.
    """
    key, _created = keys.ensure_private_key()
    pub = key.public_key()
    resp = HttpResponse(receipts.public_key_to_pem(pub),
                        content_type="application/x-pem-file")
    resp["X-Signing-Key-Fingerprint"] = receipts.public_fingerprint(pub)
    return resp


def _authed_or_401(request):
    """401 JsonResponse for an anonymous caller, else None. Auth is checked BEFORE event
    resolution so an anonymous caller cannot probe which events exist."""
    if request.user and request.user.is_authenticated:
        return None
    return JsonResponse({"detail": "authentication required"}, status=401)


def _event_or_404(request):
    """Resolve the ?event=<ext_id> event, or (None, 404 JsonResponse)."""
    event = Event.objects.filter(ext_id=request.GET.get("event", "")).first()
    if event is None:
        return None, JsonResponse({"detail": "no such event"}, status=404)
    return event, None


@require_http_methods(["GET"])
def judge_record(request):
    """The calling user's signed judge record for ?event=<ext_id>.

    Anonymous -> 401; authenticated non-judge -> 403; a judge asking for another judge's record via
    ?judge=<other ext_id> -> 403 (a judge may fetch only their own).
    """
    err = _authed_or_401(request)
    if err:
        return err
    event, err = _event_or_404(request)
    if err:
        return err
    membership = EventMembership.objects.filter(
        user=request.user, event=event, role=EventMembership.JUDGE).first()
    if membership is None:
        return JsonResponse({"detail": "judges only"}, status=403)
    requested = request.GET.get("judge")
    if requested and requested != membership.ext_id:
        return JsonResponse({"detail": "a judge may fetch only their own record"}, status=403)
    return JsonResponse(services.judge_record(event, membership))


@require_http_methods(["GET"])
def participant_record(request):
    """The calling user's signed participant record for ?event=<ext_id>.

    Anonymous -> 401; authenticated non-participant of the event -> 403.
    """
    err = _authed_or_401(request)
    if err:
        return err
    event, err = _event_or_404(request)
    if err:
        return err
    is_participant = EventMembership.objects.filter(
        user=request.user, event=event, role=EventMembership.PARTICIPANT).exists()
    if not is_participant:
        return JsonResponse({"detail": "participants only"}, status=403)
    return JsonResponse(services.participant_record(event, request.user))


@csrf_exempt
@require_http_methods(["POST"])
def verify(request):
    """Verify a previously issued record against OUR public key. 200 with {"valid": bool} either way.

    csrf_exempt is SAFE here and deliberate: this is a pure, stateless verifier -- it performs no
    state change (writes nothing to the DB), reads only the deployment's public key, takes no
    auth-sensitive action, and returns only a boolean. So it is not a writable data API and needs no
    CSRF token / session; anyone holding a record can check it. A malformed body -> {"valid": false}.
    """
    try:
        record = json.loads(request.body.decode("utf-8"))
    except (ValueError, UnicodeDecodeError):
        return JsonResponse({"valid": False})
    return JsonResponse({"valid": services.verify_record_json(record)})
