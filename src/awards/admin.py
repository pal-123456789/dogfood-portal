# src/awards/admin.py
from django.contrib import admin

from .models import Prize


@admin.register(Prize)
class PrizeAdmin(admin.ModelAdmin):
    list_display = ("ext_id", "event", "track", "name", "position", "awarded_submission",
                    "created_at")
    list_filter = ("position",)
    search_fields = ("ext_id", "name", "event__ext_id", "event__name", "track__ext_id")
    raw_id_fields = ("event", "track", "awarded_submission")
    readonly_fields = ("created_at",)
