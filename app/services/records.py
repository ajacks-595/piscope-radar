"""All-time records (closest pass, lowest, fastest, longest range) + bookmarks.

Updated incrementally from the feed loop. One row per category in `records`; we only write
when the new value beats the existing one.
"""
from __future__ import annotations

import contextlib
import logging
import sqlite3
import time
from typing import Any, Optional

from ..models.aircraft import (
    Aircraft,
    PLAUSIBLE_ALT_MAX_FT,
    PLAUSIBLE_ALT_MIN_FT,
    PLAUSIBLE_GS_MAX_KT,
    PLAUSIBLE_RANGE_MAX_NM,
)
from .settings import _connect  # type: ignore[attr-defined]


@contextlib.contextmanager
def _conn_or_provided(conn: Optional[sqlite3.Connection]):
    if conn is not None:
        yield conn
    else:
        own = _connect()
        try:
            yield own
            own.commit()
        finally:
            own.close()


log = logging.getLogger("piscope.records")


# --- Records ----------------------------------------------------------------

# (category, comparator) — "min" or "max" determines whether smaller or larger values win.
CATEGORIES: dict[str, tuple[str, str]] = {
    "closest_pass":  ("min", "Closest pass (nm)"),
    "lowest_alt":    ("min", "Lowest altitude (ft)"),
    "fastest":       ("max", "Fastest ground speed (kts)"),
    "longest_range": ("max", "Longest range (nm)"),
    "highest":       ("max", "Highest altitude (ft)"),
}


def _maybe_update(category: str, value: float, *, ac: Aircraft, conn: sqlite3.Connection) -> None:
    """Compare and write within the caller's transaction — no commit here."""
    if value is None:
        return
    comp, _label = CATEGORIES[category]
    row = conn.execute("SELECT value FROM records WHERE category = ?", (category,)).fetchone()
    if row is not None:
        existing = row["value"]
        if existing is not None:
            if comp == "min" and value >= existing:
                return
            if comp == "max" and value <= existing:
                return
    conn.execute(
        "INSERT OR REPLACE INTO records(category, hex, callsign, registration, type_code, "
        "value, lat, lon, altitude, recorded_at) VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (category, ac.hex, ac.callsign, ac.registration, ac.type_code,
         float(value), ac.lat, ac.lon, ac.altitude_baro, time.time()),
    )


def update_records(ac: Aircraft, conn: Optional[sqlite3.Connection] = None) -> None:
    """Test every category against this aircraft. Called once per aircraft per poll.

    Use `update_records_bulk` for poll-loop scale — it does 5 SQL ops per poll regardless
    of aircraft count, vs ~5×N for this per-aircraft entry point.
    """
    with _conn_or_provided(conn) as c:
        # Plausibility-bounded throughout: a single garbage frame (ATR at 1,885 kts,
        # A330 at 126,500 ft — both observed live) would otherwise set an unbeatable
        # all-time record that never self-corrects.
        if ac.distance_nm is not None and 0 < ac.distance_nm <= PLAUSIBLE_RANGE_MAX_NM:
            _maybe_update("closest_pass", ac.distance_nm, ac=ac, conn=c)
            _maybe_update("longest_range", ac.distance_nm, ac=ac, conn=c)
        if ac.altitude_baro is not None and not ac.on_ground \
                and PLAUSIBLE_ALT_MIN_FT <= ac.altitude_baro <= PLAUSIBLE_ALT_MAX_FT:
            # Filter out spurious 0-ft reports from ground squitters.
            if ac.altitude_baro > 500:
                _maybe_update("lowest_alt", ac.altitude_baro, ac=ac, conn=c)
            _maybe_update("highest", ac.altitude_baro, ac=ac, conn=c)
        if ac.ground_speed is not None and 0 < ac.ground_speed <= PLAUSIBLE_GS_MAX_KT:
            _maybe_update("fastest", ac.ground_speed, ac=ac, conn=c)


def update_records_bulk(aircraft: list[Aircraft], conn: Optional[sqlite3.Connection] = None) -> None:
    """Compute per-poll bests across the whole aircraft list in memory, then push at most
    one row per category to SQLite. Reduces O(N×categories) DB ops to O(categories)."""
    bests: dict[str, tuple[float, Aircraft]] = {}

    def consider(cat: str, value: Optional[float], ac: Aircraft) -> None:
        if value is None:
            return
        comp, _ = CATEGORIES[cat]
        cur = bests.get(cat)
        if cur is None or (comp == "min" and value < cur[0]) or (comp == "max" and value > cur[0]):
            bests[cat] = (float(value), ac)

    for ac in aircraft:
        # Same plausibility envelope as update_records — see comment there.
        if ac.distance_nm is not None and 0 < ac.distance_nm <= PLAUSIBLE_RANGE_MAX_NM:
            consider("closest_pass", ac.distance_nm, ac)
            consider("longest_range", ac.distance_nm, ac)
        if ac.altitude_baro is not None and not ac.on_ground \
                and PLAUSIBLE_ALT_MIN_FT <= ac.altitude_baro <= PLAUSIBLE_ALT_MAX_FT:
            if ac.altitude_baro > 500:
                consider("lowest_alt", float(ac.altitude_baro), ac)
            consider("highest", float(ac.altitude_baro), ac)
        if ac.ground_speed is not None and 0 < ac.ground_speed <= PLAUSIBLE_GS_MAX_KT:
            consider("fastest", float(ac.ground_speed), ac)
    if not bests:
        return
    with _conn_or_provided(conn) as c:
        for cat, (value, ac) in bests.items():
            _maybe_update(cat, value, ac=ac, conn=c)


def all_records() -> list[dict[str, Any]]:
    with _connect() as conn:
        rows = conn.execute(
            "SELECT category, hex, callsign, registration, type_code, value, lat, lon, altitude, recorded_at "
            "FROM records"
        ).fetchall()
    by_cat = {r["category"]: dict(r) for r in rows}
    out = []
    for cat, (_comp, label) in CATEGORIES.items():
        if cat in by_cat:
            d = by_cat[cat]
            d["label"] = label
            out.append(d)
        else:
            out.append({"category": cat, "label": label, "value": None})
    return out


# --- Bookmarks --------------------------------------------------------------


def list_bookmarks() -> list[dict[str, Any]]:
    with _connect() as conn:
        rows = conn.execute(
            "SELECT hex, label, callsign, registration, type_code, added_at FROM bookmarks ORDER BY added_at DESC"
        ).fetchall()
    return [dict(r) for r in rows]


def add_bookmark(hex_id: str, *, label: str = "", callsign: Optional[str] = None,
                 registration: Optional[str] = None, type_code: Optional[str] = None) -> dict[str, Any]:
    hex_id = (hex_id or "").lower().strip()
    label = (label or "").strip()[:80]
    if not hex_id:
        raise ValueError("hex required")
    now = time.time()
    with _connect() as conn:
        conn.execute(
            "INSERT INTO bookmarks(hex, label, callsign, registration, type_code, added_at) "
            "VALUES(?, ?, ?, ?, ?, ?) "
            "ON CONFLICT(hex) DO UPDATE SET "
            "  label = excluded.label, "
            "  callsign = COALESCE(excluded.callsign, bookmarks.callsign), "
            "  registration = COALESCE(excluded.registration, bookmarks.registration), "
            "  type_code = COALESCE(excluded.type_code, bookmarks.type_code)",
            (hex_id, label, callsign, registration, type_code, now),
        )
        conn.commit()
        row = conn.execute("SELECT * FROM bookmarks WHERE hex = ?", (hex_id,)).fetchone()
    return dict(row)


def remove_bookmark(hex_id: str) -> None:
    hex_id = (hex_id or "").lower().strip()
    if not hex_id:
        return
    with _connect() as conn:
        conn.execute("DELETE FROM bookmarks WHERE hex = ?", (hex_id,))
        conn.commit()


def has_bookmark(hex_id: str) -> bool:
    hex_id = (hex_id or "").lower().strip()
    if not hex_id:
        return False
    with _connect() as conn:
        row = conn.execute("SELECT 1 FROM bookmarks WHERE hex = ?", (hex_id,)).fetchone()
    return row is not None
