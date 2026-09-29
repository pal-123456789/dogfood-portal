# src/apitokens/tokens.py
"""Pure token primitives for personal API (Bearer) tokens -- NO django import, so this module is
importable and unit-testable without a database or settings (tests/test_apitokens.py drives it
directly under PYTHONPATH=src).

The scheme is deliberately simple and one-way: `generate_token` mints a RAW bearer string shown to
the user exactly once; `hash_token` reduces it to a sha256 hex digest, which is the ONLY form ever
persisted (see apitokens.models / services). A raw token is therefore unrecoverable from the
database -- a leaked dump exposes hashes, not usable credentials. `token_display_prefix` yields a
short, non-secret label so a token can be identified in a listing without revealing it.
"""
import hashlib
import secrets

TOKEN_PREFIX = "dgf_"          # visible scheme marker on the raw token


def generate_token() -> str:
    """Return a fresh RAW token (prefix + 256 bits of urlsafe entropy). Shown to the user ONCE;
    only its hash is stored."""
    return TOKEN_PREFIX + secrets.token_urlsafe(32)


def hash_token(raw: str) -> str:
    """sha256 hex of the raw token -- the ONLY form persisted. Deterministic, so a presented raw
    can be matched against the stored hash without ever storing the raw."""
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def token_display_prefix(raw: str, n: int = 12) -> str:
    """First `n` chars of the raw token: a short, non-secret identifier for listings."""
    return raw[:n]
