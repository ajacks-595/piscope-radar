"""Historical analytics: per-(hex, UTC-day) sighting ledger + per-hour traffic stats.

Two halves:

  WRITE — `SightingsBuffer`, owned by the feed loop. Coalesces per-poll observations
  in memory and flushes inside the loop's existing batch transaction, so SD-card
  write pressure stays flat: the single `hourly_stats` row upserts every poll
  (one statement), the `aircraft_sightings` batch lands every ~15 polls (~30 s at
  the default 2 s interval) via one `executemany`. A crash loses at most that
  ~30 s of dwell/last-seen detail — acceptable for analytics, never for alerting
  (alerting stays in events.py).

  READ — `overview()`, the query layer behind GET /api/analytics. Pure aggregation
  over the two tables above plus the pre-existing `daily_stats` (per-day backfill
  from before this feature shipped), `events`, `seen_types`, and `records`. The
  payload's `meta.sightings_coverage_start` tells the UI how far back per-aircraft
  data actually goes so charts can be captioned honestly.

Both tables are DERIVED — raw ingestion and every pre-existing table are untouched.
Schema lives in settings._migrate (user_version 3).
"""
from __future__ import annotations

import logging
import sqlite3
import time
from datetime import datetime, timezone
from typing import Any, Iterable, Optional

from ..models.aircraft import (
    Aircraft,
    PLAUSIBLE_ALT_MAX_FT,
    PLAUSIBLE_ALT_MIN_FT,
    PLAUSIBLE_GS_MAX_KT,
    PLAUSIBLE_RANGE_MAX_NM,
)
from . import airlines
from . import categorize
from . import records as records_store
from .settings import _connect  # type: ignore[attr-defined]


log = logging.getLogger("piscope.analytics")

# Sightings flush cadence, in polls. 15 polls ≈ 30 s at the default 2 s interval —
# frequent enough that "last seen" feels live, rare enough that the per-poll write
# load stays one statement (the hourly row).
SIGHTINGS_FLUSH_POLLS = 15

# Plausibility envelope for ledger extremes — a single garbage frame would
# otherwise poison a sighting's min/max forever and false-positive the notable
# high-altitude rule. Shared with records.py via the model module.
_ALT_MIN_FT = PLAUSIBLE_ALT_MIN_FT
_ALT_MAX_FT = PLAUSIBLE_ALT_MAX_FT
_GS_MAX_KT = PLAUSIBLE_GS_MAX_KT
_RANGE_MAX_NM = PLAUSIBLE_RANGE_MAX_NM

# Second line of defence for altitude extremes: glitches INSIDE the envelope
# (live example: an A339 "at" 46,675 ft for one frame) are single-frame spikes,
# so min/max only accept a value continuous with the aircraft's previous frame
# (|Δ| ≤ 5,000 ft — far beyond any real manoeuvre per poll, far below glitch
# jumps). The previous-altitude map updates on EVERY frame regardless of the
# verdict, which makes the gate self-healing: a spike poisons nothing (the
# next real frame is discontinuous with the spike, but the one after that is
# continuous again), and after a genuine jump — coverage gap, first contact —
# extremes lag by exactly one frame, then catch up.
_ALT_STEP_MAX_FT = 5000
# Safety valve: a busy global feed can hold hundreds of aircraft; if the pending
# map somehow balloons (e.g. user cranks the flush cadence later), flush early.
MAX_PENDING_SIGHTINGS = 4000

RANGE_SECONDS: dict[str, Optional[int]] = {
    "24h": 24 * 3600,
    "7d": 7 * 86400,
    "30d": 30 * 86400,
    "all": None,
}

# Raw hourly rows shipped to the UI are capped to the most recent N days even for
# wider ranges — the hour×weekday matrix covers the full window in aggregate, so
# the raw series only needs to feed the "recent traffic" chart.
HOURLY_SERIES_CAP_DAYS = 31

_BAND_COLUMNS = {
    "ground": "alt_ground",
    "low": "alt_low",
    "mid": "alt_mid",
    "high": "alt_high",
    "very_high": "alt_very_high",
}


def _utc_date(ts: float) -> str:
    return datetime.fromtimestamp(ts, timezone.utc).strftime("%Y-%m-%d")


def _utc_hour(ts: float) -> str:
    return datetime.fromtimestamp(ts, timezone.utc).strftime("%Y-%m-%dT%H")


# --- WRITE side ---------------------------------------------------------------


_SIGHTING_UPSERT = (
    "INSERT INTO aircraft_sightings(hex, date, first_ts, last_ts, polls, callsign, "
    "registration, type_code, military, max_alt, min_alt, max_gs, max_range_nm, min_range_nm) "
    "VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?) "
    "ON CONFLICT(hex, date) DO UPDATE SET "
    "  first_ts = MIN(first_ts, excluded.first_ts), "
    "  last_ts = MAX(last_ts, excluded.last_ts), "
    "  polls = polls + excluded.polls, "
    # Latest non-null wins; a buffered None never erases a stored value.
    "  callsign = COALESCE(excluded.callsign, callsign), "
    "  registration = COALESCE(excluded.registration, registration), "
    "  type_code = COALESCE(excluded.type_code, type_code), "
    "  military = MAX(military, excluded.military), "
    # NULL-safe min/max: SQLite's scalar MAX/MIN return NULL if ANY argument is
    # NULL, which would erase a real value — hence the CASE dance.
    "  max_alt = CASE WHEN excluded.max_alt IS NULL THEN max_alt "
    "                 WHEN max_alt IS NULL THEN excluded.max_alt "
    "                 ELSE MAX(max_alt, excluded.max_alt) END, "
    "  min_alt = CASE WHEN excluded.min_alt IS NULL THEN min_alt "
    "                 WHEN min_alt IS NULL THEN excluded.min_alt "
    "                 ELSE MIN(min_alt, excluded.min_alt) END, "
    "  max_gs = CASE WHEN excluded.max_gs IS NULL THEN max_gs "
    "                WHEN max_gs IS NULL THEN excluded.max_gs "
    "                ELSE MAX(max_gs, excluded.max_gs) END, "
    "  max_range_nm = CASE WHEN excluded.max_range_nm IS NULL THEN max_range_nm "
    "                      WHEN max_range_nm IS NULL THEN excluded.max_range_nm "
    "                      ELSE MAX(max_range_nm, excluded.max_range_nm) END, "
    "  min_range_nm = CASE WHEN excluded.min_range_nm IS NULL THEN min_range_nm "
    "                      WHEN min_range_nm IS NULL THEN excluded.min_range_nm "
    "                      ELSE MIN(min_range_nm, excluded.min_range_nm) END"
)

_HOURLY_UPSERT = (
    "INSERT INTO hourly_stats(hour, unique_aircraft, obs, max_range_nm, military, "
    "alt_ground, alt_low, alt_mid, alt_high, alt_very_high) "
    "VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?, ?) "
    "ON CONFLICT(hour) DO UPDATE SET "
    # Set sizes are absolute, not deltas. MAX (rather than plain replace) so a
    # process restart mid-hour — which resets the in-memory sets — can never
    # LOWER a stored count. The merge undercounts the union of the pre/post
    # restart sets; that's the cheap, honest trade (daily_stats has the same
    # restart semantics, minus the MAX guard).
    "  unique_aircraft = MAX(unique_aircraft, excluded.unique_aircraft), "
    "  military = MAX(military, excluded.military), "
    "  max_range_nm = MAX(max_range_nm, excluded.max_range_nm), "
    # Counters are per-poll deltas.
    "  obs = obs + excluded.obs, "
    "  alt_ground = alt_ground + excluded.alt_ground, "
    "  alt_low = alt_low + excluded.alt_low, "
    "  alt_mid = alt_mid + excluded.alt_mid, "
    "  alt_high = alt_high + excluded.alt_high, "
    "  alt_very_high = alt_very_high + excluded.alt_very_high"
)


class SightingsBuffer:
    """In-memory coalescing for the analytics tables. Contract with the feed loop:
    call `observe_poll()` exactly once per poll cycle with the DE-DUPED aircraft
    store, then `flush(conn)` inside the same poll's batch transaction. Call
    `flush(conn, force=True)` on shutdown so the tail isn't lost."""

    def __init__(self) -> None:
        self._pending: dict[tuple[str, str], dict[str, Any]] = {}   # (hex, date) → row
        self._polls_since_flush = 0
        # Per-hex previous altitude for the spike gate. Survives flushes (unlike
        # `_pending`) so the gate keeps context across batches; size-capped as a
        # leak backstop — a clear just re-opens the gate for one frame per hex.
        self._last_alts: dict[str, int] = {}
        # Per-hour state (reset on hour rollover).
        self._hour: Optional[str] = None
        self._hourly_unique: set[str] = set()
        self._hourly_military: set[str] = set()
        self._hourly_max_range: float = 0.0
        # Per-poll accumulators (reset by every flush).
        self._poll_obs = 0
        self._poll_bands = {col: 0 for col in _BAND_COLUMNS.values()}

    def observe_poll(self, aircraft: Iterable[Aircraft], now_ts: float) -> None:
        date_key = _utc_date(now_ts)
        hour_key = _utc_hour(now_ts)
        if hour_key != self._hour:
            self._hour = hour_key
            self._hourly_unique.clear()
            self._hourly_military.clear()
            self._hourly_max_range = 0.0

        for ac in aircraft:
            self._poll_obs += 1
            # Band counters only for contacts whose altitude is actually known —
            # Aircraft.altitude_band maps a missing altitude to "ground", which
            # would otherwise lump every mode-S-only echo into that bucket and
            # skew the distribution. obs still counts everything.
            if ac.altitude_baro is not None or ac.on_ground:
                self._poll_bands[_BAND_COLUMNS[ac.altitude_band]] += 1
            self._hourly_unique.add(ac.hex)
            if ac.military:
                self._hourly_military.add(ac.hex)

            key = (ac.hex, date_key)
            row = self._pending.get(key)
            if row is None:
                row = {
                    "first_ts": now_ts, "last_ts": now_ts, "polls": 0,
                    "callsign": None, "registration": None, "type_code": None,
                    "military": 0, "max_alt": None, "min_alt": None, "max_gs": None,
                    "max_range_nm": None, "min_range_nm": None,
                }
                self._pending[key] = row
            row["last_ts"] = now_ts
            row["polls"] += 1
            if ac.callsign:
                row["callsign"] = ac.callsign.strip().upper()
            if ac.registration:
                row["registration"] = ac.registration
            if ac.type_code:
                row["type_code"] = ac.type_code.strip().upper()
            if ac.military:
                row["military"] = 1
            alt = ac.altitude_baro
            if alt is not None and not ac.on_ground and _ALT_MIN_FT <= alt <= _ALT_MAX_FT:
                prev = self._last_alts.get(ac.hex)
                if len(self._last_alts) > 20000:
                    self._last_alts.clear()
                self._last_alts[ac.hex] = alt
                # Spike gate: only frame-continuous altitudes may set extremes.
                if prev is None or abs(alt - prev) <= _ALT_STEP_MAX_FT:
                    if row["max_alt"] is None or alt > row["max_alt"]:
                        row["max_alt"] = alt
                    # >500 ft floor mirrors records.py — spurious 0-ft ground
                    # squitters would otherwise own every min-altitude slot.
                    if alt > 500 and (row["min_alt"] is None or alt < row["min_alt"]):
                        row["min_alt"] = alt
            gs = ac.ground_speed
            if gs is not None and 0 < gs <= _GS_MAX_KT and (row["max_gs"] is None or gs > row["max_gs"]):
                row["max_gs"] = gs
            dist = ac.distance_nm
            if dist is not None and 0 < dist <= _RANGE_MAX_NM:
                if row["max_range_nm"] is None or dist > row["max_range_nm"]:
                    row["max_range_nm"] = dist
                if row["min_range_nm"] is None or dist < row["min_range_nm"]:
                    row["min_range_nm"] = dist
                if dist > self._hourly_max_range:
                    self._hourly_max_range = dist

    def flush(self, conn: sqlite3.Connection, force: bool = False) -> None:
        """Write within the caller's transaction — no commit here (feed-batch rule,
        same as insights/records)."""
        if self._hour is None:
            return   # never observed anything; nothing to write
        conn.execute(_HOURLY_UPSERT, (
            self._hour, len(self._hourly_unique), self._poll_obs,
            self._hourly_max_range, len(self._hourly_military),
            self._poll_bands["alt_ground"], self._poll_bands["alt_low"],
            self._poll_bands["alt_mid"], self._poll_bands["alt_high"],
            self._poll_bands["alt_very_high"],
        ))
        self._poll_obs = 0
        for col in self._poll_bands:
            self._poll_bands[col] = 0

        self._polls_since_flush += 1
        due = (force or self._polls_since_flush >= SIGHTINGS_FLUSH_POLLS
               or len(self._pending) > MAX_PENDING_SIGHTINGS)
        if not due or not self._pending:
            if due:
                self._polls_since_flush = 0
            return
        rows = [
            (hex_id, date_key, r["first_ts"], r["last_ts"], r["polls"],
             r["callsign"], r["registration"], r["type_code"], r["military"],
             r["max_alt"], r["min_alt"], r["max_gs"], r["max_range_nm"], r["min_range_nm"])
            for (hex_id, date_key), r in self._pending.items()
        ]
        conn.executemany(_SIGHTING_UPSERT, rows)
        self._pending.clear()
        self._polls_since_flush = 0

    def pending_count(self) -> int:
        return len(self._pending)


def seen_hexes_for_date(date_key: str) -> set[str]:
    """Every hex with a sighting row on the given UTC date. Used by the feed loop
    to re-seed its in-memory daily-unique set after a restart, so today's
    daily_stats count no longer collapses to "aircraft seen since the restart"."""
    try:
        with _connect() as conn:
            rows = conn.execute(
                "SELECT hex FROM aircraft_sightings WHERE date = ?", (date_key,)
            ).fetchall()
        return {r["hex"] for r in rows}
    except sqlite3.Error as exc:
        log.warning("seen_hexes_for_date failed: %s", exc)
        return set()


def prune_old_analytics(max_age_days: Any) -> int:
    """Trim sightings + hourly rows older than the retention window. 0/None/garbage
    disables (keep forever). Own connection — called from the feed loop's 5-minute
    maintenance block, outside the poll batch, like prune_old_snapshots."""
    try:
        days = int(max_age_days)
    except (TypeError, ValueError):
        return 0
    if days <= 0:
        return 0
    cutoff_date = _utc_date(time.time() - days * 86400)
    try:
        with _connect() as conn:
            removed = conn.execute(
                "DELETE FROM aircraft_sightings WHERE date < ?", (cutoff_date,)
            ).rowcount or 0
            # Hour keys ('YYYY-MM-DDTHH') compare lexicographically against a date
            # cutoff: any hour ON the cutoff day sorts above the bare date string,
            # so this deletes strictly-older days only.
            removed += conn.execute(
                "DELETE FROM hourly_stats WHERE hour < ?", (cutoff_date,)
            ).rowcount or 0
            conn.commit()
            return removed
    except sqlite3.Error as exc:
        log.warning("analytics prune failed: %s", exc)
        return 0


# --- READ side ------------------------------------------------------------------


def _sightings_where(since_ts: Optional[float]) -> tuple[str, list[Any]]:
    """WHERE fragment for windowed sightings queries. The date bound rides the
    idx_sightings_date index; the last_ts bound refines sub-day windows (24h)
    that the date granularity alone would over-include."""
    if since_ts is None:
        return "1=1", []
    return "date >= ? AND last_ts >= ?", [_utc_date(since_ts), since_ts]


def overview(range_key: str) -> dict[str, Any]:
    """Everything the Analytics tab / weekly digest needs, in one envelope.
    Raises ValueError on an unknown range key (endpoint maps it to a 400)."""
    if range_key not in RANGE_SECONDS:
        raise ValueError(f"range must be one of {'/'.join(RANGE_SECONDS)}")
    now = time.time()
    span = RANGE_SECONDS[range_key]
    since_ts = (now - span) if span else None
    sight_where, sight_params = _sightings_where(since_ts)

    with _connect() as conn:
        # --- traffic: per-day series (daily_stats backfills pre-feature history) ---
        if since_ts is None:
            daily_rows = conn.execute(
                "SELECT date, total_polls, unique_aircraft, max_range_nm, emergencies, "
                "military_seen FROM daily_stats ORDER BY date ASC"
            ).fetchall()
        else:
            daily_rows = conn.execute(
                "SELECT date, total_polls, unique_aircraft, max_range_nm, emergencies, "
                "military_seen FROM daily_stats WHERE date >= ? ORDER BY date ASC",
                (_utc_date(since_ts),),
            ).fetchall()
        daily = [dict(r) for r in daily_rows]

        # --- traffic: raw hourly series (capped) + full-window hour×weekday matrix ---
        series_floor = max(since_ts or 0.0, now - HOURLY_SERIES_CAP_DAYS * 86400)
        hourly_rows = conn.execute(
            "SELECT hour, unique_aircraft, obs, max_range_nm, military FROM hourly_stats "
            "WHERE hour >= ? ORDER BY hour ASC",
            (_utc_hour(series_floor),),
        ).fetchall()
        hourly = [dict(r) for r in hourly_rows]

        matrix_where = "hour >= ?" if since_ts is not None else "1=1"
        matrix_params = [_utc_hour(since_ts)] if since_ts is not None else []
        matrix_rows = conn.execute(
            "SELECT CAST(strftime('%w', substr(hour, 1, 10)) AS INTEGER) AS dow, "
            "       CAST(substr(hour, 12, 2) AS INTEGER) AS hod, "
            "       COUNT(*) AS hours_counted, "
            "       SUM(obs) AS obs, "
            "       AVG(unique_aircraft) AS avg_unique "
            f"FROM hourly_stats WHERE {matrix_where} GROUP BY dow, hod",
            matrix_params,
        ).fetchall()
        matrix = [
            {"dow": r["dow"], "hod": r["hod"], "hours_counted": r["hours_counted"],
             "obs": int(r["obs"] or 0), "avg_unique": round(float(r["avg_unique"] or 0.0), 1)}
            for r in matrix_rows
        ]

        # Busiest hour-of-day / day-of-week, aggregated across the window.
        by_hod: dict[int, list[float]] = {}
        by_dow: dict[int, tuple[int, int]] = {}   # dow → (obs_sum, hours_sum)
        for m in matrix:
            by_hod.setdefault(m["hod"], []).append(m["avg_unique"])
            obs_sum, hrs_sum = by_dow.get(m["dow"], (0, 0))
            by_dow[m["dow"]] = (obs_sum + m["obs"], hrs_sum + m["hours_counted"])
        busiest_hour = None
        if by_hod:
            hod, vals = max(by_hod.items(), key=lambda kv: sum(kv[1]) / len(kv[1]))
            busiest_hour = {"hod": hod, "avg_unique": round(sum(vals) / len(vals), 1)}
        busiest_dow = None
        if by_dow:
            dow, (obs_sum, hrs_sum) = max(
                by_dow.items(), key=lambda kv: kv[1][0] / max(1, kv[1][1]))
            busiest_dow = {"dow": dow,
                           "avg_obs_per_hour": round(obs_sum / max(1, hrs_sum), 1)}
        busiest_day = None
        if daily:
            best = max(daily, key=lambda d: d["unique_aircraft"] or 0)
            busiest_day = {"date": best["date"], "unique_aircraft": best["unique_aircraft"]}

        # --- type breakdown (windowed, from the sighting ledger) ---
        type_rows = conn.execute(
            "SELECT type_code, COUNT(*) AS sightings, COUNT(DISTINCT hex) AS unique_aircraft "
            f"FROM aircraft_sightings WHERE {sight_where} AND type_code IS NOT NULL "
            "GROUP BY type_code ORDER BY sightings DESC LIMIT 25",
            sight_params,
        ).fetchall()
        types = [
            {"type_code": r["type_code"], "sightings": r["sightings"],
             "unique_aircraft": r["unique_aircraft"],
             "category": categorize.category_for({"type_code": r["type_code"]})}
            for r in type_rows
        ]
        types_lifetime = None
        if range_key == "all":
            # The ledger only reaches back to this feature's deploy; the seen_types
            # counter has run since install, so "all" also ships the lifetime view.
            lt = conn.execute(
                "SELECT type_code, sightings, first_seen, last_seen FROM seen_types "
                "ORDER BY sightings DESC LIMIT 25"
            ).fetchall()
            types_lifetime = [dict(r) for r in lt]

        # --- operator breakdown (callsign prefix → airline) ---
        # Group over ALL airline-style prefixes (the distinct-prefix count is small),
        # resolve in Python, then slice for display — keeps the resolved/unresolved
        # coverage stats exact rather than top-N-truncated.
        op_rows = conn.execute(
            "SELECT substr(callsign, 1, 3) AS prefix, COUNT(*) AS sightings, "
            "       COUNT(DISTINCT hex) AS unique_aircraft "
            f"FROM aircraft_sightings WHERE {sight_where} "
            "AND callsign GLOB '[A-Z][A-Z][A-Z][0-9]*' "
            "GROUP BY prefix ORDER BY sightings DESC",
            sight_params,
        ).fetchall()
        operators = []
        resolved_sightings = 0
        airline_style_sightings = 0
        for r in op_rows:
            info = airlines.operator_for_prefix(r["prefix"])
            airline_style_sightings += r["sightings"]
            if info:
                resolved_sightings += r["sightings"]
            operators.append({
                "prefix": r["prefix"], "sightings": r["sightings"],
                "unique_aircraft": r["unique_aircraft"],
                "name": info["name"] if info else None,
                "country": info["country"] if info else None,
                "radio_callsign": info["callsign"] if info else None,
            })
        with_callsign = conn.execute(
            f"SELECT COUNT(*) FROM aircraft_sightings WHERE {sight_where} "
            "AND callsign IS NOT NULL",
            sight_params,
        ).fetchone()[0]
        operator_coverage = {
            "with_callsign": with_callsign,
            "airline_style": airline_style_sightings,
            "resolved": resolved_sightings,
        }
        operators = operators[:25]

        # --- altitude bands (observation-weighted, from hourly counters) ---
        band_row = conn.execute(
            "SELECT SUM(alt_ground), SUM(alt_low), SUM(alt_mid), SUM(alt_high), "
            f"SUM(alt_very_high) FROM hourly_stats WHERE {matrix_where}",
            matrix_params,
        ).fetchone()
        altitude_bands = {
            band: int(band_row[i] or 0)
            for i, band in enumerate(("ground", "low", "mid", "high", "very_high"))
        }

        # --- range distribution (per-sighting best range, 25 nm buckets, 250+ capped) ---
        hist_rows = conn.execute(
            "SELECT CAST(MIN(max_range_nm, 249.999) / 25 AS INTEGER) * 25 AS bucket, "
            "       COUNT(*) AS n "
            f"FROM aircraft_sightings WHERE {sight_where} AND max_range_nm IS NOT NULL "
            "GROUP BY bucket ORDER BY bucket ASC",
            sight_params,
        ).fetchall()
        range_extremes = conn.execute(
            "SELECT MAX(max_range_nm), "
            "MIN(CASE WHEN min_range_nm > 0 THEN min_range_nm END) "
            f"FROM aircraft_sightings WHERE {sight_where}",
            sight_params,
        ).fetchone()
        ranges = {
            "histogram": [{"bucket_nm": r["bucket"], "count": r["n"]} for r in hist_rows],
            "max_nm": round(range_extremes[0], 1) if range_extremes[0] is not None else None,
            "min_nm": round(range_extremes[1], 1) if range_extremes[1] is not None else None,
        }

        # --- totals ---
        uniq = conn.execute(
            f"SELECT COUNT(DISTINCT hex) FROM aircraft_sightings WHERE {sight_where}",
            sight_params,
        ).fetchone()[0]
        # Military counted with the SAME classification the notable panel uses
        # (dbFlags bit OR hex-allocation range OR callsign-prefix rules), so the
        # chip and the panel can never disagree — counting only the dbFlags bit
        # here read 0 while the panel showed four C-17s/A330s whose feeds don't
        # set dbFlags. Lazy import: notable imports this module's window helpers
        # at module level, so importing it at call time avoids the cycle.
        from . import notable as notable_rules
        mil_where, mil_params = notable_rules.military_where()
        mil_uniq = conn.execute(
            "SELECT COUNT(DISTINCT hex) FROM aircraft_sightings "
            f"WHERE {sight_where} AND {mil_where}",
            [*sight_params, *mil_params],
        ).fetchone()[0]
        if since_ts is None:
            ev_rows = conn.execute(
                "SELECT kind, COUNT(*) AS n FROM events GROUP BY kind").fetchall()
        else:
            ev_rows = conn.execute(
                "SELECT kind, COUNT(*) AS n FROM events WHERE ts >= ? GROUP BY kind",
                (since_ts,),
            ).fetchall()
        events_by_kind = {r["kind"]: r["n"] for r in ev_rows}
        # aircraft_days sums the per-day unique counts — unlike `unique_aircraft`
        # it works across the full daily_stats backfill, at the cost of counting a
        # multi-day visitor once per day.
        aircraft_days = sum(int(d["unique_aircraft"] or 0) for d in daily)
        max_range_daily = max((float(d["max_range_nm"] or 0.0) for d in daily), default=0.0)

        # --- coverage metadata (so the UI can caption charts honestly) ---
        coverage_row = conn.execute(
            "SELECT (SELECT MIN(date) FROM aircraft_sightings), "
            "(SELECT MIN(hour) FROM hourly_stats)").fetchone()

    # --- records (own connection inside records_store) ---
    all_recs = records_store.all_records()
    broken = []
    if since_ts is not None:
        broken = [r for r in all_recs
                  if r.get("value") is not None and (r.get("recorded_at") or 0) >= since_ts]

    return {
        "range": range_key,
        "since": since_ts,
        "generated_at": now,
        "traffic": {
            "daily": daily,
            "hourly": hourly,
            "matrix": matrix,
            "busiest_hour": busiest_hour,
            "busiest_dow": busiest_dow,
            "busiest_day": busiest_day,
        },
        "types": types,
        "types_lifetime": types_lifetime,
        "operators": operators,
        "operator_coverage": operator_coverage,
        "altitude_bands": altitude_bands,
        "ranges": ranges,
        "records": {"all_time": all_recs, "broken_in_window": broken},
        "totals": {
            "unique_aircraft": uniq or 0,
            "military_unique": mil_uniq or 0,
            "aircraft_days": aircraft_days,
            "max_range_nm": round(max_range_daily, 1),
            "events_by_kind": events_by_kind,
            "events": sum(events_by_kind.values()),
        },
        "meta": {
            "sightings_coverage_start": coverage_row[0],
            "hourly_coverage_start": coverage_row[1],
            "hourly_series_cap_days": HOURLY_SERIES_CAP_DAYS,
            "operator_table_size": airlines.table_size(),
        },
    }
