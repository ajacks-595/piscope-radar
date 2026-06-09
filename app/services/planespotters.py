from __future__ import annotations

import logging
import re
from typing import Any, Optional

from ._http import LRUCache, get_client


log = logging.getLogger("piscope.planespotters")

_CACHE = LRUCache(max_size=2048)
_HEX_RE = re.compile(r"^[0-9a-f]{6}$")
URL = "https://api.planespotters.net/pub/photos/hex/{hex}"


def _safe_https_url(value: Any) -> Optional[str]:
    """Photo URLs are inserted into <img src>. Only accept https URLs to defang any future
    upstream compromise that returns javascript: / data: payloads."""
    if not isinstance(value, str):
        return None
    if not value.lower().startswith("https://"):
        return None
    # Strip control chars / whitespace just in case.
    return value.strip()


async def lookup(hex_id: str) -> Optional[dict[str, Any]]:
    hex_id = (hex_id or "").lower().strip()
    if not _HEX_RE.match(hex_id):
        return None
    cached = _CACHE.get(hex_id)
    if cached is not None:
        return cached
    try:
        client = await get_client()
        r = await client.get(URL.format(hex=hex_id), headers={"Accept": "application/json"}, timeout=6.0)
        if r.status_code != 200:
            _CACHE.set(hex_id, {})
            return {}
        data = r.json()
    except Exception as exc:
        log.info("planespotters lookup failed for %s: %s", hex_id, exc)
        return None

    # Parsing runs outside the try; guard every shape assumption so a
    # schema-changed / hostile upstream (non-object body, non-list photos,
    # non-dict entries) returns a clean {} instead of 500-ing /api/enrich/photo.
    photos_raw = data.get("photos") if isinstance(data, dict) else None
    if not isinstance(photos_raw, list) or not photos_raw:
        _CACHE.set(hex_id, {})
        return {}
    # Return up to 6 photos so the UI can show a small carousel.
    gallery = []
    for p in photos_raw[:6]:
        if not isinstance(p, dict):
            continue
        thumbnail = p.get("thumbnail_large") or p.get("thumbnail") or {}
        if not isinstance(thumbnail, dict):
            continue
        url = _safe_https_url(thumbnail.get("src"))
        if not url:
            continue
        photographer = p.get("photographer")
        gallery.append({
            "url": url,
            "photographer": (photographer[:120] or None) if isinstance(photographer, str) else None,
            "link": _safe_https_url(p.get("link")),
        })
    if not gallery:
        _CACHE.set(hex_id, {})
        return {}
    # First entry is exposed as the legacy top-level fields so older clients still work.
    first = gallery[0]
    result = {
        "hex": hex_id,
        "photo_url": first["url"],
        "photographer": first["photographer"],
        "link": first["link"],
        "photos": gallery,
    }
    _CACHE.set(hex_id, result)
    return result
