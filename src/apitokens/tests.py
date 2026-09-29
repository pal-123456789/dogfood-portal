# src/apitokens/tests.py
"""DB-backed tests for the personal-API-token service layer (run: manage.py test apitokens).

They drive the real create/resolve/revoke path against the database and pin the security-critical
invariant: only the sha256 HASH of the raw token is ever persisted, the raw string is not; a good
raw resolves and stamps last_used_at, a wrong one resolves to nothing, and a revoked token stops
resolving. (The pure token primitives are also covered DB-free in tests/test_apitokens.py.) No
request-authentication path is exercised -- the reviewer wires and tests Bearer auth separately.
"""
from django.contrib.auth import get_user_model
from django.test import TestCase

from apitokens import services
from apitokens.models import ApiToken
from apitokens.tokens import hash_token

User = get_user_model()


class ApiTokenServiceTests(TestCase):
    def test_create_resolve_revoke_lifecycle(self):
        user = User.objects.create_user(email="tok@x.com", password="pw")
        token, raw = services.create_token(user, "CI token")

        # the raw is shown once and carries the scheme marker
        self.assertTrue(raw.startswith("dgf_"))
        # only the hash is persisted -- never the raw
        self.assertEqual(token.token_hash, hash_token(raw))
        self.assertNotEqual(token.token_hash, raw)
        self.assertEqual(ApiToken.objects.get(pk=token.pk).token_hash, hash_token(raw))
        self.assertFalse(ApiToken.objects.filter(token_hash=raw).exists())

        # resolve returns the row for a good raw and stamps last_used_at
        self.assertIsNone(token.last_used_at)
        resolved = services.resolve_token(raw)
        self.assertIsNotNone(resolved)
        self.assertEqual(resolved.pk, token.pk)
        self.assertIsNotNone(resolved.last_used_at)

        # a wrong secret resolves to nothing
        self.assertIsNone(services.resolve_token("wrong"))

        # after revoke, the token no longer resolves
        self.assertTrue(services.revoke_token(user, token.ext_id))
        self.assertIsNone(services.resolve_token(raw))
