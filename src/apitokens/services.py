# src/apitokens/services.py
"""Django-side service layer for personal API tokens: mint, verify, and revoke.

The raw token exists only in-process: `create_token` returns it to the caller once and stores only
its hash. `resolve_token` is the verifier called by this app's Bearer-auth layer
(apitokens.authentication.BearerTokenAuthentication), which is wired ONLY onto the /api/v1/me/
endpoint -- no middleware and no public API view authenticates. Each function performs only its one
documented DB write.
"""
import secrets

from django.utils import timezone

from .models import ApiToken
from .tokens import generate_token, hash_token, token_display_prefix


def create_token(user, name):
    """Mint a token for `user`: generate a raw string, persist a row carrying only its hash (plus a
    display prefix and a unique ext_id), and return (ApiToken, raw). The raw is returned ONCE and is
    never persisted beyond its hash."""
    raw = generate_token()
    token = ApiToken.objects.create(
        ext_id="tok_" + secrets.token_hex(8),
        user=user,
        name=name,
        prefix=token_display_prefix(raw),
        token_hash=hash_token(raw),
    )
    return token, raw


def resolve_token(raw):
    """Verifier: return the active ApiToken whose stored hash matches `raw`, stamping last_used_at;
    return None if there is no active match. (Called by BearerTokenAuthentication -- not by any view
    in this module.)"""
    token = ApiToken.objects.filter(token_hash=hash_token(raw), revoked_at__isnull=True).first()
    if token is None:
        return None
    token.last_used_at = timezone.now()
    token.save(update_fields=["last_used_at"])
    return token


def revoke_token(user, ext_id):
    """Soft-revoke the caller's OWN token by ext_id; return True iff an active token was revoked.
    Scoped to `user` so one user can never revoke another's, and gated on revoked_at IS NULL so an
    already-revoked token's original revocation time is never overwritten."""
    updated = ApiToken.objects.filter(
        user=user, ext_id=ext_id, revoked_at__isnull=True
    ).update(revoked_at=timezone.now())
    return bool(updated)
