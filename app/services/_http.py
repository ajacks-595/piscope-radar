"""Shared HTTP client + LRU cache helpers for enrichment services.

A single long-lived httpx.AsyncClient per enrichment service avoids repeatedly opening
TLS/TCP connections (each lookup against hexdb/adsbdb/planespotters was previously a fresh
handshake). Caches are bounded so a long-running Pi instance can't grow without limit.
"""
from __future__ import annotations

import asyncio
import ipaddress
import socket
import time
from collections import OrderedDict
from typing import Any, Optional, Union
from urllib.parse import urlparse

import httpx

from . import settings as settings_store


# Single client per process — keep-alive pools are reused across lookups.
_CLIENT_LOCK = asyncio.Lock()
_CLIENT: Optional[httpx.AsyncClient] = None


# --- SSRF guard -------------------------------------------------------------
# Lives here (a neutral outbound-HTTP module) rather than in feed.py so both
# feed.py and webhooks.py can use it without an import cycle.

_IP = Union[ipaddress.IPv4Address, ipaddress.IPv6Address]

# Block link-local + the cloud instance-metadata addresses. Other private
# ranges (10.x / 192.168.x / 172.16.x) are deliberately ALLOWED — the user
# points PiScope at LAN tar1090 hosts and LAN webhook receivers on purpose.
#
# Hostname forms that always resolve to metadata (so a literal-IP check alone
# would miss them):
_BLOCKED_METADATA_HOSTS = {"metadata.google.internal"}
# Literal metadata IPs we always refuse, however they were encoded in the URL.
# 169.254.0.0/16 is also caught by the is_link_local check below; 100.100.100.200
# (Alibaba) is NOT link-local, so it must be listed explicitly.
_BLOCKED_METADATA_IPS = frozenset({
    ipaddress.ip_address("169.254.169.254"),   # AWS / GCP / Azure IMDS (also link-local)
    ipaddress.ip_address("100.100.100.200"),   # Alibaba Cloud metadata
    ipaddress.ip_address("fd00:ec2::254"),      # AWS IPv6 IMDS
})


# IPv6 forms that carry an IPv4 address the kernel (or a NAT64/6to4 gateway) will
# actually route to. Each embedded address is checked like a plain IPv4 literal.
_NAT64_PREFIXES = (
    ipaddress.ip_network("64:ff9b::/96"),     # RFC 6052 well-known NAT64 prefix
    ipaddress.ip_network("64:ff9b:1::/48"),   # RFC 8215 local-use NAT64
)


def _embedded_ipv4(ip: _IP) -> Optional[ipaddress.IPv4Address]:
    """The IPv4 address hidden inside an IPv6 literal, if any."""
    if not isinstance(ip, ipaddress.IPv6Address):
        return None
    if ip.ipv4_mapped is not None:            # ::ffff:a.b.c.d — connects to a.b.c.d
        return ip.ipv4_mapped
    if ip.sixtofour is not None:              # 2002:aabb:ccdd::/48
        return ip.sixtofour
    for net in _NAT64_PREFIXES:
        if ip in net:
            return ipaddress.IPv4Address(int(ip) & 0xFFFFFFFF)
    n = int(ip)
    if 1 < n <= 0xFFFFFFFF:                   # deprecated IPv4-compatible ::a.b.c.d
        return ipaddress.IPv4Address(n)
    return None


def _ip_block_reason(ip: _IP) -> Optional[str]:
    """Why this literal IP is refused, or None if it's allowed.

    IPv4 addresses embedded in IPv6 (``::ffff:169.254.169.254`` and friends) are
    unwrapped and checked recursively. Python < 3.13 does not classify a mapped
    link-local address as link-local, and the mapped Alibaba metadata address is
    in no stdlib category on any version, so both reached httpx before
    (2026-11-08 P1-2). Unspecified (0.0.0.0, ::) and multicast are refused too:
    neither is a legitimate fetch target. Loopback and private ranges stay
    allowed on purpose (local tar1090, LAN webhook receivers)."""
    inner = _embedded_ipv4(ip)
    if inner is not None:
        reason = _ip_block_reason(inner)
        if reason:
            return f"{reason} (via {ip})"
    if ip.is_unspecified:
        return "unspecified"
    if ip.is_multicast:
        return "multicast"
    if ip.is_link_local:
        return "link-local"
    if ip in _BLOCKED_METADATA_IPS:
        return "cloud-metadata"
    return None


def _parse_ip_literal(host: str) -> Optional[_IP]:
    """Parse `host` as an IP literal if it is one — INCLUDING the non-dotted
    IPv4 encodings (decimal `2852039166`, hex `0xA9FEA9FE`, octal) that the libc
    resolver still honours but `ipaddress.ip_address(str)` rejects. Without this,
    `http://2852039166/` slips past the literal check and httpx then resolves it
    to 169.254.169.254. Returns None for genuine hostnames."""
    try:
        return ipaddress.ip_address(host)
    except ValueError:
        pass
    # socket.inet_aton accepts the legacy numeric IPv4 forms the resolver uses;
    # it raises OSError for anything that isn't one (i.e. a real hostname).
    try:
        packed = socket.inet_aton(host)
    except OSError:
        return None
    return ipaddress.ip_address(packed)


def validate_external_url(raw: str, *, resolve: bool = False) -> str:
    """Return a sanitised http(s) URL, or raise ValueError. SSRF guard for any
    user-supplied URL the server is about to fetch.

    Always: rejects non-http(s) schemes, the cloud-metadata hostnames, and
    link-local / metadata literal IPs (in dotted OR numeric-encoded form).

    When ``resolve=True``: also DNS-resolves the hostname and rejects it if any
    returned address is link-local or a metadata address. This closes the
    "innocuous domain whose A record points at 169.254.169.254" rebinding
    vector. Enable it for freshly user-supplied URLs hit on-demand (webhook
    test, connection test); leave it off for the admin-set feed URL that's
    fetched every poll (no per-cycle DNS cost, low rebind risk).

    NOTE: resolve=True does a *blocking* socket.getaddrinfo. Never call it on
    the event loop — use ``validate_external_url_async`` from async code so a
    slow resolver can't stall the poll / WS / SSE loops.

    NOTE: this is not TOCTOU-proof — httpx re-resolves DNS when it makes the
    actual request, so a determined rebinding attacker could still race us.
    True immunity needs resolve-once-then-pin-IP at the transport layer; given
    PiScope's LAN-only, no-auth posture (anyone who can call the API can
    already reach the LAN) that's deliberately out of scope. This raises the
    bar without pretending to be a fortress.
    """
    raw = (raw or "").strip()
    if not raw:
        raise ValueError("URL is empty")
    parsed = urlparse(raw)
    if parsed.scheme not in ("http", "https"):
        raise ValueError(f"Unsupported scheme: {parsed.scheme!r}; only http/https are allowed")
    try:
        parsed.port   # raises ValueError on a non-numeric / out-of-range port
    except ValueError:
        raise ValueError("URL has an invalid port") from None
    # A trailing dot is a valid absolute-FQDN spelling the resolver honours
    # ("metadata.google.internal." / "169.254.169.254."), so strip it before
    # comparing — otherwise it slips past both the name and literal checks.
    host = (parsed.hostname or "").strip().rstrip(".")
    if not host:
        raise ValueError("URL has no host component")
    if host.lower() in _BLOCKED_METADATA_HOSTS:
        raise ValueError("Refusing to fetch cloud-metadata endpoint")

    # Literal-IP host (incl. sneaky numeric encodings)?
    literal_ip = _parse_ip_literal(host)
    if literal_ip is not None:
        reason = _ip_block_reason(literal_ip)
        if reason:
            raise ValueError(f"Refusing to fetch {reason} address ({literal_ip})")
        return raw  # non-blocked literal (incl. private LAN) — allowed

    # Hostname (not a literal). Optionally resolve to catch rebinding.
    if resolve:
        try:
            infos = socket.getaddrinfo(host, None, proto=socket.IPPROTO_TCP)
        except socket.gaierror as exc:
            raise ValueError(f"Could not resolve host {host!r}: {exc}")
        for info in infos:
            addr = info[4][0]
            try:
                ip = ipaddress.ip_address(addr)
            except ValueError:
                continue  # not an IP literal we can reason about — skip
            reason = _ip_block_reason(ip)
            if reason:
                raise ValueError(f"Host {host!r} resolves to a {reason} address ({addr})")
    return raw


async def validate_external_url_async(raw: str, *, resolve: bool = False) -> str:
    """Async-safe wrapper around :func:`validate_external_url`.

    With ``resolve=True`` the underlying ``socket.getaddrinfo`` is a blocking
    C call; running it inline on the event loop freezes the poll loop, WS
    heartbeats and the SSE stream for the full resolver timeout. Offload it to a
    worker thread so a slow / dead DNS server can't stall the whole app. With
    ``resolve=False`` there's no DNS and we just run inline."""
    if not resolve:
        return validate_external_url(raw, resolve=False)
    return await asyncio.to_thread(validate_external_url, raw, resolve=True)


def build_user_agent() -> str:
    """Compose a User-Agent that satisfies the Planespotters policy (and is generally polite).
    They reject UAs that don't include a +URL or email, e.g. `MyApp/1.0 (+https://…)`."""
    contact = (settings_store.get("contact_url") or "https://github.com/ajacks-595/piscope-radar").strip()
    return f"PiScope-Radar/1.0 (+{contact})"


async def get_client() -> httpx.AsyncClient:
    global _CLIENT
    if _CLIENT is not None:
        return _CLIENT
    async with _CLIENT_LOCK:
        if _CLIENT is None:
            _CLIENT = httpx.AsyncClient(
                timeout=httpx.Timeout(8.0, connect=4.0),
                headers={"User-Agent": build_user_agent()},
                limits=httpx.Limits(max_connections=20, max_keepalive_connections=10),
            )
    return _CLIENT


async def reset_client() -> None:
    """Discard the existing shared client so the next get_client() rebuilds with fresh settings
    (e.g. after the contact_url setting changes)."""
    global _CLIENT
    if _CLIENT is not None:
        await _CLIENT.aclose()
        _CLIENT = None


async def close_client() -> None:
    await reset_client()


class LRUCache:
    """A tiny insertion-order LRU. We don't need thread-safety; asyncio serialises access."""

    def __init__(self, max_size: int = 1024) -> None:
        self.max_size = max_size
        self._data: OrderedDict[Any, Any] = OrderedDict()

    def __contains__(self, key: Any) -> bool:
        return key in self._data

    def get(self, key: Any) -> Any:
        if key not in self._data:
            return None
        self._data.move_to_end(key)
        return self._data[key]

    def set(self, key: Any, value: Any) -> None:
        if key in self._data:
            self._data.move_to_end(key)
        self._data[key] = value
        while len(self._data) > self.max_size:
            self._data.popitem(last=False)

    def clear(self) -> None:
        self._data.clear()

    def __len__(self) -> int:
        return len(self._data)


class FailureBackoff:
    """Remember recent *transient* upstream failures (429 / 5xx / network errors) so a
    flapping enrichment API isn't re-hit on every request: callers skip the upstream
    for `ttl_s` after a failure. Unlike the old behaviour (cache `{}` forever), the key
    is retried once the backoff lapses. Bounded like the caches it sits beside."""

    def __init__(self, ttl_s: float = 120.0, max_size: int = 4096) -> None:
        self.ttl_s = ttl_s
        self._until = LRUCache(max_size=max_size)

    def blocked(self, key: Any) -> bool:
        until = self._until.get(key)
        return until is not None and until > time.monotonic()

    def record(self, key: Any) -> None:
        self._until.set(key, time.monotonic() + self.ttl_s)

    def clear(self) -> None:
        self._until.clear()
