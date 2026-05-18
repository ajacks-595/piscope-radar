from __future__ import annotations

import logging
import re
from datetime import datetime, timezone
from typing import Any, Optional

import httpx

from . import settings as settings_store
from ._http import LRUCache, get_client


log = logging.getLogger("piscope.flightaware")

# Cost per AeroAPI call, in cents. AeroAPI bills $0.05 per flight lookup at the time of writing.
COST_CENTS_PER_CALL = 5
URL = "https://aeroapi.flightaware.com/aeroapi/flights/{ident}?max_pages=1"
_CALLSIGN_RE = re.compile(r"^[A-Z0-9]{2,8}$")

_CACHE = LRUCache(max_size=512)


def _today_utc() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%d")


def _pick_best(flights: list[dict[str, Any]]) -> Optional[dict[str, Any]]:
    if not flights:
        return None

    def parse(ts: Optional[str]) -> datetime:
        if not ts:
            return datetime.fromtimestamp(0, tz=timezone.utc)
        try:
            return datetime.fromisoformat(ts.replace("Z", "+00:00"))
        except Exception:
            return datetime.fromtimestamp(0, tz=timezone.utc)

    flights = list(flights)
    flights.sort(
        key=lambda f: parse((f.get("scheduled_out") or f.get("estimated_out") or f.get("actual_out"))),
        reverse=True,
    )
    return flights[0]


def _airport_block(prefix: str, src: dict[str, Any]) -> dict[str, Any]:
    return {
        f"{prefix}_code_icao": src.get("code_icao") or src.get("code"),
        f"{prefix}_code_iata": src.get("code_iata"),
        f"{prefix}_name": src.get("name"),
        f"{prefix}_city": src.get("city"),
    }


def _flatten(flight: dict[str, Any]) -> dict[str, Any]:
    origin = flight.get("origin") or {}
    dest = flight.get("destination") or {}
    out: dict[str, Any] = {
        "ident": flight.get("ident"),
        "ident_icao": flight.get("ident_icao"),
        "ident_iata": flight.get("ident_iata"),
        "fa_flight_id": flight.get("fa_flight_id"),
        "operator": flight.get("operator"),
        "operator_icao": flight.get("operator_icao"),
        "operator_iata": flight.get("operator_iata"),
        "flight_number": flight.get("flight_number"),
        "aircraft_type": flight.get("aircraft_type"),
        "registration": flight.get("registration"),
        "status": flight.get("status"),
        "progress_percent": flight.get("progress_percent"),
        "route": flight.get("route"),
        "route_distance": flight.get("route_distance"),
        "filed_altitude": flight.get("filed_altitude"),
        "filed_airspeed": flight.get("filed_airspeed"),
        "gate_origin": flight.get("gate_origin"),
        "gate_destination": flight.get("gate_destination"),
        "terminal_origin": flight.get("terminal_origin"),
        "terminal_destination": flight.get("terminal_destination"),
        "baggage_claim": flight.get("baggage_claim"),
        "seats_cabin_business": flight.get("seats_cabin_business"),
        "seats_cabin_coach": flight.get("seats_cabin_coach"),
        "seats_cabin_first": flight.get("seats_cabin_first"),
    }
    out.update(_airport_block("origin", origin))
    out.update(_airport_block("destination", dest))

    # 12 timestamp fields (scheduled / estimated / actual × out / off / on / in)
    for kind in ("out", "off", "on", "in"):
        for state in ("scheduled", "estimated", "actual"):
            key = f"{state}_{kind}"
            out[key] = flight.get(key)
    return out


async def lookup(callsign: str) -> dict[str, Any]:
    callsign = (callsign or "").upper().strip()
    if not _CALLSIGN_RE.match(callsign):
        return {"error": "Invalid callsign"}

    cache_key = (callsign, _today_utc())
    cached = _CACHE.get(cache_key)
    if cached is not None:
        return {**cached, "cached": True}

    api_key = settings_store.get("fa_api_key") or ""
    if not api_key:
        return {"error": "FlightAware API key is not configured."}

    try:
        client = await get_client()
        r = await client.get(URL.format(ident=callsign), headers={"x-apikey": api_key}, timeout=12.0)
        if r.status_code == 401:
            return {"error": "FlightAware rejected the API key."}
        if r.status_code == 404:
            _CACHE.set(cache_key, {"flight": None})
            settings_store.fa_record_call(COST_CENTS_PER_CALL)
            return {"flight": None}
        r.raise_for_status()
        data = r.json()
    except httpx.HTTPError as exc:
        # Intentionally do NOT log the URL — even though the key is in a header, paranoia is cheap.
        log.warning("FlightAware call failed for %s: %s", callsign, type(exc).__name__)
        return {"error": "FlightAware request failed"}

    flight = _pick_best(data.get("flights") or [])
    flattened = _flatten(flight) if flight else None
    payload = {"flight": flattened}

    _CACHE.set(cache_key, payload)
    settings_store.fa_record_call(COST_CENTS_PER_CALL)
    return payload
