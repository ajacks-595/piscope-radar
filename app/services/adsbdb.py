from __future__ import annotations

import logging
import re
from typing import Any, Optional

from ._http import LRUCache, get_client


log = logging.getLogger("piscope.adsbdb")

_CACHE = LRUCache(max_size=2048)
_CALLSIGN_RE = re.compile(r"^[A-Z0-9]{2,8}$")
URL = "https://api.adsbdb.com/v0/callsign/{callsign}"


async def lookup(callsign: str) -> Optional[dict[str, Any]]:
    callsign = (callsign or "").upper().strip()
    if not _CALLSIGN_RE.match(callsign):
        # Invalid callsign (military stand-ins like "@@@@@@@@", or strings with spaces).
        # Treat as "no data" rather than an upstream failure so the API returns 200 {} and
        # the frontend doesn't log a 502 for what is really a client-side classification.
        return {}
    cached = _CACHE.get(callsign)
    if cached is not None:
        return cached
    try:
        client = await get_client()
        r = await client.get(URL.format(callsign=callsign), timeout=6.0)
        if r.status_code != 200:
            _CACHE.set(callsign, {})
            return {}
        data = r.json()
    except Exception as exc:
        log.info("adsbdb lookup failed for %s: %s", callsign, exc)
        return None

    response = (data or {}).get("response") or {}
    flightroute = response.get("flightroute") or {}
    origin = flightroute.get("origin") or {}
    dest = flightroute.get("destination") or {}
    airline = flightroute.get("airline") or {}

    result = {
        "callsign": flightroute.get("callsign") or callsign,
        "callsign_icao": flightroute.get("callsign_icao"),
        "callsign_iata": flightroute.get("callsign_iata"),
        "origin_iata": origin.get("iata_code"),
        "origin_icao": origin.get("icao_code"),
        "origin_name": origin.get("name"),
        "origin_municipality": origin.get("municipality"),
        "origin_country_iso": origin.get("country_iso_name"),
        "origin_lat": origin.get("latitude"),
        "origin_lon": origin.get("longitude"),
        "destination_iata": dest.get("iata_code"),
        "destination_icao": dest.get("icao_code"),
        "destination_name": dest.get("name"),
        "destination_municipality": dest.get("municipality"),
        "destination_country_iso": dest.get("country_iso_name"),
        "destination_lat": dest.get("latitude"),
        "destination_lon": dest.get("longitude"),
        "airline_name": airline.get("name"),
        "airline_iata": airline.get("iata"),
        "airline_icao": airline.get("icao"),
        "airline_country_iso": airline.get("country_iso_name"),
    }
    _CACHE.set(callsign, result)
    return result
