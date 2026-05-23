"""Long-running aggregates: per-aircraft notes, type leaderboard, polar coverage, heatmap.

Each of these is a tiny SQL surface so the feed loop can update incrementally with no fuss.
The dashboard pulls aggregated rows via /api/coverage, /api/heatmap, /api/leaderboard etc.
"""
from __future__ import annotations

import contextlib
import logging
import math
import sqlite3
import time
from typing import Any, Optional

from .settings import _connect  # type: ignore[attr-defined]


@contextlib.contextmanager
def _conn_or_provided(conn: Optional[sqlite3.Connection]):
    """If a connection is provided, use it and don't commit (the caller's batch will).
    Otherwise open our own, commit at the end. Lets every helper be called either way."""
    if conn is not None:
        yield conn
    else:
        own = _connect()
        try:
            yield own
            own.commit()
        finally:
            own.close()


log = logging.getLogger("piscope.insights")

HEATMAP_BUCKET_DEG = 0.05   # ~3 nm at mid-latitudes — keeps the table dense but bounded.


# --- Personal notes ---------------------------------------------------------


def get_note(hex_id: str) -> Optional[str]:
    hex_id = (hex_id or "").lower().strip()
    if not hex_id:
        return None
    with _connect() as conn:
        row = conn.execute("SELECT note FROM aircraft_notes WHERE hex = ?", (hex_id,)).fetchone()
    return row["note"] if row else None


def set_note(hex_id: str, note: str) -> None:
    hex_id = (hex_id or "").lower().strip()
    note = (note or "").strip()[:2000]   # cap length — these are personal scribbles, not logs
    if not hex_id:
        return
    with _connect() as conn:
        if not note:
            conn.execute("DELETE FROM aircraft_notes WHERE hex = ?", (hex_id,))
        else:
            conn.execute(
                "INSERT INTO aircraft_notes(hex, note, updated_at) VALUES(?, ?, ?) "
                "ON CONFLICT(hex) DO UPDATE SET note = excluded.note, updated_at = excluded.updated_at",
                (hex_id, note, time.time()),
            )
        conn.commit()


def all_notes() -> dict[str, str]:
    with _connect() as conn:
        rows = conn.execute("SELECT hex, note FROM aircraft_notes").fetchall()
    return {r["hex"]: r["note"] for r in rows}


# --- Type ledger / leaderboard / rare alerts --------------------------------


def is_known_type(type_code: str) -> bool:
    type_code = (type_code or "").upper().strip()
    if not type_code:
        return True   # don't fire "rare" when we have no classification at all
    with _connect() as conn:
        row = conn.execute("SELECT 1 FROM seen_types WHERE type_code = ?", (type_code,)).fetchone()
    return row is not None


# In-memory cache of known type_codes. Populated lazily on first lookup; updated by
# `flush_type_sightings`. Lets the feed loop ask "is this type new?" without a DB
# round-trip per aircraft. Pre-iter-10.3 each aircraft per poll did SELECT+UPDATE
# (or SELECT+INSERT) — ~210 SQL ops/poll at 50 aircraft. Now it's at most one
# executemany per distinct type per poll.
_KNOWN_TYPES: Optional[set[str]] = None


def ensure_known_types() -> set[str]:
    """Return the in-memory set of type_codes we've ever recorded. Populates from DB
    on first call. Callers may read but should not mutate — `flush_type_sightings`
    is the only writer."""
    global _KNOWN_TYPES
    if _KNOWN_TYPES is None:
        with _connect() as conn:
            rows = conn.execute("SELECT type_code FROM seen_types").fetchall()
        _KNOWN_TYPES = {r["type_code"] for r in rows}
    return _KNOWN_TYPES


def record_type_sighting(type_code: str, conn: Optional[sqlite3.Connection] = None) -> bool:
    """Upsert a single sighting. Returns True if this is the first time we've ever
    seen this type. Prefer `flush_type_sightings` from the poll loop — this entry
    point is kept for one-off calls (admin tools, tests, the background enrichment
    loop's odd type discovery)."""
    type_code = (type_code or "").upper().strip()
    if not type_code:
        return False
    known = ensure_known_types()
    is_new = type_code not in known
    now = time.time()
    with _conn_or_provided(conn) as c:
        if is_new:
            c.execute(
                "INSERT INTO seen_types(type_code, first_seen, last_seen, sightings) VALUES(?, ?, ?, 1)",
                (type_code, now, now),
            )
            known.add(type_code)
        else:
            c.execute(
                "UPDATE seen_types SET last_seen = ?, sightings = sightings + 1 WHERE type_code = ?",
                (now, type_code),
            )
    return is_new


def flush_type_sightings(counts: dict[str, int], conn: sqlite3.Connection) -> set[str]:
    """Persist per-poll type counts in one or two `executemany` calls instead of N
    SELECT+UPDATEs. Returns the set of type_codes that were NEW (not previously
    in the known set) — caller uses this to fire "rare" events.

    Caller owns the surrounding transaction; we never commit. Updates the
    in-memory `_KNOWN_TYPES` set as a side effect so subsequent polls don't
    re-classify these as new."""
    if not counts:
        return set()
    known = ensure_known_types()
    now = time.time()
    new_types: set[str] = set()
    insert_rows: list[tuple] = []
    update_rows: list[tuple] = []
    for type_code, n in counts.items():
        type_code = type_code.upper().strip()
        if not type_code:
            continue
        if type_code in known:
            update_rows.append((now, n, type_code))
        else:
            insert_rows.append((type_code, now, now, n))
            new_types.add(type_code)
            known.add(type_code)
    if insert_rows:
        conn.executemany(
            "INSERT INTO seen_types(type_code, first_seen, last_seen, sightings) VALUES(?, ?, ?, ?)",
            insert_rows,
        )
    if update_rows:
        conn.executemany(
            "UPDATE seen_types SET last_seen = ?, sightings = sightings + ? WHERE type_code = ?",
            update_rows,
        )
    return new_types


def leaderboard(limit: int = 20) -> list[dict[str, Any]]:
    limit = max(1, min(int(limit or 20), 100))
    with _connect() as conn:
        rows = conn.execute(
            "SELECT type_code, sightings, first_seen, last_seen FROM seen_types "
            "ORDER BY sightings DESC LIMIT ?", (limit,),
        ).fetchall()
    return [dict(r) for r in rows]


# --- Polar coverage ---------------------------------------------------------


def _bearing_deg(receiver_lat: float, receiver_lon: float, lat: float, lon: float) -> float:
    p1 = math.radians(receiver_lat)
    p2 = math.radians(lat)
    dl = math.radians(lon - receiver_lon)
    y = math.sin(dl) * math.cos(p2)
    x = math.cos(p1) * math.sin(p2) - math.sin(p1) * math.cos(p2) * math.cos(dl)
    return (math.degrees(math.atan2(y, x)) + 360.0) % 360.0


def update_polar(receiver_lat: float, receiver_lon: float, *, hex_id: str,
                 lat: float, lon: float, distance_nm: float,
                 conn: Optional[sqlite3.Connection] = None) -> None:
    if distance_nm is None or distance_nm <= 0:
        return
    bearing = int(round(_bearing_deg(receiver_lat, receiver_lon, lat, lon))) % 360
    now = time.time()
    with _conn_or_provided(conn) as c:
        c.execute(
            "INSERT INTO polar_coverage(bearing, max_nm, last_hex, last_seen) VALUES(?, ?, ?, ?) "
            "ON CONFLICT(bearing) DO UPDATE SET "
            "  max_nm = MAX(polar_coverage.max_nm, excluded.max_nm), "
            "  last_hex = CASE WHEN excluded.max_nm > polar_coverage.max_nm THEN excluded.last_hex ELSE polar_coverage.last_hex END, "
            "  last_seen = CASE WHEN excluded.max_nm > polar_coverage.max_nm THEN excluded.last_seen ELSE polar_coverage.last_seen END",
            (bearing, float(distance_nm), hex_id, now),
        )


def polar_coverage() -> list[dict[str, Any]]:
    """Return all 360 bearings, filling zeros where we haven't recorded anything yet."""
    with _connect() as conn:
        rows = conn.execute("SELECT bearing, max_nm, last_hex, last_seen FROM polar_coverage").fetchall()
    by_bearing = {r["bearing"]: dict(r) for r in rows}
    out = []
    for b in range(360):
        if b in by_bearing:
            out.append(by_bearing[b])
        else:
            out.append({"bearing": b, "max_nm": 0.0, "last_hex": None, "last_seen": None})
    return out


# --- Position heatmap -------------------------------------------------------


def update_heatmap(lat: float, lon: float, conn: Optional[sqlite3.Connection] = None) -> None:
    """Single-point update. Prefer `flush_heatmap_batch` for the poll loop —
    it coalesces aircraft sharing a bucket into one UPSERT, saving DB ops."""
    lat_b = int(math.floor(lat / HEATMAP_BUCKET_DEG))
    lon_b = int(math.floor(lon / HEATMAP_BUCKET_DEG))
    with _conn_or_provided(conn) as c:
        c.execute(
            "INSERT INTO position_heatmap(lat_bucket, lon_bucket, hits) VALUES(?, ?, 1) "
            "ON CONFLICT(lat_bucket, lon_bucket) DO UPDATE SET hits = hits + 1",
            (lat_b, lon_b),
        )


def heatmap_bucket(lat: float, lon: float) -> tuple[int, int]:
    return (int(math.floor(lat / HEATMAP_BUCKET_DEG)),
            int(math.floor(lon / HEATMAP_BUCKET_DEG)))


def flush_heatmap_batch(counts: dict[tuple[int, int], int], conn: sqlite3.Connection) -> None:
    """Add `counts[(lat_b, lon_b)] = N` hits to the heatmap in a single batch. Caller is
    responsible for the surrounding transaction (we never commit here)."""
    if not counts:
        return
    # `executemany` is significantly faster than many `execute`s on SQLite.
    conn.executemany(
        "INSERT INTO position_heatmap(lat_bucket, lon_bucket, hits) VALUES(?, ?, ?) "
        "ON CONFLICT(lat_bucket, lon_bucket) DO UPDATE SET hits = hits + excluded.hits",
        [(lat_b, lon_b, n) for (lat_b, lon_b), n in counts.items()],
    )


def heatmap_points(top_n: int = 5000) -> list[tuple[float, float, int]]:
    top_n = max(100, min(int(top_n or 5000), 20000))
    with _connect() as conn:
        rows = conn.execute(
            "SELECT lat_bucket, lon_bucket, hits FROM position_heatmap ORDER BY hits DESC LIMIT ?",
            (top_n,),
        ).fetchall()
    out = []
    for r in rows:
        # Centre of the bucket.
        lat = (r["lat_bucket"] + 0.5) * HEATMAP_BUCKET_DEG
        lon = (r["lon_bucket"] + 0.5) * HEATMAP_BUCKET_DEG
        out.append((lat, lon, r["hits"]))
    return out
