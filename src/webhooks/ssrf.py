# src/webhooks/ssrf.py
"""SSRF defense for organizer-registered outbound webhook URLs -- DB-free and, by injecting the
resolver, network-free unit-testable.

`validate_url` is the single gate an untrusted URL must pass before we ever open a socket to it.
What it enforces, precisely:

  * scheme must be http or https (anything else -- ftp:, file:, gopher:, a schemeless value --
    is rejected);
  * embedded credentials (user:pass@host) are rejected;
  * a hostname must be present;
  * if the host is an IP LITERAL it is checked directly; if it is a NAME it is resolved to ALL of
    its addresses and EVERY one must pass -- a single blocked address rejects the whole URL, so a
    name that points inside (DNS rebinding / split-horizon) cannot slip through;
  * an address is BLOCKED when it is loopback, private (RFC1918 + IPv6 ULA fc00::/7), link-local
    (incl. the 169.254.169.254 cloud-metadata endpoint), reserved, multicast, or unspecified;
    IPv4-mapped IPv6 (::ffff:a.b.c.d) is unwrapped and its v4 form checked too.

This is a strong reduction of SSRF risk, not an absolute one: name resolution here and the actual
connection in services._http_post are two moments, so services._http_post re-validates immediately
before connecting to shrink that resolve-then-connect window. It does not proxy-pierce, and a
public host that itself forwards internally is out of scope.
"""
from __future__ import annotations

import ipaddress
import socket
from urllib.parse import urlsplit

_ALLOWED_SCHEMES = {"http", "https"}

# Cloud metadata endpoints, blocked EXPLICITLY as well as by their range (169.254.0.0/16 is
# link-local; fd00:ec2::254 is ULA), so the intent is legible and survives a future range tweak.
_METADATA_IPS = {"169.254.169.254", "fd00:ec2::254"}


class SsrfError(Exception):
    """A URL was refused by the SSRF gate. `.reason` is a short human-readable cause."""

    def __init__(self, reason: str):
        super().__init__(reason)
        self.reason = reason


def _blocked_flags(ip: ipaddress._BaseAddress) -> bool:
    return bool(ip.is_loopback or ip.is_private or ip.is_link_local
                or ip.is_reserved or ip.is_multicast or ip.is_unspecified)


def is_blocked_ip(ip_str: str) -> bool:
    """True iff `ip_str` is not a safe public destination (or is not a parseable IP at all)."""
    try:
        ip = ipaddress.ip_address(ip_str)
    except ValueError:
        return True  # not a literal IP -> cannot confirm it is public; treat as blocked
    if str(ip) in _METADATA_IPS:
        return True
    if ip.version == 6 and ip.ipv4_mapped is not None:
        mapped = ip.ipv4_mapped
        if str(mapped) in _METADATA_IPS or _blocked_flags(mapped):
            return True
    return _blocked_flags(ip)


def _default_resolver(host: str) -> list[str]:
    """Resolve `host` to a list of IP strings via getaddrinfo. The ONE network touch in this module;
    injected as `resolver` in tests so no real DNS is ever performed there."""
    infos = socket.getaddrinfo(host, None)
    return [info[4][0] for info in infos]


def validate_url(url: str, *, resolver=None) -> list[str]:
    """Validate `url` for outbound delivery. Returns the list of allowed resolved IPs, or raises
    SsrfError. `resolver(host) -> list[str]` is injectable so this stays network-free under test."""
    parts = urlsplit(url)
    scheme = (parts.scheme or "").lower()
    if scheme not in _ALLOWED_SCHEMES:
        raise SsrfError("scheme %r is not allowed (only http/https)" % scheme)
    if parts.username or parts.password:
        raise SsrfError("credentials embedded in the URL are not allowed")
    host = parts.hostname
    if not host:
        raise SsrfError("the URL has no host")

    try:
        literal = ipaddress.ip_address(host)
    except ValueError:
        literal = None
    if literal is not None:
        if is_blocked_ip(host):
            raise SsrfError("host IP %s is in a blocked range" % host)
        return [str(literal)]

    resolve = resolver or _default_resolver
    try:
        addrs = [a for a in (resolve(host) or []) if a]
    except SsrfError:
        raise
    except Exception as exc:  # DNS failure, bad host, etc. -- fail closed
        raise SsrfError("could not resolve host %r: %s" % (host, exc))
    if not addrs:
        raise SsrfError("host %r did not resolve to any address" % host)
    for a in addrs:
        if is_blocked_ip(a):
            raise SsrfError("host %r resolves to blocked address %s" % (host, a))
    return addrs
