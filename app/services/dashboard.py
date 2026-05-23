"""Dashboard summary computation (iter 9.2).

Builds the response for GET /api/dashboard/summary from `feed_service`'s
in-memory aircraft set. Pure-functional given the snapshot — no DB hits, no
upstream calls. The endpoint adds a small response cache on top so multiple
browser tabs polling the same coordinates share work.

Math note: distances are computed with the haversine formula. The "overhead"
projection uses a great-circle bearing + speed forward-step at fixed intervals
within the time horizon; for the small distances and short horizons here
(≤500 km, ≤300 s) the flat-Earth approximation would also be fine but
haversine is the same code we use elsewhere and keeps results consistent.
"""
from __future__ import annotations

import math
import time
from typing import Any, Iterable, Optional

from . import categorize
from . import settings as settings_store


EARTH_RADIUS_KM = 6371.0
NM_TO_KM = 1.852

# How far out into the future we step when projecting "overhead_imminent".
# Steps are 5 s wide, matching the typical ADS-B position-message cadence —
# any finer resolution is illusory precision.
_OVERHEAD_STEP_S = 5.0


def haversine_km(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    """Great-circle distance in kilometres."""
    phi1, phi2 = math.radians(lat1), math.radians(lat2)
    dphi = math.radians(lat2 - lat1)
    dlam = math.radians(lon2 - lon1)
    a = math.sin(dphi / 2) ** 2 + math.cos(phi1) * math.cos(phi2) * math.sin(dlam / 2) ** 2
    return 2 * EARTH_RADIUS_KM * math.asin(math.sqrt(a))


def _project_position(lat: float, lon: float, track_deg: float, speed_kt: float, dt_s: float) -> tuple[float, float]:
    """Project lat/lon forward by `dt_s` seconds at the given track and ground
    speed (knots). Uses the standard spherical-Earth destination formula —
    accurate for the short forward horizons used here."""
    # Distance covered in km.
    distance_km = (speed_kt * NM_TO_KM) * (dt_s / 3600.0)
    angular = distance_km / EARTH_RADIUS_KM
    track_rad = math.radians(track_deg)
    lat_rad = math.radians(lat)
    lon_rad = math.radians(lon)
    new_lat = math.asin(
        math.sin(lat_rad) * math.cos(angular)
        + math.cos(lat_rad) * math.sin(angular) * math.cos(track_rad)
    )
    new_lon = lon_rad + math.atan2(
        math.sin(track_rad) * math.sin(angular) * math.cos(lat_rad),
        math.cos(angular) - math.sin(lat_rad) * math.sin(new_lat),
    )
    return math.degrees(new_lat), math.degrees(new_lon)


def _matches_filters(ac: dict[str, Any], category: str, filters_set: set[str],
                     min_alt: Optional[float], max_alt: Optional[float],
                     min_speed: Optional[float]) -> bool:
    """Apply the documented filter vocabulary. Empty filter set = pass-through.
    Numeric filters apply regardless of which category tokens are present."""
    if filters_set:
        # Category tokens accept the same names as `categorize.CATEGORIES`,
        # plus the standalone tokens `emergency` and `watchlist` which are
        # orthogonal to category (an emergency commercial flight passes
        # both `commercial` and `emergency`).
        ok = False
        if category in filters_set:
            ok = True
        if not ok and "emergency" in filters_set and categorize.is_emergency(ac):
            ok = True
        if not ok and "watchlist" in filters_set and ac.get("watchlist_match"):
            ok = True
        if not ok:
            return False
    alt = ac.get("altitude_baro")
    if min_alt is not None and (alt is None or alt < min_alt):
        return False
    if max_alt is not None and (alt is None or alt > max_alt):
        return False
    spd = ac.get("ground_speed")
    if min_speed is not None and (spd is None or spd < min_speed):
        return False
    return True


def _contact_compact(ac: dict[str, Any], distance_km: float, category: str,
                     overhead_eta_s: Optional[float] = None) -> dict[str, Any]:
    """The trimmed-down per-aircraft shape we return in nearest/overhead/
    highlights. Drops the raw enrichment fields the dashboard doesn't need."""
    tags: list[str] = [category]
    if categorize.is_emergency(ac):
        tags.append("emergency")
    if ac.get("watchlist_match"):
        tags.append("watchlist")
    if ac.get("military"):
        tags.append("military")
    out = {
        "icao": ac.get("hex"),
        "callsign": (ac.get("callsign") or "").strip() or None,
        "registration": ac.get("registration"),
        "type_code": ac.get("type_code"),
        "category": category,
        "tags": tags,
        "lat": ac.get("lat"),
        "lon": ac.get("lon"),
        "altitude_ft": ac.get("altitude_baro"),
        "ground_speed_kt": ac.get("ground_speed"),
        "track_deg": ac.get("heading"),
        "distance_km": round(distance_km, 2),
    }
    if overhead_eta_s is not None:
        out["eta_overhead_s"] = round(overhead_eta_s, 1)
    return out


def _highlight_score(ac: dict[str, Any], category: str, distance_km: float,
                     overhead_eta_s: Optional[float]) -> tuple:
    """Ordering key for `highlights` — lower is more interesting. Tuple
    ordering: emergency first, then overhead-imminent, then military,
    then watchlist, then rare types, then distance."""
    emergency = 0 if categorize.is_emergency(ac) else 1
    overhead = overhead_eta_s if overhead_eta_s is not None else float("inf")
    military = 0 if ac.get("military") else 1
    watchlist = 0 if ac.get("watchlist_match") else 1
    # Rare types — placeholder. The seen_types table has frequency data; we
    # could look up sightings here, but a DB round-trip per aircraft would be
    # silly. Leave at 0 for now and let distance be the tiebreaker.
    rare = 0
    return (emergency, overhead, military, watchlist, rare, distance_km)


def build_summary(
    aircraft_iter: Iterable[dict[str, Any]],
    *,
    observer_lat: float,
    observer_lon: float,
    radius_km: float = 50.0,
    filters: Optional[str] = None,
    top: int = 3,
    min_alt: Optional[float] = None,
    max_alt: Optional[float] = None,
    min_speed: Optional[float] = None,
    overhead_threshold_s: float = 60.0,
    overhead_radius_km: float = 2.0,
    feed_total_messages: int = 0,
    feed_uptime_seconds: float = 0.0,
    feed_last_poll_at: Optional[float] = None,
    feed_connection_state: str = "unknown",
    daily_unique_count: int = 0,
    watchlist: Optional[set[str]] = None,
) -> dict[str, Any]:
    """Single-pass computation of the dashboard summary. Caller wraps in the
    `{success, data, error}` envelope."""
    filters_set = set()
    if filters:
        filters_set = {t.strip().lower() for t in filters.split(",") if t.strip()}

    counts = {cat: 0 for cat in categorize.CATEGORIES}
    counts["total"] = 0
    counts["emergency"] = 0
    counts["watchlist"] = 0
    counts["overhead_imminent"] = 0

    nearest_ac: Optional[dict[str, Any]] = None
    nearest_dist_km: float = float("inf")
    nearest_cat: str = "unknown"

    overhead_list: list[tuple[float, dict[str, Any], str]] = []
    candidates: list[tuple[tuple, dict[str, Any], str, float, Optional[float]]] = []

    for ac in aircraft_iter:
        lat = ac.get("lat")
        lon = ac.get("lon")
        if lat is None or lon is None:
            continue
        # Watchlist match — local check against the configured CSV string of
        # callsigns and registrations. Mirrors the same logic feed.py uses to
        # decide event logging.
        if watchlist:
            cs = (ac.get("callsign") or "").strip().upper()
            reg = (ac.get("registration") or "").strip().upper()
            if cs in watchlist or reg in watchlist:
                ac = {**ac, "watchlist_match": True}

        dist_km = haversine_km(observer_lat, observer_lon, lat, lon)
        if dist_km > radius_km:
            continue
        category = categorize.category_for(ac)
        if not _matches_filters(ac, category, filters_set, min_alt, max_alt, min_speed):
            continue

        counts["total"] += 1
        counts[category] = counts.get(category, 0) + 1
        if categorize.is_emergency(ac):
            counts["emergency"] += 1
        if ac.get("watchlist_match"):
            counts["watchlist"] += 1

        if dist_km < nearest_dist_km:
            nearest_dist_km = dist_km
            nearest_ac = ac
            nearest_cat = category

        # Overhead projection — needs a valid ground track and speed.
        overhead_eta: Optional[float] = None
        speed = ac.get("ground_speed")
        track = ac.get("heading")
        if speed and speed > 30 and track is not None:
            steps = int(overhead_threshold_s // _OVERHEAD_STEP_S)
            best_d = dist_km
            best_t: Optional[float] = None
            for i in range(1, steps + 1):
                t = i * _OVERHEAD_STEP_S
                plat, plon = _project_position(lat, lon, track, speed, t)
                d = haversine_km(observer_lat, observer_lon, plat, plon)
                if d < best_d:
                    best_d = d
                    if d <= overhead_radius_km:
                        best_t = t
                        break
            if best_t is not None:
                overhead_eta = best_t
                overhead_list.append((best_t, ac, category))
                counts["overhead_imminent"] += 1

        candidates.append((_highlight_score(ac, category, dist_km, overhead_eta), ac, category, dist_km, overhead_eta))

    # Top-N highlights — sorted by score tuple.
    candidates.sort(key=lambda x: x[0])
    highlights = [
        _contact_compact(ac, dist_km, cat, overhead_eta)
        for (_score, ac, cat, dist_km, overhead_eta) in candidates[: max(0, min(int(top), 10))]
    ]

    overhead_list.sort(key=lambda x: x[0])
    overhead_imminent_out = [
        _contact_compact(ac, haversine_km(observer_lat, observer_lon, ac["lat"], ac["lon"]), cat, eta)
        for (eta, ac, cat) in overhead_list[:5]
    ]

    nearest_out: Optional[dict[str, Any]] = None
    if nearest_ac is not None:
        nearest_out = _contact_compact(nearest_ac, nearest_dist_km, nearest_cat)

    # Feed-level signals. messages_per_second is a *lifetime* average (uptime
    # since process start). We document this so the dashboard doesn't mistake
    # it for a current-rate gauge.
    mps = (feed_total_messages / feed_uptime_seconds) if feed_uptime_seconds > 0 else 0.0
    feed_lag = None
    if feed_last_poll_at:
        feed_lag = max(0.0, time.time() - feed_last_poll_at)

    return {
        "observer": {"lat": observer_lat, "lon": observer_lon},
        "radius_km": radius_km,
        "filters": sorted(filters_set) if filters_set else [],
        "counts": counts,
        "rates": {
            "messages_per_second": round(mps, 1),
            "messages_per_second_window": "lifetime",
            "contacts_today": daily_unique_count,
        },
        "nearest": nearest_out,
        "overhead_imminent": overhead_imminent_out,
        "highlights": highlights,
        "connection": {
            "state": feed_connection_state,
            "feed_lag_seconds": None if feed_lag is None else round(feed_lag, 2),
        },
        "as_of": time.time(),
    }


# --- Response cache ---------------------------------------------------------
#
# Keys are tuples of all summary inputs that affect output. A 5-second TTL
# matches the spec and lines up with the typical feed poll cadence — within
# one poll cycle, multiple browser tabs polling the same coords get the same
# already-computed answer.

_CACHE: dict[tuple, tuple[float, dict[str, Any]]] = {}
_CACHE_TTL_S = 5.0


def cache_get(key: tuple) -> Optional[dict[str, Any]]:
    entry = _CACHE.get(key)
    if entry is None:
        return None
    expires_at, data = entry
    if expires_at < time.time():
        _CACHE.pop(key, None)
        return None
    return data


def cache_set(key: tuple, data: dict[str, Any]) -> None:
    _CACHE[key] = (time.time() + _CACHE_TTL_S, data)
    # Bound cache size — dashboards typically poll one or two distinct
    # observer coords, so 64 entries is plenty even with churn.
    if len(_CACHE) > 64:
        # Drop the oldest expiring entry.
        oldest = min(_CACHE.items(), key=lambda kv: kv[1][0])[0]
        _CACHE.pop(oldest, None)


def parse_watchlist() -> set[str]:
    """Read the user's CSV watchlist setting and return uppercase tokens."""
    raw = settings_store.get("watchlist") or ""
    return {t.strip().upper() for t in raw.split(",") if t.strip()}
