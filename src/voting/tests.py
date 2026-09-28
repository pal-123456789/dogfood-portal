# src/voting/tests.py
"""DB-backed tests for the community-voting HTTP surface and service invariants.

These exercise the real request/response path (django.test.Client) against a migrated test
database: the three-mode gates, the quadratic budget refusal, ballot replacement, the
email-link duplicate guard, the results-hidden/publish-window transitions, and that each
mutating path writes its tamper-evident audit event. The pure functions (cost, normalization,
ballot order) are covered DB-free in test_pure.py.
"""
from datetime import timedelta

from django.contrib.auth import get_user_model
from django.test import TestCase, override_settings
from django.urls import reverse
from django.utils import timezone

from audit.models import AuditEvent
from events.models import Event, EventMembership, Team, Track
from submissions.models import Submission
from voting import services
from voting.models import (EligibleVoter, OutboundEmail, Voter, VoteAllocation, VoteToken,
                           VotingCampaign)


class VotingTestBase(TestCase):
    def setUp(self):
        User = get_user_model()
        self.org = User.objects.create_user(email="org@example.com", password="x")
        self.judge = User.objects.create_user(email="judge@example.com", password="x")
        self.alice = User.objects.create_user(email="alice@example.com", password="x")
        self.bob = User.objects.create_user(email="bob@example.com", password="x")
        self.event = Event.objects.create(
            ext_id="evt_vtest", name="Test Event", state=Event.OPEN,
            submissions_close=timezone.now() + timedelta(days=1))
        EventMembership.objects.create(user=self.org, event=self.event,
                                       role=EventMembership.ORGANIZER, ext_id="org_t")
        EventMembership.objects.create(user=self.judge, event=self.event,
                                       role=EventMembership.JUDGE, ext_id="jdg_t")
        self.track = Track.objects.create(ext_id="trk_vtest", event=self.event, name="Main")
        self.team = Team.objects.create(ext_id="tm_vtest", event=self.event, name="Team")
        self.subs = [Submission.objects.create(
            ext_id="prj_0%d" % i, event=self.event, team=self.team, track=self.track,
            title="Project %d" % i, state=Submission.SUBMITTED) for i in range(1, 7)]

    def make_campaign(self, mode=VotingCampaign.AUTHENTICATED, budget=25,
                      opens_delta=-1, closes_delta=1, eligible=None):
        now = timezone.now()
        return services.configure_campaign(
            self.org, self.event, mode=mode, credit_budget=budget,
            opens_at=(now + timedelta(hours=opens_delta)).isoformat(),
            closes_at=(now + timedelta(hours=closes_delta)).isoformat(),
            eligible_emails=eligible or [])

    def ballot_url(self):
        return reverse("voting:ballot", args=[self.event.ext_id])
class AuthenticatedModeTests(VotingTestBase):
    def test_anonymous_ballot_401(self):
        self.make_campaign()
        self.assertEqual(self.client.get(self.ballot_url()).status_code, 401)
        self.assertEqual(self.client.post(self.ballot_url(), {}).status_code, 401)

    def test_judge_forbidden_403(self):
        self.make_campaign()
        self.client.force_login(self.judge)
        self.assertEqual(self.client.get(self.ballot_url()).status_code, 403)
        self.assertEqual(self.client.post(self.ballot_url(), {}).status_code, 403)

    def test_over_budget_409_writes_nothing(self):
        self.make_campaign(budget=25)
        self.client.force_login(self.alice)
        # 5**2 + 1**2 = 26 credits, over the 25-credit budget.
        resp = self.client.post(self.ballot_url(),
                                {self.subs[0].ext_id: "5", self.subs[1].ext_id: "1"})
        self.assertEqual(resp.status_code, 409)
        self.assertFalse(VoteAllocation.objects.filter(voter__user=self.alice).exists())

    def test_valid_cast_200_and_audit(self):
        self.make_campaign(budget=25)
        self.client.force_login(self.alice)
        # 3**2 + 4**2 = 25 credits, exactly the budget.
        resp = self.client.post(self.ballot_url(),
                                {self.subs[0].ext_id: "3", self.subs[1].ext_id: "4"})
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(VoteAllocation.objects.filter(voter__user=self.alice).count(), 2)
        self.assertTrue(AuditEvent.objects.filter(event_type="vote.cast").exists())

    def test_recast_replaces_prior_ballot(self):
        self.make_campaign()
        self.client.force_login(self.alice)
        self.client.post(self.ballot_url(), {self.subs[0].ext_id: "3"})
        self.client.post(self.ballot_url(), {self.subs[1].ext_id: "2"})
        allocs = VoteAllocation.objects.filter(voter__user=self.alice)
        self.assertEqual(allocs.count(), 1)
        self.assertEqual(allocs.get().submission_id, self.subs[1].id)

    def test_json_cast_returns_credits(self):
        self.make_campaign()
        self.client.force_login(self.alice)
        resp = self.client.post(self.ballot_url(),
                                data={self.subs[0].ext_id: 2}, content_type="application/json")
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp.json()["credits_spent"], 4)


class RateLimitTests(VotingTestBase):
    @override_settings(
        CACHES={"default": {"BACKEND": "django.core.cache.backends.locmem.LocMemCache"}},
        DOGFOOD_RATE_LIMITS={"vote_write": "1/m"})
    def test_second_cast_rate_limited_429(self):
        self.make_campaign()
        self.client.force_login(self.alice)
        first = self.client.post(self.ballot_url(), {self.subs[0].ext_id: "1"})
        self.assertEqual(first.status_code, 200)
        second = self.client.post(self.ballot_url(), {self.subs[0].ext_id: "1"})
        self.assertEqual(second.status_code, 429)
        self.assertIn("Retry-After", second)
class EmailModeTests(VotingTestBase):
    def _join(self, email):
        return self.client.post(reverse("voting:join", args=[self.event.ext_id]), {"email": email})

    def _confirm(self, token):
        return self.client.post(reverse("voting:confirm", args=[token]))

    def test_email_link_join_records_outbound_without_echo(self):
        self.make_campaign(mode=VotingCampaign.EMAIL_LINK)
        resp = self._join("voter@example.com")
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(OutboundEmail.objects.count(), 1)
        # The confirm link/token is recorded, never echoed back in the response body.
        token = VoteToken.objects.get().token
        self.assertNotIn(token.encode(), resp.content)

    def test_duplicate_email_refused_409_and_audited(self):
        campaign = self.make_campaign(mode=VotingCampaign.EMAIL_LINK)
        self.assertEqual(self._join("a.b+x@gmail.com").status_code, 200)
        token = VoteToken.objects.get(campaign=campaign).token
        self.assertEqual(self._confirm(token).status_code, 302)
        self.assertTrue(Voter.objects.filter(
            campaign=campaign, email_normalized="ab@gmail.com").exists())
        # A gmail dot/plus alias of the confirmed address is a duplicate -> refused + audited.
        dup = self._join("ab@gmail.com")
        self.assertEqual(dup.status_code, 409)
        self.assertTrue(AuditEvent.objects.filter(event_type="vote.duplicate_refused").exists())

    def test_confirmed_email_voter_can_cast(self):
        self.make_campaign(mode=VotingCampaign.EMAIL_LINK)
        self._join("voter@example.com")
        self._confirm(VoteToken.objects.get().token)
        resp = self.client.post(self.ballot_url(), {self.subs[0].ext_id: "2"})
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(VoteAllocation.objects.filter(votes=2).count(), 1)

    def test_gated_ineligible_confirm_403(self):
        campaign = self.make_campaign(mode=VotingCampaign.EMAIL_GATED,
                                      eligible=["allowed@example.com"])
        self._join("outsider@example.com")
        token = VoteToken.objects.get(email="outsider@example.com").token
        self.assertEqual(self._confirm(token).status_code, 403)
        self.assertFalse(Voter.objects.filter(campaign=campaign).exists())

    def test_gated_eligible_confirm_ok(self):
        campaign = self.make_campaign(mode=VotingCampaign.EMAIL_GATED,
                                      eligible=["allowed@example.com"])
        self.assertEqual(EligibleVoter.objects.filter(campaign=campaign).count(), 1)
        self._join("allowed@example.com")
        token = VoteToken.objects.get(email="allowed@example.com").token
        self.assertEqual(self._confirm(token).status_code, 302)
        self.assertTrue(Voter.objects.filter(
            campaign=campaign, email_normalized="allowed@example.com").exists())
class ResultsWindowTests(VotingTestBase):
    def test_results_hidden_while_open_404(self):
        self.make_campaign(closes_delta=1)
        self.assertEqual(
            self.client.get(reverse("voting:results", args=[self.event.ext_id])).status_code, 404)

    def test_publish_refused_while_open_409(self):
        self.make_campaign(closes_delta=1)
        self.client.force_login(self.org)
        self.assertEqual(
            self.client.post(reverse("voting:publish", args=[self.event.ext_id])).status_code, 409)

    def test_results_hidden_after_close_until_published(self):
        campaign = self.make_campaign()
        campaign.closes_at = timezone.now() - timedelta(minutes=1)
        campaign.save(update_fields=["closes_at"])
        # Window closed but not yet published -> still hidden (404), not merely 403.
        self.assertEqual(
            self.client.get(reverse("voting:results", args=[self.event.ext_id])).status_code, 404)

    def test_publish_after_close_then_results_200_and_audit(self):
        campaign = self.make_campaign()
        campaign.closes_at = timezone.now() - timedelta(minutes=1)
        campaign.save(update_fields=["closes_at"])
        self.client.force_login(self.org)
        pub = self.client.post(reverse("voting:publish", args=[self.event.ext_id]))
        self.assertEqual(pub.status_code, 302)
        campaign.refresh_from_db()
        self.assertTrue(campaign.results_published)
        self.assertTrue(AuditEvent.objects.filter(event_type="results.publish").exists())
        self.assertEqual(
            self.client.get(reverse("voting:results", args=[self.event.ext_id])).status_code, 200)


class ManageTests(VotingTestBase):
    def manage_url(self):
        return reverse("voting:manage", args=[self.event.ext_id])

    def test_manage_anonymous_redirects_to_login(self):
        self.assertEqual(self.client.get(self.manage_url()).status_code, 302)

    def test_manage_non_organizer_403(self):
        self.client.force_login(self.alice)
        self.assertEqual(self.client.get(self.manage_url()).status_code, 403)

    def test_manage_configures_campaign_and_audits(self):
        self.client.force_login(self.org)
        now = timezone.now()
        resp = self.client.post(self.manage_url(), {
            "mode": "authenticated", "credit_budget": "16",
            "opens_at": (now - timedelta(hours=1)).strftime("%Y-%m-%dT%H:%M"),
            "closes_at": (now + timedelta(hours=1)).strftime("%Y-%m-%dT%H:%M"),
            "eligible_emails": ""})
        self.assertEqual(resp.status_code, 302)
        campaign = VotingCampaign.objects.get(event=self.event)
        self.assertEqual(campaign.credit_budget, 16)
        self.assertTrue(AuditEvent.objects.filter(event_type="campaign.configured").exists())


class BallotOrderServiceTests(VotingTestBase):
    def test_order_stable_per_voter_diverges_and_not_id_order(self):
        campaign = self.make_campaign()
        subs = services.voteable_submissions(self.event)
        id_order = [s.ext_id for s in subs]
        a1 = [s.ext_id for s in services.ballot_order(campaign.ext_id, "user:1", subs)]
        a2 = [s.ext_id for s in services.ballot_order(campaign.ext_id, "user:1", subs)]
        b = [s.ext_id for s in services.ballot_order(campaign.ext_id, "user:2", subs)]
        self.assertEqual(a1, a2)          # stable for one voter
        self.assertNotEqual(a1, b)        # differs across voters
        self.assertNotEqual(a1, id_order)  # not the database id order
        self.assertEqual(sorted(a1), sorted(id_order))  # a permutation of the same projects



