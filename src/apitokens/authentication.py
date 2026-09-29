# src/apitokens/authentication.py
"""DRF Bearer authentication for personal API tokens.

Additive and OPT-IN. When a request carries no `Authorization: Bearer ...` header this returns
None, so DRF treats the caller as anonymous and the public AllowAny endpoints behave exactly as
before -- adding this class changes nothing a public endpoint returns. Only when a Bearer scheme IS
present do we verify it: a good, non-revoked token authenticates its owning user (and stamps
last_used_at via services.resolve_token); a malformed, unknown, or revoked one is a hard 401. The
raw token is never stored or logged -- resolve_token matches on its sha256 hash -- and an inactive
user is refused even with a valid token.

Mirrors the idiom of rest_framework.authentication.TokenAuthentication (return None to defer,
raise AuthenticationFailed to reject) so behavior is unsurprising to a reviewer.
"""
from rest_framework import authentication, exceptions

from . import services

KEYWORD = "bearer"


class BearerTokenAuthentication(authentication.BaseAuthentication):
    """Authenticate `Authorization: Bearer <raw-token>` against apitokens.services.resolve_token."""

    def authenticate(self, request):
        header = authentication.get_authorization_header(request).split()
        if not header or header[0].lower() != KEYWORD.encode():
            return None  # no Bearer scheme -> defer; AllowAny endpoints stay anonymous as before
        if len(header) == 1:
            raise exceptions.AuthenticationFailed("Invalid bearer header: no credentials provided.")
        if len(header) > 2:
            raise exceptions.AuthenticationFailed("Invalid bearer header: token may not contain spaces.")
        try:
            raw = header[1].decode()
        except UnicodeError:
            raise exceptions.AuthenticationFailed("Invalid bearer header: token is not valid UTF-8.")
        token = services.resolve_token(raw)
        if token is None:
            raise exceptions.AuthenticationFailed("Invalid or revoked API token.")
        if not token.user.is_active:
            raise exceptions.AuthenticationFailed("User inactive or deleted.")
        return (token.user, token)  # DRF sets request.user and request.auth (the ApiToken)

    def authenticate_header(self, request):
        # Drives the WWW-Authenticate header on a 401 so clients learn the scheme.
        return "Bearer"


# drf-spectacular auto-discovers this when the module is imported (api.views imports the auth
# class), so /api/v1/docs shows a "bearer" HTTP security scheme instead of emitting an
# "unable to resolve authenticator" warning at schema-build time. Documentation only.
try:
    from drf_spectacular.extensions import OpenApiAuthenticationExtension

    class _BearerScheme(OpenApiAuthenticationExtension):
        target_class = "apitokens.authentication.BearerTokenAuthentication"
        name = "BearerToken"

        def get_security_definition(self, auto_schema):
            return {"type": "http", "scheme": "bearer"}
except ImportError:  # drf-spectacular is a project dependency; guard only so import never hard-fails
    pass
