from __future__ import annotations

import math
from dataclasses import dataclass, field
from typing import Any, Optional


EMERGENCY_SQUAWKS = {"7500", "7600", "7700"}

# Plausibility envelope for RF-sourced measurements. ADS-B frames occasionally
# decode to garbage (live examples from this receiver: an ATR-45 "doing" 1,885 kts,
# an A330 "at" 126,500 ft) and a single bad frame would otherwise poison all-time
# records and per-day sighting extremes forever. Bounds are deliberately generous —
# above every transponder-equipped conventional aircraft, below the garbage.
PLAUSIBLE_ALT_MIN_FT = -1500       # Death Valley + baro-offset headroom
PLAUSIBLE_ALT_MAX_FT = 66_000      # Concorde ceiling 60k; U-2 ops are once-a-decade
PLAUSIBLE_GS_MAX_KT = 1500         # fast jets supersonic at altitude stay well under
PLAUSIBLE_RANGE_MAX_NM = 2500      # global-feed radius caps at 2000; beyond is CPR junk


@dataclass
class Aircraft:
    hex: str
    callsign: Optional[str] = None
    registration: Optional[str] = None
    type_code: Optional[str] = None
    category: Optional[str] = None
    military: bool = False
    data_source: str = "other"  # adsb_icao / mlat / tisb_icao / mode_s / ...

    lat: Optional[float] = None
    lon: Optional[float] = None
    altitude_baro: Optional[int] = None  # feet; None when not transmitted
    on_ground: bool = False
    altitude_geom: Optional[int] = None
    ground_speed: Optional[float] = None
    ias: Optional[float] = None
    tas: Optional[float] = None
    mach: Optional[float] = None
    track: Optional[float] = None
    mag_heading: Optional[float] = None
    true_heading: Optional[float] = None
    baro_rate: Optional[int] = None
    geom_rate: Optional[int] = None

    squawk: Optional[str] = None
    emergency: str = "none"

    wind_direction: Optional[int] = None
    wind_speed: Optional[int] = None
    oat: Optional[int] = None
    tat: Optional[int] = None
    roll: Optional[float] = None
    track_rate: Optional[float] = None
    nav_altitude_mcp: Optional[int] = None
    nav_qnh: Optional[float] = None

    rssi: Optional[float] = None
    distance_nm: Optional[float] = None
    seen: Optional[float] = None
    seen_pos: Optional[float] = None
    messages: Optional[int] = None
    observed_at: float = 0.0

    @property
    def heading(self) -> Optional[float]:
        # Prefer true heading, then magnetic, then ground track. Use explicit None
        # checks rather than `or`: 0.0 (due north) is a valid heading but falsy, so
        # `true_heading or mag_heading or track` would wrongly skip a 0.0 in favour
        # of a less-preferred source.
        for h in (self.true_heading, self.mag_heading, self.track):
            if h is not None:
                return h
        return None

    @property
    def is_emergency_squawk(self) -> bool:
        return self.squawk in EMERGENCY_SQUAWKS

    @property
    def display_name(self) -> str:
        return self.callsign or self.registration or self.hex.upper()

    @property
    def vertical_trend(self) -> str:
        if self.baro_rate is None:
            return "level"
        if self.baro_rate > 100:
            return "climb"
        if self.baro_rate < -100:
            return "descent"
        return "level"

    @property
    def altitude_band(self) -> str:
        if self.on_ground:
            return "ground"
        ft = self.altitude_baro
        if ft is None:
            return "ground"
        if ft < 10_000:
            return "low"
        if ft < 25_000:
            return "mid"
        if ft < 35_000:
            return "high"
        return "very_high"

    def to_json(self) -> dict[str, Any]:
        # Hand-rolled flat literal: dataclasses.asdict() does a recursive copy with
        # per-field isinstance checks (`_is_dataclass_instance`) which dominated the
        # poll-loop profile pre-iter10 — ~36% of active CPU inclusive, ~16% pure
        # leaf time, because this is called twice per aircraft per poll (snapshot
        # + broadcast). Skipping asdict drops both numbers to near zero. Field list
        # must stay in sync with the @dataclass declaration above; if you add a
        # field there, add it here too.
        return {
            "hex": self.hex,
            "callsign": self.callsign,
            "registration": self.registration,
            "type_code": self.type_code,
            "category": self.category,
            "military": self.military,
            "data_source": self.data_source,
            "lat": self.lat,
            "lon": self.lon,
            "altitude_baro": self.altitude_baro,
            "on_ground": self.on_ground,
            "altitude_geom": self.altitude_geom,
            "ground_speed": self.ground_speed,
            "ias": self.ias,
            "tas": self.tas,
            "mach": self.mach,
            "track": self.track,
            "mag_heading": self.mag_heading,
            "true_heading": self.true_heading,
            "baro_rate": self.baro_rate,
            "geom_rate": self.geom_rate,
            "squawk": self.squawk,
            "emergency": self.emergency,
            "wind_direction": self.wind_direction,
            "wind_speed": self.wind_speed,
            "oat": self.oat,
            "tat": self.tat,
            "roll": self.roll,
            "track_rate": self.track_rate,
            "nav_altitude_mcp": self.nav_altitude_mcp,
            "nav_qnh": self.nav_qnh,
            "rssi": self.rssi,
            "distance_nm": self.distance_nm,
            "seen": self.seen,
            "seen_pos": self.seen_pos,
            "messages": self.messages,
            "observed_at": self.observed_at,
            # Promoted derived fields so the frontend doesn't recompute.
            "heading": self.heading,
            "is_emergency_squawk": self.is_emergency_squawk,
            "display_name": self.display_name,
            "vertical_trend": self.vertical_trend,
            "altitude_band": self.altitude_band,
        }


def _strip(v: Any) -> Optional[str]:
    if v is None:
        return None
    s = str(v).strip()
    return s if s else None


def _num(v: Any) -> Optional[float]:
    """Coerce a wire value to a finite float, else None. ADS-B rows are RF-sourced (and
    adsb.lol is an external service), so a field may arrive as a string, NaN, or the wrong
    type entirely — we drop it to None rather than let it crash downstream maths."""
    if isinstance(v, bool):
        return None                      # bool is an int subclass; a flag is never a measurement
    if isinstance(v, (int, float)):
        return float(v) if math.isfinite(v) else None
    if isinstance(v, str):
        try:
            f = float(v.strip())
        except ValueError:
            return None
        return f if math.isfinite(f) else None
    return None


def _intn(v: Any) -> Optional[int]:
    """Like _num but returns an int (truncated) or None."""
    f = _num(v)
    return int(f) if f is not None else None


def _data_source_label(raw: Optional[str]) -> str:
    raw = (raw or "").lower()
    if raw in ("adsb_icao", "adsb_other"):
        return "ADS-B"
    if raw == "adsr_icao":
        return "ADS-R"
    if raw == "mlat":
        return "MLAT"
    if raw == "tisb_icao":
        return "TIS-B"
    if raw == "mode_s":
        return "Mode-S"
    return "—"


def aircraft_from_wire(row: dict[str, Any], observed_at: float) -> Optional[Aircraft]:
    # Feeds are RF-sourced (and adsb.lol is external): a row may be missing fields, carry
    # the wrong types, or be actively malformed. Parse defensively — every numeric goes
    # through _num/_intn (string/NaN/wrong-type → None) so a single bad row can never raise
    # out of the poll loop and abort the whole cycle (which would freeze the live view).
    if not isinstance(row, dict):
        return None
    hex_id = str(row.get("hex") or "").strip().lower()
    if not hex_id:
        return None

    alt_baro_raw = row.get("alt_baro")
    if isinstance(alt_baro_raw, str) and alt_baro_raw.strip().lower() == "ground":
        alt_baro: Optional[int] = None
        on_ground = True
    else:
        alt_baro = _intn(alt_baro_raw)
        on_ground = False

    military = bool((_intn(row.get("dbFlags")) or 0) & 0x01)

    return Aircraft(
        hex=hex_id,
        callsign=_strip(row.get("flight")),
        registration=_strip(row.get("r")),
        type_code=_strip(row.get("t")),
        category=_strip(row.get("category")),
        military=military,
        data_source=_strip(row.get("type")) or "other",
        lat=_num(row.get("lat")),
        lon=_num(row.get("lon")),
        altitude_baro=alt_baro,
        on_ground=on_ground,
        altitude_geom=_intn(row.get("alt_geom")),
        ground_speed=_num(row.get("gs")),
        ias=_num(row.get("ias")),
        tas=_num(row.get("tas")),
        mach=_num(row.get("mach")),
        track=_num(row.get("track")),
        mag_heading=_num(row.get("mag_heading")),
        true_heading=_num(row.get("true_heading")),
        baro_rate=_intn(row.get("baro_rate")),
        geom_rate=_intn(row.get("geom_rate")),
        squawk=_strip(row.get("squawk")),
        emergency=_strip(row.get("emergency")) or "none",
        wind_direction=_intn(row.get("wd")),
        wind_speed=_intn(row.get("ws")),
        oat=_intn(row.get("oat")),
        tat=_intn(row.get("tat")),
        roll=_num(row.get("roll")),
        track_rate=_num(row.get("track_rate")),
        nav_altitude_mcp=_intn(row.get("nav_altitude_mcp")),
        nav_qnh=_num(row.get("nav_qnh")),
        rssi=_num(row.get("rssi")),
        distance_nm=_num(row.get("dst")),
        seen=_num(row.get("seen")),
        seen_pos=_num(row.get("seen_pos")),
        messages=_intn(row.get("messages")),
        observed_at=observed_at,
    )


def haversine_nm(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    """Great-circle distance in nautical miles."""
    r_nm = 3440.065  # earth radius in nm
    p1 = math.radians(lat1)
    p2 = math.radians(lat2)
    dp = math.radians(lat2 - lat1)
    dl = math.radians(lon2 - lon1)
    a = math.sin(dp / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dl / 2) ** 2
    return 2 * r_nm * math.asin(math.sqrt(a))


def data_source_short(raw: str) -> str:
    return _data_source_label(raw)
