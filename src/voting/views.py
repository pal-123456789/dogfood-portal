# src/voting/views.py
"""Community-voting HTTP surface, all under the /voting/ prefix (wired by portal/urls.py) and
additive to the checker's five flat routes. Authorization lives here; the atomic write + audit
invariants live in voting.services, mirroring the events/judging split.

Gate summary:
  * organizer-only: manage (configure), publish -- anonymous is sent to log in, an authenticated
    non-organizer gets a plain 403;
  * voter-facing: ballot (GET shows the ballot or, in email modes, the email-entry form; POST
    casts), join (request an email link), confirm (confirm the link);
  * public: results (hidden with 404 until published AND the window has closed).
CSP is `script-src 'self'`, so the templates carry no inline <script>; budget math is server-side.
"""
import json

from django.conf import settings
from django.contrib.auth.views import redirect_to_login
from django.core.exceptions import ValidationError
from django.http import Http404, HttpResponse, JsonResponse
from django.shortcuts import get_object_or_404, redirect, render
from django.urls import reverse
from django.utils import timezone
from django.views.decorators.http import require_http_methods

from portal import ratelimit
from events.models import Event

from . import services
from .models import VoteToken, VotingCampaign

_NOTICES = {"configured": "Voting campaign saved.", "cast": "Your ballot has been recorded.",
            "published": "Results published."}


def _plain(msg, status):
    return HttpResponse(msg, status=status, content_type="text/plain")


def _login_redirect(request):
    return redirect_to_login(request.get_full_path(), settings.LOGIN_URL)


def _organizer_or_response(request, event_ext_id):
    """Resolve event + enforce organizer-of-this-event. Returns (event, None) or (None, response).
    Anonymous -> login redirect; authenticated non-organizer -> plain 403."""
    if not (request.user and request.user.is_authenticated):
        return None, _login_redirect(request)
    event = get_object_or_404(Event, ext_id=event_ext_id)
    if not services.is_organizer(request.user, event):
        return None, _plain("organizers only", 403)
    return event, None


@require_http_methods(["GET", "POST"])
def manage(request, event_ext_id):
    """Organizer-only campaign console: GET shows the current config + form; POST saves it."""
    event, resp = _organizer_or_response(request, event_ext_id)
    if resp is not None:
        return resp
    campaign = services.get_campaign(event)
    if request.method == "POST":
        emails = request.POST.get("eligible_emails", "").splitlines()
        try:
            services.configure_campaign(
                request.user, event,
                mode=request.POST.get("mode", VotingCampaign.AUTHENTICATED),
                credit_budget=request.POST.get("credit_budget", "25"),
                opens_at=request.POST.get("opens_at", ""),
                closes_at=request.POST.get("closes_at", ""),
                eligible_emails=emails)
        except ValidationError as e:
            return render(request, "voting/manage.html",
                          _manage_ctx(event, campaign, error=" ".join(e.messages),
                                      form=request.POST), status=400)
        return redirect("%s?ok=configured" % reverse("voting:manage", args=[event.ext_id]))
    return render(request, "voting/manage.html",
                  _manage_ctx(event, campaign,
                              notice=_NOTICES.get(request.GET.get("ok", ""), "")))
# CHUNK_VIEWS_1


def _manage_ctx(event, campaign, *, notice="", error="", form=None):
    eligible = ""
    if campaign is not None:
        eligible = "\n".join(campaign.eligible_voters.values_list("email", flat=True))
    return {"event": event, "campaign": campaign, "modes": VotingCampaign.MODES,
            "eligible_emails": eligible, "notice": notice, "error": error,
            "form": form or {}}


def _voter_key(voter):
    """A stable per-voter key for the ballot shuffle: the auth user pk, else the voter ext_id."""
    return "user:%s" % voter.user_id if voter.user_id else "voter:%s" % voter.ext_id


def _session_key(campaign):
    return "voting_voter:%s" % campaign.ext_id


def _current_voter(request, campaign):
    """The caller's Voter for this campaign, or None. Authenticated mode uses request.user; email
    modes use the confirmed voter ext_id stashed in the session by the confirm view."""
    if campaign.mode == VotingCampaign.AUTHENTICATED:
        if not (request.user and request.user.is_authenticated):
            return None
        return services.voter_for_user(campaign, request.user)
    ext = request.session.get(_session_key(campaign))
    if not ext:
        return None
    return campaign.voters.filter(ext_id=ext).first()


def _ballot_rows(campaign, voter):
    """Ordered ballot rows for `voter`: each SUBMITTED project in the per-voter shuffle, carrying
    the voter's current vote count (0 if none)."""
    subs = services.voteable_submissions(campaign.event)
    ordered = services.ballot_order(campaign.ext_id, _voter_key(voter), subs)
    current = {a.submission_id: a.votes for a in voter.allocations.all()}
    return [{"submission": s, "votes": current.get(s.id, 0)} for s in ordered]


def _ballot_ctx(campaign, voter, *, notice="", error=""):
    return {"event": campaign.event, "campaign": campaign, "rows": _ballot_rows(campaign, voter),
            "budget": campaign.credit_budget, "open": campaign.is_open(timezone.now()),
            "notice": notice, "error": error}


@require_http_methods(["GET", "POST"])
def ballot(request, event_ext_id):
    """GET shows the ballot (or, in email modes without a confirmed voter, the email-entry form);
    POST casts/replaces the ballot. Authenticated mode: anonymous -> 401, a judge of the event ->
    403. Casts are rate-limited under DOGFOOD_RATE_LIMITS['vote_write'] (the demo shim is exempt)."""
    event = get_object_or_404(Event, ext_id=event_ext_id)
    campaign = services.get_campaign(event)
    if campaign is None:
        raise Http404("voting is not configured for this event")

    if campaign.mode == VotingCampaign.AUTHENTICATED:
        if not (request.user and request.user.is_authenticated):
            return _plain("authentication required", 401)
        if services.is_judge(request.user, event):
            return _plain("judges may not vote in community voting", 403)

    voter = _current_voter(request, campaign)

    if request.method == "GET":
        if voter is None:
            # email modes, not yet confirmed: offer the email-entry form.
            return render(request, "voting/join.html",
                          {"event": event, "campaign": campaign, "notice": "", "error": ""})
        return render(request, "voting/ballot.html", _ballot_ctx(campaign, voter))

    # POST = cast. An email-mode caller with no confirmed identity must confirm first.
    if voter is None:
        return _plain("confirm your email before voting", 401)
    if not getattr(request, "demo_shim", False):
        allowed, retry = ratelimit.hit(
            "vote_write:%s:%s" % (campaign.ext_id, voter.ext_id),
            settings.DOGFOOD_RATE_LIMITS.get("vote_write", "20/m"))
        if not allowed:
            resp = _plain("rate limited", 429)
            resp["Retry-After"] = str(retry)
            return resp

    raw_map = _posted_allocations(request)
    try:
        result = services.cast_ballot(voter, campaign, raw_map=raw_map)
    except services.VotingClosed:
        return _cast_error(request, campaign, voter, "Voting is not open.", 409)
    except services.OverBudget:
        return _cast_error(
            request, campaign, voter,
            "That ballot spends more than the %d-credit budget." % campaign.credit_budget, 409)
    except services.InvalidBallot as e:
        return _cast_error(request, campaign, voter, str(e), 400)
    if request.content_type == "application/json":
        return JsonResponse({"ok": True, "credits_spent": result["credits_spent"],
                             "allocations": result["allocations"]})
    return render(request, "voting/ballot.html",
                  _ballot_ctx(campaign, voter, notice=_NOTICES["cast"]))


def _posted_allocations(request):
    """Submitted {submission_ext_id: count} map, from a JSON body or the form POST. Unknown keys
    are ignored downstream (services.clean_allocations restricts to the event's projects)."""
    if request.content_type == "application/json":
        try:
            data = json.loads(request.body or b"{}")
        except (ValueError, TypeError):
            data = {}
        return data if isinstance(data, dict) else {}
    return request.POST


def _cast_error(request, campaign, voter, msg, status):
    if request.content_type == "application/json":
        return JsonResponse({"detail": msg}, status=status)
    return render(request, "voting/ballot.html",
                  _ballot_ctx(campaign, voter, error=msg), status=status)
# CHUNK_VIEWS_2


@require_http_methods(["POST"])
def join(request, event_ext_id):
    """Email modes: a voter submits their address to receive a confirm link. The link is recorded
    as an OutboundEmail (this deployment records rather than sends mail) and is NOT echoed back, so
    possession of the delivered link remains the proof of address control. A normalized address
    that already has a confirmed voter is refused (409, audited)."""
    event = get_object_or_404(Event, ext_id=event_ext_id)
    campaign = services.get_campaign(event)
    if campaign is None:
        raise Http404("voting is not configured for this event")
    if campaign.mode == VotingCampaign.AUTHENTICATED:
        return _plain("this campaign uses portal sign-in, not email links", 400)
    try:
        services.request_vote_token(campaign, request.POST.get("email", ""))
    except services.DuplicateVoter:
        return render(request, "voting/join.html",
                      {"event": event, "campaign": campaign, "notice": "",
                       "error": "That email has already been used to vote in this event."},
                      status=409)
    except ValidationError as e:
        return render(request, "voting/join.html",
                      {"event": event, "campaign": campaign, "notice": "",
                       "error": " ".join(e.messages)}, status=400)
    return render(request, "voting/join.html",
                  {"event": event, "campaign": campaign, "error": "",
                   "notice": ("A confirmation link has been generated for that address. This "
                              "deployment records outbound email rather than sending it; an "
                              "organizer can retrieve the link from the outbound-email log.")})


@require_http_methods(["GET", "POST"])
def confirm(request, token):
    """Confirm an email link: GET shows a confirm button, POST creates the voter identity, stores
    it in the session, and redirects to the ballot. A gated campaign refuses an address that is not
    on the organizer allow-list with a plain 403."""
    token_obj = get_object_or_404(VoteToken.objects.select_related("campaign__event"), token=token)
    campaign = token_obj.campaign
    if request.method == "GET":
        return render(request, "voting/confirm.html",
                      {"event": campaign.event, "campaign": campaign, "token": token_obj.token,
                       "email": token_obj.email})
    try:
        voter = services.confirm_token(token_obj)
    except services.NotEligible:
        return _plain("this email is not on the organizer's eligible-voter list", 403)
    request.session[_session_key(campaign)] = voter.ext_id
    return redirect("voting:ballot", event_ext_id=campaign.event.ext_id)


@require_http_methods(["GET"])
def results(request, event_ext_id):
    """PUBLIC per-project vote tallies -- but only after an organizer publishes AND the voting
    window has closed. Hidden results answer 404 (not 403), so an in-progress campaign discloses
    nothing about standings."""
    event = get_object_or_404(Event, ext_id=event_ext_id)
    campaign = services.get_campaign(event)
    if campaign is None or not campaign.results_visible(timezone.now()):
        raise Http404("results not published")
    return render(request, "voting/results.html",
                  {"event": event, "campaign": campaign, "tally": services.tally(campaign)})


@require_http_methods(["POST"])
def publish(request, event_ext_id):
    """Organizer-only: publish the tallies. Refused with 409 while the voting window is still open;
    after the window closes it flips results_published (atomic + audited) and redirects to the now-
    public results page."""
    event, resp = _organizer_or_response(request, event_ext_id)
    if resp is not None:
        return resp
    campaign = services.get_campaign(event)
    if campaign is None:
        raise Http404("voting is not configured for this event")
    try:
        services.publish_results(request.user, campaign)
    except services.PublishTooEarly:
        return _plain("voting is still open; results cannot be published yet", 409)
    return redirect("voting:results", event_ext_id=event.ext_id)

