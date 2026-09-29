# src/apitokens/models.py
"""Personal API token model (T4 feature), additive under its own `api_token` table.

A token authenticates a single user for programmatic (Bearer) access. Only the sha256 HASH of the
raw token is stored (`token_hash`) -- the raw string is returned once at creation by
apitokens.services.create_token and never persisted, so a database dump exposes hashes, not usable
credentials. `prefix` is the non-secret leading slice kept purely so a user can recognise a token in
a listing; `last_used_at` is stamped by the verifier; `revoked_at` soft-revokes without deleting the
row (so history survives). No existing table is touched.

Request-authentication lives in apitokens.authentication.BearerTokenAuthentication, which calls
apitokens.services.resolve_token; it is wired ONLY onto the authenticated /api/v1/me/ endpoint, so
no existing public API route is affected.
"""
from django.conf import settings
from django.db import models


class ApiToken(models.Model):
    ext_id = models.CharField(max_length=64, unique=True)
    user = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.CASCADE,
                             related_name="api_tokens")
    name = models.CharField(max_length=120)                    # human label
    prefix = models.CharField(max_length=12)                   # first chars of the raw token (display only)
    token_hash = models.CharField(max_length=64, unique=True)  # sha256 hex of the raw token; raw NEVER stored
    created_at = models.DateTimeField(auto_now_add=True)
    last_used_at = models.DateTimeField(null=True, blank=True)
    revoked_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        db_table = "api_token"
        ordering = ["-created_at"]

    @property
    def is_active(self) -> bool:
        return self.revoked_at is None

    def __str__(self):
        return f"{self.name} ({self.prefix}…)"
