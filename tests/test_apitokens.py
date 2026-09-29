# tests/test_apitokens.py
"""Pure, DB-free tests for the personal-API-token primitives (src/apitokens/tokens.py).

DB-free on purpose, like test_recusal.py: tokens.py imports no django, so this runs under
`PYTHONPATH=src python3 tests/test_apitokens.py` with no database or settings. They pin the one-way
scheme #131 relies on -- every mint is distinct and prefixed, the stored form is exactly
sha256(raw) (deterministic, 64-hex, collision-free across distinct raws), and the display prefix is
the first n chars -- so the DB never has to hold a recoverable secret. Prints a PASS line; any failed
assertion propagates and exits nonzero.
"""
import hashlib

from apitokens.tokens import (TOKEN_PREFIX, generate_token, hash_token,
                              token_display_prefix)


# --- generate_token --------------------------------------------------------------------------
def test_generate_token_is_prefixed():
    t = generate_token()
    assert t.startswith("dgf_")
    assert t.startswith(TOKEN_PREFIX)


def test_generate_token_is_long():
    # "dgf_" + secrets.token_urlsafe(32) -> ~47 chars of entropy-bearing string
    assert len(generate_token()) > 40


def test_generate_token_is_distinct_each_call():
    seen = {generate_token() for _ in range(100)}
    assert len(seen) == 100                       # no repeats across 100 mints


# --- hash_token ------------------------------------------------------------------------------
def test_hash_token_matches_direct_sha256():
    raw = "dgf_example-raw-token"
    assert hash_token(raw) == hashlib.sha256(raw.encode("utf-8")).hexdigest()


def test_hash_token_is_deterministic_and_hex64():
    raw = generate_token()
    assert hash_token(raw) == hash_token(raw)     # stable
    h = hash_token(raw)
    assert len(h) == 64                            # sha256 hex digest
    assert all(c in "0123456789abcdef" for c in h)


def test_different_raws_hash_differently():
    a, b = generate_token(), generate_token()
    assert a != b
    assert hash_token(a) != hash_token(b)


# --- token_display_prefix --------------------------------------------------------------------
def test_token_display_prefix_length_is_n():
    raw = generate_token()
    assert token_display_prefix(raw) == raw[:12]   # default n == 12
    assert len(token_display_prefix(raw)) == 12
    assert token_display_prefix(raw, 6) == raw[:6]
    assert len(token_display_prefix(raw, 6)) == 6


def _run():
    fns = [v for k, v in sorted(globals().items())
           if k.startswith("test_") and callable(v)]
    for f in fns:
        f()
    print("test_apitokens: %d passed -- PASS" % len(fns))


if __name__ == "__main__":
    _run()
