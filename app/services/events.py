"""Event log, daily stats, and snapshot ring buffer for replay.

All three live on the SQLite store shared with settings; we keep this isolated so the feed
service can call into a small surface without knowing about the database internals.

Event kinds:
  - "military"   : a military aircraft entered coverage
  - "emergency"  : a transponder is squawking 7500 / 7600 / 7700
  - "watchlist"  : an aircraft on the user's watchlist entered coverage

De-duplication is the caller's responsibility (we already track `notifiedHexes` for the WS
broadcast); this module just records what it's given.
"""
from __future__ import annotations

import json
import logging
import time
import zlib
from datetime import datetime, timezone
from typing import Any, Iterable, Optional

from .settings import _connect, get  # type: ignore[attr-defined]


# Snapshots are stored zlib-compressed because JSON of aircraft state compresses ~80%.
# Format: first byte is a version tag so we can decode legacy plain-JSON rows too.
_SNAPSHOT_MAGIC = b"Z1"   # 2-byte tag → zlib-deflated UTF-8 JSON follows


log = logging.getLogger("piscope.events")


# --- Event log ---------------------------------------------------------------


def record_event(kind: str, *, hex: str, callsign: Optional[str] = None,
                 registration: Optional[str] = None, distance_nm: Optional[float] = None,
                 payload: Optional[dict[str, Any]] = None,
                 conn: Optional[Any] = None) -> None:
    """If `conn` is provided, write within the caller's transaction (no commit). Used by the
    feed loop's batch so we don't open a second SQLite connection while holding a write lock
    — which would deadlock against our own batch transaction until busy_timeout fires."""
    if not hex or kind not in {"military", "emergency", "watchlist", "rare"}:
        return
    row = (time.time(), kind, hex, callsign, registration, distance_nm,
           json.dumps(payload, default=str) if payload else None)
    sql = ("INSERT INTO events(ts, kind, hex, callsign, registration, distance_nm, payload) "
           "VALUES(?, ?, ?, ?, ?, ?, ?)")
    try:
        if conn is not None:
            conn.execute(sql, row)
        else:
            with _connect() as c:
                c.execute(sql, row)
                c.commit()
    except Exception as exc:  # pragma: no cover — DB error shouldn't crash the poll loop
        log.warning("record_event failed: %s", exc)


def recent_events(limit: int = 100, kind: Optional[str] = None) -> list[dict[str, Any]]:
    limit = max(1, min(int(limit or 100), 500))
    with _connect() as conn:
        if kind:
            rows = conn.execute(
                "SELECT id, ts, kind, hex, callsign, registration, distance_nm, payload "
                "FROM events WHERE kind = ? ORDER BY ts DESC LIMIT ?",
                (kind, limit),
            ).fetchall()
        else:
            rows = conn.execute(
                "SELECT id, ts, kind, hex, callsign, registration, distance_nm, payload "
                "FROM events ORDER BY ts DESC LIMIT ?",
                (limit,),
            ).fetchall()
    out = []
    for r in rows:
        item = dict(r)
        if item.get("payload"):
            try:
                item["payload"] = json.loads(item["payload"])
            except (json.JSONDecodeError, TypeError):
                # Leave payload as the raw string if it isn't valid JSON — best-effort
                # decode of a display field. Narrowed from bare Exception.
                pass
        out.append(item)
    return out


def prune_old_events(max_age_seconds: float = 7 * 24 * 3600) -> int:
    """Trim events older than `max_age_seconds`. Returns rows removed."""
    cutoff = time.time() - max_age_seconds
    with _connect() as conn:
        cur = conn.execute("DELETE FROM events WHERE ts < ?", (cutoff,))
        conn.commit()
        return cur.rowcount or 0


# --- Daily stats -------------------------------------------------------------


def _today() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%d")


def update_daily_stats(*, unique_hexes_today: int, max_range_nm_today: float,
                       emergencies_today: int, military_today: int) -> None:
    """Replace the row for today with the latest aggregates. Cheap; runs once per poll."""
    date = _today()
    try:
        with _connect() as conn:
            conn.execute(
                "INSERT INTO daily_stats(date, total_polls, unique_aircraft, max_range_nm, emergencies, military_seen) "
                "VALUES(?, 1, ?, ?, ?, ?) "
                "ON CONFLICT(date) DO UPDATE SET "
                "  total_polls = daily_stats.total_polls + 1, "
                "  unique_aircraft = excluded.unique_aircraft, "
                "  max_range_nm = MAX(daily_stats.max_range_nm, excluded.max_range_nm), "
                "  emergencies = excluded.emergencies, "
                "  military_seen = excluded.military_seen",
                (date, unique_hexes_today, max_range_nm_today, emergencies_today, military_today),
            )
            conn.commit()
    except Exception as exc:
        log.warning("update_daily_stats failed: %s", exc)


def get_stats(days: int = 7) -> dict[str, Any]:
    days = max(1, min(int(days or 7), 31))
    with _connect() as conn:
        rows = conn.execute(
            "SELECT date, total_polls, unique_aircraft, max_range_nm, emergencies, military_seen "
            "FROM daily_stats ORDER BY date DESC LIMIT ?",
            (days,),
        ).fetchall()
    return {"days": [dict(r) for r in rows]}


# --- Snapshot ring buffer (replay) -------------------------------------------


def record_snapshot(ts: float, payload: dict[str, Any]) -> None:
    """Persist a snapshot. Caller (feed loop) trims by calling `prune_old_snapshots`."""
    try:
        slim = {**payload}
        slim.pop("trails", None)             # replay only needs positions, not history
        slim.pop("trail_appends", None)       # broadcast field — irrelevant to replay
        slim.pop("feeds", None)               # transient per-source status
        # zlib level 6 is a good size/CPU tradeoff. ~80% compression on aircraft JSON.
        body = _SNAPSHOT_MAGIC + zlib.compress(
            json.dumps(slim, default=str, separators=(",", ":")).encode("utf-8"),
            6,
        )
        with _connect() as conn:
            conn.execute(
                "INSERT OR REPLACE INTO feed_snapshots(ts, payload) VALUES(?, ?)",
                (ts, body),
            )
            conn.commit()
    except Exception as exc:
        log.warning("record_snapshot failed: %s", exc)


def prune_old_snapshots(max_age_seconds: float) -> int:
    cutoff = time.time() - max_age_seconds
    try:
        with _connect() as conn:
            cur = conn.execute("DELETE FROM feed_snapshots WHERE ts < ?", (cutoff,))
            conn.commit()
            return cur.rowcount or 0
    except Exception:
        return 0


def snapshot_timeline(max_points: int = 600) -> list[float]:
    """Return a list of recent snapshot timestamps for the timeline scrubber."""
    with _connect() as conn:
        rows = conn.execute(
            "SELECT ts FROM feed_snapshots ORDER BY ts ASC LIMIT ?",
            (max(1, int(max_points)),),
        ).fetchall()
    return [r["ts"] for r in rows]


def _decode_snapshot(raw: Any) -> Optional[dict[str, Any]]:
    """Decode a stored snapshot blob, supporting both the new zlib-tagged format and
    legacy plain JSON strings written before compression was added."""
    if raw is None:
        return None
    try:
        if isinstance(raw, (bytes, bytearray, memoryview)):
            b = bytes(raw)
            if b.startswith(_SNAPSHOT_MAGIC):
                return json.loads(zlib.decompress(b[len(_SNAPSHOT_MAGIC):]).decode("utf-8"))
            return json.loads(b.decode("utf-8"))
        return json.loads(raw)
    except Exception:
        return None


def snapshot_nearest(ts: float) -> Optional[dict[str, Any]]:
    """Find the snapshot whose timestamp is closest to `ts`."""
    with _connect() as conn:
        row = conn.execute(
            "SELECT ts, payload FROM feed_snapshots "
            "ORDER BY ABS(ts - ?) ASC LIMIT 1", (ts,),
        ).fetchone()
    if not row:
        return None
    payload = _decode_snapshot(row["payload"])
    if payload is None:
        return None
    payload["_replay_ts"] = row["ts"]
    return payload


# --- Convenience: split a comma list of callsign/reg tokens for watchlists ---


def parse_watchlist(raw: str) -> list[str]:
    return [s.strip().upper() for s in (raw or "").split(",") if s.strip()]
