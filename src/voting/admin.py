# src/voting/admin.py
"""Admin registrations for community voting. Campaigns and allow-list entries are editable for
organizer housekeeping; the vote-bearing tables (Voter, VoteAllocation) and the OutboundEmail log
are read-only in admin, so a tally or an audit-linked email record cannot be silently rewritten
through the admin UI."""
from django.contrib import admin

from portal.admin_mixins import ReadOnlyModelAdmin

from .models import (EligibleVoter, OutboundEmail, Voter, VoteAllocation, VoteToken,
                     VotingCampaign)


@admin.register(VotingCampaign)
class VotingCampaignAdmin(admin.ModelAdmin):
    list_display = ("ext_id", "event", "mode", "credit_budget", "opens_at", "closes_at",
                    "results_published")
    list_filter = ("mode", "results_published")
    search_fields = ("ext_id", "event__ext_id", "event__name")
    autocomplete_fields = ("event",)


@admin.register(EligibleVoter)
class EligibleVoterAdmin(admin.ModelAdmin):
    list_display = ("campaign", "email", "email_normalized", "created_at")
    search_fields = ("email", "email_normalized")


@admin.register(VoteToken)
class VoteTokenAdmin(ReadOnlyModelAdmin):
    list_display = ("token", "campaign", "email", "confirmed_at", "created_at")
    search_fields = ("token", "email", "email_normalized")


@admin.register(Voter)
class VoterAdmin(ReadOnlyModelAdmin):
    list_display = ("ext_id", "campaign", "user", "email_normalized", "created_at")
    search_fields = ("ext_id", "email", "email_normalized")


@admin.register(VoteAllocation)
class VoteAllocationAdmin(ReadOnlyModelAdmin):
    list_display = ("voter", "submission", "votes", "created_at")
    search_fields = ("voter__ext_id", "submission__ext_id", "submission__title")


@admin.register(OutboundEmail)
class OutboundEmailAdmin(ReadOnlyModelAdmin):
    list_display = ("to", "subject", "campaign", "created_at")
    search_fields = ("to", "subject", "body")
