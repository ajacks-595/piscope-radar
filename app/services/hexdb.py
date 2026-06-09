from __future__ import annotations

import logging
import re
from typing import Any, Optional

from ._http import LRUCache, get_client


log = logging.getLogger("piscope.hexdb")

_CACHE = LRUCache(max_size=2048)
_HEX_RE = re.compile(r"^[0-9a-f]{6}$")
URL = "https://hexdb.io/api/v1/aircraft/{hex}"


async def lookup(hex_id: str) -> Optional[dict[str, Any]]:
    hex_id = (hex_id or "").lower().strip()
    # Defence-in-depth: ICAO hex IDs are always six hex digits — anything else is rejected
    # so we never embed user input into an outbound URL path.
    if not _HEX_RE.match(hex_id):
        return None
    cached = _CACHE.get(hex_id)
    if cached is not None:
        return cached
    try:
        client = await get_client()
        r = await client.get(URL.format(hex=hex_id), timeout=5.0)
        if r.status_code != 200:
            _CACHE.set(hex_id, {})
            return {}
        data = r.json()
    except Exception as exc:
        log.info("hexdb lookup failed for %s: %s", hex_id, exc)
        return None

    # Response shaping is OUTSIDE the try, so a schema-changed / hostile upstream
    # returning a non-object body must not raise AttributeError past it (would 500
    # the /api/enrich/hexdb endpoint instead of returning a clean envelope).
    if not isinstance(data, dict):
        _CACHE.set(hex_id, {})
        return {}
    result = {
        "hex": hex_id,
        "registration": (data.get("Registration") or "").strip() or None,
        "manufacturer": (data.get("Manufacturer") or "").strip() or None,
        "type": (data.get("Type") or "").strip() or None,
        "icao_type_code": (data.get("ICAOTypeCode") or "").strip() or None,
        "registered_owners": (data.get("RegisteredOwners") or "").strip() or None,
        "operator_flag_code": (data.get("OperatorFlagCode") or "").strip() or None,
    }
    _CACHE.set(hex_id, result)
    return result
