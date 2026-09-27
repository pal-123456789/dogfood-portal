# src/events/admin.py
"""Admin for the event-scoping tables an organizer manages: events, memberships (roles), tracks,
teams and their members, plus the bootstrap ledger. These are ordinary configuration rows and stay
editable here, with one guard: deleting an Event, an EventMembership (a judge) or a Team whose
cascade would reach a *scored* assignment is refused, so append-only ballot history cannot be
destroyed through the admin UI (see `ScoredCascadeDeleteGuard`; THREAT-MODEL A7/A8). Tracks are
`PROTECT`ed by their submissions and need no such guard; TeamMember / BootstrapState do not cascade
into ballots."""
from django.contrib import admin

from judging.models import Ballot
from portal.admin_mixins import ScoredCascadeDeleteGuard

from .models import (
    BootstrapState,
    Event,
    EventMembership,
    Team,
    TeamMember,
    Track,
)


@admin.register(Event)
class EventAdmin(ScoredCascadeDeleteGuard, admin.ModelAdmin):
    list_display = ("ext_id", "name", "state", "submissions_close",
                    "results_published", "created_at")
    list_filter = ("state", "results_published")
    search_fields = ("ext_id", "name")

    def cascades_into_scored(self, obj):
        return Ballot.objects.filter(assignment__submission__event=obj).exists()


@admin.register(EventMembership)
class EventMembershipAdmin(ScoredCascadeDeleteGuard, admin.ModelAdmin):
    list_display = ("ext_id", "user", "event", "role")
    list_filter = ("role", "event")
    search_fields = ("ext_id", "user__email", "event__name")
    autocomplete_fields = ("user", "event")

    def cascades_into_scored(self, obj):
        return Ballot.objects.filter(assignment__judge=obj).exists()


@admin.register(Track)
class TrackAdmin(admin.ModelAdmin):
    list_display = ("ext_id", "name", "event")
    list_filter = ("event",)
    search_fields = ("ext_id", "name")
    autocomplete_fields = ("event",)


@admin.register(Team)
class TeamAdmin(ScoredCascadeDeleteGuard, admin.ModelAdmin):
    list_display = ("ext_id", "name", "event")
    list_filter = ("event",)
    search_fields = ("ext_id", "name")
    autocomplete_fields = ("event",)

    def cascades_into_scored(self, obj):
        return Ballot.objects.filter(assignment__submission__team=obj).exists()


@admin.register(TeamMember)
class TeamMemberAdmin(admin.ModelAdmin):
    list_display = ("team", "user")
    search_fields = ("team__name", "user__email")
    autocomplete_fields = ("team", "user")


@admin.register(BootstrapState)
class BootstrapStateAdmin(admin.ModelAdmin):
    list_display = ("key", "version", "created_at")
    search_fields = ("key",)
