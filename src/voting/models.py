# src/voting/models.py
"""Community-voting data model (T3 feature), additive under its own tables.

A `VotingCampaign` turns on community voting for one event. Beyond the organizer-configured
campaign, everything here records WHO voted and HOW MUCH, so the tally is reproducible and the
per-voter identity is enforced in the database rather than trusted from a request:

  * quadratic budget -- a voter spends `credit_budget` credits and paying N votes to a project
    costs N**2, so spreading support is cheap and piling onto one project is expensive;
  * three entry modes -- an authenticated portal user, an email magic-link, or an email
    magic-link restricted to an organizer allow-list;
  * one identity per voter -- UNIQUE(campaign, user) for authenticated voters and
    UNIQUE(campaign, email_normalized) for email voters (the normalized key folds Gmail dot/plus
    aliases together, see services.normalize_email), so an alias cannot vote twice.

Roles stay event-scoped (events.EventMembership); nothing here adds a global flag to the user.
Every write that matters is paired with a tamper-evident audit event in voting.services, never
here. All tables are prefixed `voting_` and no existing table is touched.
"""
from django.conf import settings
from django.db import models

from events.models import Event
from submissions.models import Submission


class VotingCampaign(models.Model):
    """Per-event community-voting configuration. One campaign per event (OneToOne).

    `credit_budget` is the quadratic budget each voter may spend. The window [opens_at, closes_at)
    controls when ballots are accepted and gates result publication: results stay hidden until an
    organizer publishes AND the window has closed (see services.tally / views.results).
    """
    AUTHENTICATED, EMAIL_LINK, EMAIL_GATED = "authenticated", "email_link", "email_gated"
    MODES = [(AUTHENTICATED, "authenticated"), (EMAIL_LINK, "email link"),
             (EMAIL_GATED, "email gated")]

    ext_id = models.CharField(max_length=64, unique=True)
    event = models.OneToOneField(Event, on_delete=models.CASCADE, related_name="voting_campaign")
    mode = models.CharField(max_length=16, choices=MODES, default=AUTHENTICATED)
    credit_budget = models.PositiveIntegerField(default=25)
    opens_at = models.DateTimeField()
    closes_at = models.DateTimeField()
    results_published = models.BooleanField(default=False)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        db_table = "voting_campaign"

    def __str__(self):
        return "voting:%s" % self.event_id

    def is_open(self, now):
        """True iff `now` is within the voting window [opens_at, closes_at)."""
        return self.opens_at <= now < self.closes_at

    def is_closed(self, now):
        """True iff the voting window has ended (now >= closes_at)."""
        return now >= self.closes_at

    def results_visible(self, now):
        """Results are public only once an organizer has published AND the window has closed."""
        return self.results_published and self.is_closed(now)
class EligibleVoter(models.Model):
    """Organizer allow-list entry for an `email_gated` campaign. A confirmed email voter whose
    normalized address is not on this list is refused at confirm time (403). `email_normalized`
    is the dedup key (services.normalize_email), UNIQUE per campaign."""
    campaign = models.ForeignKey(VotingCampaign, on_delete=models.CASCADE,
                                 related_name="eligible_voters")
    email = models.EmailField()
    email_normalized = models.CharField(max_length=254)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        db_table = "voting_eligible_voter"
        constraints = [
            models.UniqueConstraint(fields=["campaign", "email_normalized"],
                                    name="uniq_eligible_campaign_email"),
        ]

    def __str__(self):
        return "%s@campaign:%s" % (self.email_normalized, self.campaign_id)


class VoteToken(models.Model):
    """A single-use email magic-link token (email_link / email_gated modes).

    Requesting to vote by email mints one of these and writes an OutboundEmail carrying the confirm
    URL; visiting /voting/confirm/<token> confirms it, creates (or reuses) the Voter identity, and
    stores that identity in the session. `token` is an unguessable random hex string, not derived
    from the email, so possession of the link is the proof of address control."""
    token = models.CharField(max_length=64, unique=True)
    campaign = models.ForeignKey(VotingCampaign, on_delete=models.CASCADE, related_name="tokens")
    email = models.EmailField()
    email_normalized = models.CharField(max_length=254)
    created_at = models.DateTimeField(auto_now_add=True)
    confirmed_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        db_table = "voting_token"

    def __str__(self):
        return "token:%s" % self.token


class Voter(models.Model):
    """A confirmed voting identity within one campaign.

    Exactly one of two shapes: an authenticated portal user (`user` set, email fields blank) or an
    email voter (`user` null, `email_normalized` set). The two partial UNIQUE constraints below are
    the real "one vote per person" guard -- UNIQUE(campaign, user) for authenticated voters and
    UNIQUE(campaign, email_normalized) for email voters -- enforced by the database, not the view.
    """
    ext_id = models.CharField(max_length=64, unique=True)
    campaign = models.ForeignKey(VotingCampaign, on_delete=models.CASCADE, related_name="voters")
    user = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.CASCADE,
                             null=True, blank=True, related_name="voting_identities")
    email = models.EmailField(blank=True, default="")
    email_normalized = models.CharField(max_length=254, blank=True, default="")
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        db_table = "voting_voter"
        constraints = [
            models.UniqueConstraint(fields=["campaign", "user"],
                                    condition=models.Q(user__isnull=False),
                                    name="uniq_voter_campaign_user"),
            models.UniqueConstraint(fields=["campaign", "email_normalized"],
                                    condition=~models.Q(email_normalized=""),
                                    name="uniq_voter_campaign_email"),
        ]

    def __str__(self):
        return self.ext_id


class VoteAllocation(models.Model):
    """One voter's vote count for one submission. Cost is quadratic (votes**2 credits); the sum of
    costs across a voter's allocations must stay within the campaign budget (services.cast_ballot).
    Re-casting replaces a voter's whole set, so UNIQUE(voter, submission) holds at all times."""
    voter = models.ForeignKey(Voter, on_delete=models.CASCADE, related_name="allocations")
    submission = models.ForeignKey(Submission, on_delete=models.CASCADE,
                                   related_name="vote_allocations")
    votes = models.PositiveIntegerField()
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        db_table = "voting_allocation"
        constraints = [
            models.UniqueConstraint(fields=["voter", "submission"],
                                    name="uniq_allocation_voter_submission"),
        ]

    def __str__(self):
        return "%s->%s=%d" % (self.voter_id, self.submission_id, self.votes)


class OutboundEmail(models.Model):
    """A record of an email the app WOULD send (the confirm magic-link). This deployment does not
    ship an SMTP integration, so the message is persisted here (and surfaced to the organizer /
    admin) rather than delivered -- honest about what actually happens. Never contains a secret
    beyond the single-use confirm token already in the link."""
    campaign = models.ForeignKey(VotingCampaign, on_delete=models.CASCADE, related_name="emails")
    token = models.ForeignKey(VoteToken, on_delete=models.SET_NULL, null=True, blank=True,
                              related_name="emails")
    to = models.EmailField()
    subject = models.CharField(max_length=200)
    body = models.TextField()
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        db_table = "voting_outbound_email"
        ordering = ["-id"]

    def __str__(self):
        return "%s: %s" % (self.to, self.subject)
