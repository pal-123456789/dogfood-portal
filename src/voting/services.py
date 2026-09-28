# src/voting/services.py
"""Community-voting service layer: pure helpers (cost, budget, email normalization, ballot
order) plus the mutating operations, each of which co-commits its business write and a
tamper-evident audit event in ONE transaction (the same contract events/judging services follow).

The pure functions at the top take no database and are unit-tested directly (test_pure.py):
quadratic cost + budget check, Gmail-aware email normalization for duplicate detection, and the
deterministic per-voter ballot ordering.
"""
import hashlib
import uuid

from django.core.exceptions import ValidationError
from django.db import transaction
from django.utils import timezone

from audit import service as audit_service
from events.models import EventMembership
from submissions.models import Submission

from .models import (EligibleVoter, OutboundEmail, Voter, VoteAllocation, VoteToken,
                     VotingCampaign)


# --- domain errors (views map these to status codes) -----------------------------------------
class DuplicateVoter(Exception):
    """A confirmed voter with the same normalized email already exists in this campaign (-> 409)."""


class NotEligible(Exception):
    """A gated-campaign voter whose email is not on the organizer allow-list (-> 403)."""


class OverBudget(Exception):
    """The ballot's total quadratic cost exceeds the campaign credit budget (-> 409)."""


class VotingClosed(Exception):
    """A cast was attempted while the voting window was not open (-> 409)."""


class InvalidBallot(Exception):
    """A vote count was missing/negative/non-integer, or named an ineligible project (-> 400)."""


class PublishTooEarly(Exception):
    """Publish was attempted before the voting window closed (-> 409)."""


# --- pure functions (no DB; unit-tested in test_pure.py) -------------------------------------
_GMAIL_DOMAINS = {"gmail.com", "googlemail.com"}


def normalize_email(email):
    """Canonicalize an email into a duplicate-detection key. Applied in order:

      1. trim surrounding whitespace and lowercase the whole address;
      2. drop a "+tag" subaddress from the local part (plus-addressing), for EVERY domain
         -- foo+bar@example.com -> foo@example.com;
      3. for gmail.com / googlemail.com ONLY, delete dots from the local part and fold
         googlemail.com onto gmail.com, because Google routes a.b@gmail, ab@gmail and the
         @googlemail variant to one mailbox -- a.b+x@gmail.com -> ab@gmail.com.

    A value without "@" is only trimmed and lowercased. This is a dedup KEY, never used as the
    destination of an OutboundEmail, so collapsing aliases here cannot misdeliver mail.
    """
    e = (email or "").strip().lower()
    if "@" not in e:
        return e
    local, _at, domain = e.rpartition("@")
    if "+" in local:
        local = local.split("+", 1)[0]
    if domain in _GMAIL_DOMAINS:
        local = local.replace(".", "")
        domain = "gmail.com"
    return "%s@%s" % (local, domain)


def vote_cost(votes):
    """Quadratic voting cost: allocating `votes` votes to one project costs votes**2 credits."""
    return int(votes) ** 2


def total_credits(vote_counts):
    """Sum of the quadratic per-project costs for an iterable of vote counts."""
    return sum(vote_cost(v) for v in vote_counts)


def within_budget(vote_counts, budget):
    """True iff the total quadratic cost of `vote_counts` is within `budget` credits."""
    return total_credits(vote_counts) <= int(budget)


def clean_allocations(raw_map, valid_ext_ids):
    """Restrict a submitted {submission_ext_id: count} map to `valid_ext_ids`, coercing to
    positive ints and dropping zero/blank entries. Raises ValueError on a negative or
    non-integer count (mapped to 400 by the caller). Ignores any key not in valid_ext_ids, so
    a stray form field (e.g. csrfmiddlewaretoken) or an off-event submission is never counted."""
    valid = set(valid_ext_ids)
    cleaned = {}
    for ext_id in valid:
        raw = raw_map.get(ext_id, 0)
        if raw in ("", None):
            continue
        try:
            n = int(raw)
        except (TypeError, ValueError):
            raise ValueError("vote count for %s must be a whole number" % ext_id)
        if n < 0:
            raise ValueError("vote count for %s cannot be negative" % ext_id)
        if n > 0:
            cleaned[ext_id] = n
    return cleaned


def ballot_order(campaign_ext_id, voter_key, submissions):
    """Deterministic per-voter ordering of `submissions` (any list of objects with `.ext_id`).

    The sort key is sha256(campaign_ext_id | voter_key | submission.ext_id), so the order is
    STABLE for one (campaign, voter) across reloads yet DIFFERS between voters -- and is
    explicitly NOT the database id order, which would advantage low-id projects. Pure and
    side-effect free so a test can assert stability and cross-voter divergence directly.
    """
    def _key(sub):
        raw = "%s|%s|%s" % (campaign_ext_id, voter_key, sub.ext_id)
        return hashlib.sha256(raw.encode("utf-8")).hexdigest()
    return sorted(submissions, key=_key)


def _ext_id(prefix):
    """Opaque id in the project-wide shape ("<prefix>_<uuid16>"); disjoint from fixture ids."""
    return "%s_%s" % (prefix, uuid.uuid4().hex[:16])
# CHUNK_SERVICES_1


from django.utils.dateparse import parse_datetime  # noqa: E402  (grouped with the writers)


def parse_aware(raw, field):
    """Parse an ISO-8601 / datetime-local string into an aware datetime (naive values are read in
    the server timezone). Raises ValidationError with a field-named message on bad input."""
    dt = parse_datetime((raw or "").strip())
    if dt is None:
        raise ValidationError("Enter a valid %s date and time." % field)
    if timezone.is_naive(dt):
        dt = timezone.make_aware(dt, timezone.get_current_timezone())
    return dt


def is_organizer(user, event):
    """True iff `user` holds an organizer membership for THIS event (event-scoped, not a flag)."""
    return bool(user and user.is_authenticated and EventMembership.objects.filter(
        user=user, event=event, role=EventMembership.ORGANIZER).exists())


def is_judge(user, event):
    """True iff `user` is a judge of THIS event -- judges may not community-vote (-> 403)."""
    return bool(user and user.is_authenticated and EventMembership.objects.filter(
        user=user, event=event, role=EventMembership.JUDGE).exists())


def get_campaign(event):
    """The event's voting campaign, or None if voting was never configured."""
    return VotingCampaign.objects.filter(event=event).first()


def voteable_submissions(event):
    """The event's SUBMITTED projects -- the ballot's candidate set (withdrawn/draft excluded)."""
    return list(Submission.objects.filter(event=event, state=Submission.SUBMITTED)
                .select_related("track", "team"))


def configure_campaign(actor, event, *, mode, credit_budget, opens_at, closes_at,
                       eligible_emails=None, now=None):
    """Create or update the event's voting campaign (atomic + audited `campaign.configured`).

    `opens_at`/`closes_at` are raw strings (parsed here); `credit_budget` a positive int; `mode`
    one of the three declared modes. For an `email_gated` campaign the organizer allow-list is
    replaced wholesale with `eligible_emails` (deduped by normalized address), so the manage form
    round-trips the full list. Authorization (organizer-of-this-event) is the caller's job.
    """
    now = now or timezone.now()
    if mode not in dict(VotingCampaign.MODES):
        raise ValidationError("Choose a valid voting mode.")
    try:
        budget = int(credit_budget)
    except (TypeError, ValueError):
        raise ValidationError("Credit budget must be a whole number.")
    if budget <= 0:
        raise ValidationError("Credit budget must be greater than zero.")
    opens = parse_aware(opens_at, "opens-at")
    closes = parse_aware(closes_at, "closes-at")
    if closes <= opens:
        raise ValidationError("The close time must be after the open time.")

    norm_list = []
    seen = set()
    for raw in (eligible_emails or []):
        addr = (raw or "").strip()
        if not addr:
            continue
        key = normalize_email(addr)
        if key in seen:
            continue
        seen.add(key)
        norm_list.append((addr, key))

    with transaction.atomic():
        campaign = VotingCampaign.objects.filter(event=event).first()
        if campaign is None:
            campaign = VotingCampaign.objects.create(
                ext_id=_ext_id("vcmp"), event=event, mode=mode, credit_budget=budget,
                opens_at=opens, closes_at=closes)
        else:
            campaign.mode = mode
            campaign.credit_budget = budget
            campaign.opens_at = opens
            campaign.closes_at = closes
            campaign.save(update_fields=["mode", "credit_budget", "opens_at", "closes_at"])
        campaign.eligible_voters.all().delete()
        if mode == VotingCampaign.EMAIL_GATED and norm_list:
            EligibleVoter.objects.bulk_create([
                EligibleVoter(campaign=campaign, email=addr, email_normalized=key)
                for addr, key in norm_list])
        audit_service.record_event(
            event_type="campaign.configured", object_type="voting_campaign",
            object_id=campaign.ext_id, actor_user_id=getattr(actor, "pk", ""),
            occurred_at=now.isoformat(),
            payload={"event": event.ext_id, "mode": mode, "credit_budget": budget,
                     "opens_at": opens.isoformat(), "closes_at": closes.isoformat(),
                     "eligible_voters": len(norm_list)})
    return campaign
# CHUNK_SERVICES_2


from django.db.models import Count, Sum  # noqa: E402  (grouped with the read helpers below)


def request_vote_token(campaign, email, *, now=None):
    """Issue a single-use email confirm token and persist the OutboundEmail carrying its link.

    Duplicate guard (anti-abuse): if a CONFIRMED voter with the same normalized email already
    exists in this campaign, nothing is issued -- a `vote.duplicate_refused` audit event is
    recorded and DuplicateVoter is raised (-> 409). Gmail dot/plus aliases normalize together, so
    a.b+x@gmail.com cannot register a second identity behind ab@gmail.com.
    """
    now = now or timezone.now()
    addr = (email or "").strip()
    if not addr or "@" not in addr:
        raise ValidationError("Enter a valid email address.")
    key = normalize_email(addr)
    if Voter.objects.filter(campaign=campaign, email_normalized=key).exists():
        with transaction.atomic():
            audit_service.record_event(
                event_type="vote.duplicate_refused", object_type="voting_campaign",
                object_id=campaign.ext_id, occurred_at=now.isoformat(),
                payload={"event": campaign.event.ext_id, "email_normalized": key})
        raise DuplicateVoter(key)
    with transaction.atomic():
        token = VoteToken.objects.create(
            token=uuid.uuid4().hex, campaign=campaign, email=addr, email_normalized=key)
        confirm_path = "/voting/confirm/%s" % token.token
        OutboundEmail.objects.create(
            campaign=campaign, token=token, to=addr,
            subject="Confirm your community vote for %s" % campaign.event.name,
            body=("Open %s to confirm this email address and cast your community-voting "
                  "ballot. If you did not request this, you can ignore it." % confirm_path))
    return token


def confirm_token(token_obj, *, now=None):
    """Confirm an email token: create (or reuse) the Voter identity and stamp the token confirmed.

    For an `email_gated` campaign the token's normalized email must be on the organizer allow-list;
    otherwise NotEligible is raised (-> 403) and no voter is created. Idempotent: confirming again
    (or a second token for the same address) reuses the existing Voter.
    """
    now = now or timezone.now()
    campaign = token_obj.campaign
    if campaign.mode == VotingCampaign.EMAIL_GATED:
        if not EligibleVoter.objects.filter(
                campaign=campaign, email_normalized=token_obj.email_normalized).exists():
            raise NotEligible(token_obj.email_normalized)
    with transaction.atomic():
        voter, _created = Voter.objects.get_or_create(
            campaign=campaign, email_normalized=token_obj.email_normalized,
            defaults={"ext_id": _ext_id("vtr"), "email": token_obj.email})
        if token_obj.confirmed_at is None:
            token_obj.confirmed_at = now
            token_obj.save(update_fields=["confirmed_at"])
    return voter


def voter_for_user(campaign, user):
    """Get or create the authenticated voter identity for `user` in this campaign."""
    voter, _created = Voter.objects.get_or_create(
        campaign=campaign, user=user, defaults={"ext_id": _ext_id("vtr")})
    return voter


def cast_ballot(voter, campaign, *, raw_map, now=None):
    """Record `voter`'s ballot, REPLACING any prior allocations (atomic + audited `vote.cast`).

    Enforces, in order: the voting window is open (else VotingClosed -> 409); every count is a
    non-negative whole number naming an eligible SUBMITTED project (else InvalidBallot -> 400);
    the total quadratic cost is within the campaign budget (else OverBudget -> 409). On success the
    voter's whole allocation set is swapped in one transaction, so re-casting is idempotent in
    identity and the audit trail carries the final allocation.
    """
    now = now or timezone.now()
    if not campaign.is_open(now):
        raise VotingClosed()
    subs = {s.ext_id: s for s in voteable_submissions(campaign.event)}
    try:
        cleaned = clean_allocations(raw_map, subs.keys())
    except ValueError as exc:
        raise InvalidBallot(str(exc))
    if not within_budget(cleaned.values(), campaign.credit_budget):
        raise OverBudget()
    with transaction.atomic():
        voter.allocations.all().delete()
        VoteAllocation.objects.bulk_create([
            VoteAllocation(voter=voter, submission=subs[ext_id], votes=n)
            for ext_id, n in cleaned.items()])
        audit_service.record_event(
            event_type="vote.cast", object_type="voting_voter", object_id=voter.ext_id,
            actor_user_id=getattr(voter.user, "pk", "") or "", occurred_at=now.isoformat(),
            payload={"event": campaign.event.ext_id, "campaign": campaign.ext_id,
                     "allocations": {ext_id: n for ext_id, n in cleaned.items()},
                     "credits_spent": total_credits(cleaned.values())})
    return {"allocations": cleaned, "credits_spent": total_credits(cleaned.values())}


def publish_results(actor, campaign, *, now=None):
    """Publish the tallies (atomic + audited `results.publish`). Refuses while the window is still
    open (PublishTooEarly -> 409). Idempotent: re-publishing an already-published campaign is a
    no-op and emits no second audit event."""
    now = now or timezone.now()
    if now < campaign.closes_at:
        raise PublishTooEarly()
    if campaign.results_published:
        return campaign
    with transaction.atomic():
        campaign.results_published = True
        campaign.save(update_fields=["results_published"])
        audit_service.record_event(
            event_type="results.publish", object_type="voting_campaign",
            object_id=campaign.ext_id, actor_user_id=getattr(actor, "pk", ""),
            occurred_at=now.isoformat(),
            payload={"event": campaign.event.ext_id})
    return campaign


def tally(campaign):
    """Per-project community-vote totals, ranked by total votes then ext_id (a stable tie-break).

    Pure read over existing rows: `votes` is the summed vote count and `voters` the number of
    distinct voters who backed the project (one allocation row per voter-project). Includes every
    SUBMITTED project, even those with zero votes, so the board is complete.
    """
    subs = voteable_submissions(campaign.event)
    agg = (VoteAllocation.objects.filter(voter__campaign=campaign)
           .values("submission_id")
           .annotate(votes=Sum("votes"), voters=Count("id")))
    by_sub = {a["submission_id"]: a for a in agg}
    rows = []
    for s in subs:
        a = by_sub.get(s.id, {})
        rows.append({"submission": s, "votes": a.get("votes") or 0,
                     "voters": a.get("voters") or 0})
    rows.sort(key=lambda r: (-r["votes"], r["submission"].ext_id))
    for i, r in enumerate(rows, 1):
        r["rank"] = i
    return {"rows": rows,
            "total_voters": Voter.objects.filter(campaign=campaign).count(),
            "total_votes": sum(r["votes"] for r in rows)}

