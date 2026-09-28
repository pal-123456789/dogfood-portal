# src/api/throttling.py
"""A fail-open anonymous throttle for the public API.

Mirrors portal/ratelimit.py's stance: a cache outage must never take the read-only API down.
DRF's SimpleRateThrottle talks to the default cache (DatabaseCache in production); if that
backend raises -- e.g. the `dogfood_cache` table is absent, as it is under `manage.py test`
before the entrypoint's createcachetable step -- we fail OPEN (allow the request) instead of
surfacing a 500. When the cache works, throttling behaves exactly as DRF's AnonRateThrottle.
"""
from rest_framework.throttling import AnonRateThrottle


class FailOpenAnonThrottle(AnonRateThrottle):
    scope = "anon"

    def allow_request(self, request, view):
        try:
            return super().allow_request(request, view)
        except Exception:            # pragma: no cover - cache backend outage / missing table
            # Fail OPEN, exactly like portal.ratelimit: availability of a public read API beats
            # rate-limiting it, and the cache is best-effort here.
            return True
