# tests/test_ratelimit.py
"""Pure invariants of the fixed-window limiter (src/portal/ratelimit.py).

DB-free, like the other tests in this directory: it drives a throwaway LocMemCache and a frozen
clock, so pytest never needs a database and the CI gate stays fast. The fail-open contract -- the
one property that keeps a cache outage from becoming an outage for users -- is asserted directly.
"""
import pytest
from django.core.cache.backends.locmem import LocMemCache

from portal import ratelimit


def _cache():
    return LocMemCache("rl-test-%d" % id(object()), {})


def test_parse_rate_units():
    assert ratelimit.parse_rate("10/m") == (10, 60)
    assert ratelimit.parse_rate("1/s") == (1, 1)
    assert ratelimit.parse_rate("120/h") == (120, 3600)
    assert ratelimit.parse_rate("5/d") == (5, 86400)


@pytest.mark.parametrize("bad", ["0/m", "-1/h", "10", "10/y", "abc/m", "", "/m", "10/"])
def test_parse_rate_rejects_garbage(bad):
    with pytest.raises(ValueError):
        ratelimit.parse_rate(bad)


def test_allows_up_to_limit_then_blocks_within_window():
    c, now = _cache(), 1000.0
    assert [ratelimit.hit("k", "3/m", cache=c, now=now)[0] for _ in range(3)] == [True, True, True]
    allowed, retry = ratelimit.hit("k", "3/m", cache=c, now=now)
    assert allowed is False
    assert 1 <= retry <= 60


def test_window_rollover_resets_counter():
    c = _cache()
    for _ in range(3):
        ratelimit.hit("k", "3/m", cache=c, now=1000.0)
    assert ratelimit.hit("k", "3/m", cache=c, now=1000.0)[0] is False
    # 60s later is the next fixed window -- a clean slate.
    assert ratelimit.hit("k", "3/m", cache=c, now=1060.0)[0] is True


def test_keys_are_independent():
    c = _cache()
    for _ in range(3):
        ratelimit.hit("a", "3/m", cache=c, now=1000.0)
    assert ratelimit.hit("a", "3/m", cache=c, now=1000.0)[0] is False
    assert ratelimit.hit("b", "3/m", cache=c, now=1000.0)[0] is True


class _BrokenCache:
    def add(self, *a, **k):
        raise RuntimeError("cache down")

    def incr(self, *a, **k):
        raise RuntimeError("cache down")


def test_fails_open_when_cache_errors():
    # A limiter outage must not deny service: allowed=True, no Retry-After.
    allowed, retry = ratelimit.hit("k", "1/m", cache=_BrokenCache(), now=1000.0)
    assert allowed is True
    assert retry == 0
