# src/apitokens/admin.py
"""Admin registration for personal API tokens.

There is no raw token to expose -- only its sha256 hash is ever stored -- and even that, along with
the display prefix and the timestamps, is shown read-only: a token is minted through
apitokens.services.create_token (which returns the raw once) and revoked through revoke_token, never
edited into existence in the admin. Tokens remain viewable and revocable, but the credential itself
is unrecoverable here by construction.
"""
from django.contrib import admin

from .models import ApiToken


@admin.register(ApiToken)
class ApiTokenAdmin(admin.ModelAdmin):
    list_display = ("name", "user", "prefix", "created_at", "last_used_at", "revoked_at")
    list_filter = ("revoked_at",)
    search_fields = ("name", "prefix")
    readonly_fields = ("token_hash", "prefix", "created_at", "last_used_at")
