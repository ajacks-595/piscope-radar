from __future__ import annotations

import math
from dataclasses import dataclass, field, asdict
from typing import Any, Optional


EMERGENCY_SQUAWKS = {"7500", "7600", "7700"}


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
        return self.true_heading or self.mag_heading or self.track

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
        d = asdict(self)
        # Promote useful derived fields so the frontend doesn't recompute them.
        d["heading"] = self.heading
        d["is_emergency_squawk"] = self.is_emergency_squawk
        d["display_name"] = self.display_name
        d["vertical_trend"] = self.vertical_trend
        d["altitude_band"] = self.altitude_band
        return d


def _strip(v: Any) -> Optional[str]:
    if v is None:
        return None
    s = str(v).strip()
    return s if s else None


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
    hex_id = (row.get("hex") or "").lower()
    if not hex_id:
        return None

    alt_baro_raw = row.get("alt_baro")
    if isinstance(alt_baro_raw, int):
        alt_baro: Optional[int] = alt_baro_raw
        on_ground = False
    elif isinstance(alt_baro_raw, str) and alt_baro_raw.lower() == "ground":
        alt_baro = None
        on_ground = True
    else:
        alt_baro = None
        on_ground = False

    db_flags = row.get("dbFlags") or 0
    military = bool(db_flags & 0x01)

    return Aircraft(
        hex=hex_id,
        callsign=_strip(row.get("flight")),
        registration=_strip(row.get("r")),
        type_code=_strip(row.get("t")),
        category=row.get("category"),
        military=military,
        data_source=(row.get("type") or "other"),
        lat=row.get("lat"),
        lon=row.get("lon"),
        altitude_baro=alt_baro,
        on_ground=on_ground,
        altitude_geom=row.get("alt_geom"),
        ground_speed=row.get("gs"),
        ias=row.get("ias"),
        tas=row.get("tas"),
        mach=row.get("mach"),
        track=row.get("track"),
        mag_heading=row.get("mag_heading"),
        true_heading=row.get("true_heading"),
        baro_rate=row.get("baro_rate"),
        geom_rate=row.get("geom_rate"),
        squawk=row.get("squawk"),
        emergency=row.get("emergency") or "none",
        wind_direction=row.get("wd"),
        wind_speed=row.get("ws"),
        oat=row.get("oat"),
        tat=row.get("tat"),
        roll=row.get("roll"),
        track_rate=row.get("track_rate"),
        nav_altitude_mcp=row.get("nav_altitude_mcp"),
        nav_qnh=row.get("nav_qnh"),
        rssi=row.get("rssi"),
        distance_nm=row.get("dst"),
        seen=row.get("seen"),
        seen_pos=row.get("seen_pos"),
        messages=row.get("messages"),
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
