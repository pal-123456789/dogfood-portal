# src/events/admin.py
"""Admin for the event-scoping tables an organizer manages: events, memberships (roles), tracks,
teams and their members, plus the bootstrap ledger. These are ordinary configuration rows, so they
are fully editable here."""
from django.contrib import admin

from .models import (
    BootstrapState,
    Event,
    EventMembership,
    Team,
    TeamMember,
    Track,
)


@admin.register(Event)
class EventAdmin(admin.ModelAdmin):
    list_display = ("ext_id", "name", "state", "submissions_close",
                    "results_published", "created_at")
    list_filter = ("state", "results_published")
    search_fields = ("ext_id", "name")


@admin.register(EventMembership)
class EventMembershipAdmin(admin.ModelAdmin):
    list_display = ("ext_id", "user", "event", "role")
    list_filter = ("role", "event")
    search_fields = ("ext_id", "user__email", "event__name")
    autocomplete_fields = ("user", "event")


@admin.register(Track)
class TrackAdmin(admin.ModelAdmin):
    list_display = ("ext_id", "name", "event")
    list_filter = ("event",)
    search_fields = ("ext_id", "name")
    autocomplete_fields = ("event",)


@admin.register(Team)
class TeamAdmin(admin.ModelAdmin):
    list_display = ("ext_id", "name", "event")
    list_filter = ("event",)
    search_fields = ("ext_id", "name")
    autocomplete_fields = ("event",)


@admin.register(TeamMember)
class TeamMemberAdmin(admin.ModelAdmin):
    list_display = ("team", "user")
    search_fields = ("team__name", "user__email")
    autocomplete_fields = ("team", "user")


@admin.register(BootstrapState)
class BootstrapStateAdmin(admin.ModelAdmin):
    list_display = ("key", "version", "created_at")
    search_fields = ("key",)
