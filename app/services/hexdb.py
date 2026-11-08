from __future__ import annotations

import logging
import re
from typing import Any, Optional

from ._http import FailureBackoff, LRUCache, get_client


log = logging.getLogger("piscope.hexdb")

_CACHE = LRUCache(max_size=2048)
_BACKOFF = FailureBackoff()
_HEX_RE = re.compile(r"^[0-9a-f]{6}$")
URL = "https://hexdb.io/api/v1/aircraft/{hex}"


def _text(value: Any, max_len: int = 200) -> Optional[str]:
    """A trimmed, length-capped string, or None. Upstream fields that aren't strings
    (a schema change returning a number or object) used to hit `.strip()` and raise
    AttributeError → 500 on /api/enrich/hexdb (2026-11-08 P3)."""
    if not isinstance(value, str):
        return None
    return value.strip()[:max_len] or None


async def lookup(hex_id: str) -> Optional[dict[str, Any]]:
    hex_id = (hex_id or "").lower().strip()
    # Defence-in-depth: ICAO hex IDs are always six hex digits — anything else is rejected
    # so we never embed user input into an outbound URL path.
    if not _HEX_RE.match(hex_id):
        return None
    cached = _CACHE.get(hex_id)
    if cached is not None:
        return cached
    if _BACKOFF.blocked(hex_id):
        return None
    try:
        client = await get_client()
        r = await client.get(URL.format(hex=hex_id), timeout=5.0)
        if r.status_code == 404:          # genuinely unknown airframe — cache the miss
            _CACHE.set(hex_id, {})
            return {}
        if r.status_code != 200:          # 429 / 5xx: transient — back off, don't cache
            log.info("hexdb lookup for %s: HTTP %s", hex_id, r.status_code)
            _BACKOFF.record(hex_id)
            return None
        data = r.json()
    except Exception as exc:
        log.info("hexdb lookup failed for %s: %s", hex_id, exc)
        _BACKOFF.record(hex_id)
        return None

    # Response shaping is OUTSIDE the try, so a schema-changed / hostile upstream
    # returning a non-object body must not raise AttributeError past it (would 500
    # the /api/enrich/hexdb endpoint instead of returning a clean envelope).
    if not isinstance(data, dict):
        _CACHE.set(hex_id, {})
        return {}
    result = {
        "hex": hex_id,
        "registration": _text(data.get("Registration")),
        "manufacturer": _text(data.get("Manufacturer")),
        "type": _text(data.get("Type")),
        "icao_type_code": _text(data.get("ICAOTypeCode")),
        "registered_owners": _text(data.get("RegisteredOwners")),
        "operator_flag_code": _text(data.get("OperatorFlagCode")),
    }
    _CACHE.set(hex_id, result)
    return result
