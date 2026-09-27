# src/portal/ratelimit.py
"""A tiny fixed-window rate limiter over the SHARED cache (DatabaseCache in the running stack).

Why the shared cache and not a per-process counter: with DOGFOOD_WORKERS=2 a per-process
"10/min" is really 20/min, because each gunicorn worker keeps its own tally. The DB cache is
the one counter every worker increments, so the limit is a property of the deployment, not of
a process. See settings.CACHES (LOCATION "dogfood_cache") and THREAT-MODEL.md.

Two deliberate properties:

* FAIL OPEN. If the cache backend raises -- most importantly when the cache table is absent, as
  it is under `manage.py test` (the table is created by `createcachetable` in the entrypoint, not
  by a migration) -- the limiter ALLOWS the request. A limiter outage must never become an
  availability outage; this is abuse control, not an authorization boundary. The real access
  rules are the event-scoped grants, which do not fail open.

* BEST EFFORT under concurrency. DatabaseCache.incr is not strictly atomic across workers, so a
  burst can leak a few requests over the nominal limit. That is acceptable for slowing credential
  stuffing and write floods; it is not a hard quota.

The window counter lives at `rl:{key}:{window}` with a TTL of the window length, so it expires on
its own -- no sweeper, no migration, nothing to clean up.
"""
from __future__ import annotations

import time

from django.core.cache import caches

_UNITS = {"s": 1, "m": 60, "h": 3600, "d": 86400}


def parse_rate(token):
    """'N/unit' -> (count, seconds). unit is one of s/m/h/d. Raises ValueError on a bad token.

    The count must be a positive integer; "0/m" and "-1/h" are rejected rather than silently
    treated as "block everything", which would be a foot-gun in a config file.
    """
    count, sep, unit = token.partition("/")
    if not sep or unit not in _UNITS:
        raise ValueError("bad rate %r: expected 'N/s|m|h|d'" % (token,))
    n = int(count)                       # ValueError on non-numeric -- intended
    if n <= 0:
        raise ValueError("bad rate %r: count must be positive" % (token,))
    return n, _UNITS[unit]


def hit(key, token, *, cache=None, now=None):
    """Count one event against `key` under rate `token`. Returns (allowed, retry_after_seconds).

    Fixed window: the window index is floor(now / seconds); each window has its own counter with
    a TTL equal to the window length. `cache` and `now` are injectable so the pure invariants can
    be tested DB-free with a throwaway LocMemCache and a frozen clock.
    """
    n, seconds = parse_rate(token)
    c = cache if cache is not None else caches["default"]
    t = time.time() if now is None else now
    window = int(t // seconds)
    ckey = "rl:%s:%d" % (key, window)
    try:
        # add() only writes when the key is absent, so the first hit of a window sets the TTL;
        # incr() is then a plain increment on an existing integer.
        c.add(ckey, 0, timeout=seconds)
        current = c.incr(ckey)
    except Exception:
        return True, 0                   # fail open: never turn a cache outage into a denial
    if current > n:
        retry = seconds - int(t % seconds)
        return False, max(retry, 1)
    return True, 0
