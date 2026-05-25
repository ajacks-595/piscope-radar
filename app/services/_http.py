"""Shared HTTP client + LRU cache helpers for enrichment services.

A single long-lived httpx.AsyncClient per enrichment service avoids repeatedly opening
TLS/TCP connections (each lookup against hexdb/adsbdb/planespotters was previously a fresh
handshake). Caches are bounded so a long-running Pi instance can't grow without limit.
"""
from __future__ import annotations

import asyncio
import ipaddress
import socket
from collections import OrderedDict
from typing import Any, Optional
from urllib.parse import urlparse

import httpx

from . import settings as settings_store


# Single client per process — keep-alive pools are reused across lookups.
_CLIENT_LOCK = asyncio.Lock()
_CLIENT: Optional[httpx.AsyncClient] = None


# --- SSRF guard -------------------------------------------------------------
# Lives here (a neutral outbound-HTTP module) rather than in feed.py so both
# feed.py and webhooks.py can use it without an import cycle.

# Block link-local + the AWS/GCP instance-metadata addresses. Other private
# ranges (10.x / 192.168.x / 172.16.x) are deliberately ALLOWED — the user
# points PiScope at LAN tar1090 hosts and LAN webhook receivers on purpose.
_BLOCKED_METADATA_HOSTS = {"169.254.169.254", "fd00:ec2::254", "metadata.google.internal"}


def validate_external_url(raw: str, *, resolve: bool = False) -> str:
    """Return a sanitised http(s) URL, or raise ValueError. SSRF guard for any
    user-supplied URL the server is about to fetch.

    Always: rejects non-http(s) schemes, the cloud-metadata hostnames, and
    link-local literal IPs.

    When ``resolve=True``: also DNS-resolves the hostname and rejects it if any
    returned address is link-local or a metadata address. This closes the
    "innocuous domain whose A record points at 169.254.169.254" rebinding
    vector. Enable it for freshly user-supplied URLs hit on-demand (webhook
    test, connection test); leave it off for the admin-set feed URL that's
    fetched every poll (no per-cycle DNS cost, low rebind risk).

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
    host = (parsed.hostname or "").strip()
    if not host:
        raise ValueError("URL has no host component")
    if host in _BLOCKED_METADATA_HOSTS:
        raise ValueError("Refusing to fetch cloud-metadata endpoint")

    # Literal-IP host?
    literal_ip: Optional[ipaddress._BaseAddress] = None
    try:
        literal_ip = ipaddress.ip_address(host)
    except ValueError:
        pass
    if literal_ip is not None:
        if literal_ip.is_link_local:
            raise ValueError("Link-local IPs are not allowed")
        return raw  # non-link-local literal (incl. private LAN) — allowed

    # Hostname (not a literal). Optionally resolve to catch rebinding.
    if resolve:
        try:
            infos = socket.getaddrinfo(host, None, proto=socket.IPPROTO_TCP)
        except socket.gaierror as exc:
            raise ValueError(f"Could not resolve host {host!r}: {exc}")
        for info in infos:
            addr = info[4][0]
            if addr in _BLOCKED_METADATA_HOSTS:
                raise ValueError(f"Host {host!r} resolves to a cloud-metadata address")
            try:
                if ipaddress.ip_address(addr).is_link_local:
                    raise ValueError(f"Host {host!r} resolves to a link-local address ({addr})")
            except ValueError as exc:
                if "link-local" in str(exc):
                    raise  # re-raise our own rejection; swallow "not an IP" parse errors
    return raw


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
