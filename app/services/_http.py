"""Shared HTTP client + LRU cache helpers for enrichment services.

A single long-lived httpx.AsyncClient per enrichment service avoids repeatedly opening
TLS/TCP connections (each lookup against hexdb/adsbdb/planespotters was previously a fresh
handshake). Caches are bounded so a long-running Pi instance can't grow without limit.
"""
from __future__ import annotations

import asyncio
from collections import OrderedDict
from typing import Any, Optional

import httpx

from . import settings as settings_store


# Single client per process — keep-alive pools are reused across lookups.
_CLIENT_LOCK = asyncio.Lock()
_CLIENT: Optional[httpx.AsyncClient] = None


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
