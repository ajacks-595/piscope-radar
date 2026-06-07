"""Notable-aircraft classification (analytics feature, phase 3).

Evaluates the editable rule set in `app/data/notable_rules.json` over the
aircraft_sightings ledger:

  military/government — live dbFlags military bit (primary), ICAO hex allocation
  ranges, military callsign prefixes.
  unusual            — helicopters (via categorize.py's type table), sustained
  low/very-high altitude, persistent no-callsign contacts.
  emergencies        — pulled from the events pipeline (which already captures
  7500/7600/7700 with squawk + position the moment they happen).

Candidate rows are pre-filtered in SQL (cheap even at "all" scale — six-char
lowercase hex sorts lexicographically exactly like its numeric value, so the
allocation ranges become BETWEEN bounds), then classified in Python so every
match carries a human-readable reason traceable to a rule in the JSON file.

Same conventions as categorize.py / airlines.py: rules load once at import,
`reload_rules()` re-reads for interactive editing, and GET /api/analytics/rules
exposes the active set read-only."""
from __future__ import annotations

import json
import logging
import re
import time
from pathlib import Path
from typing import Any, Optional

from . import categorize
from .analytics import _sightings_where  # shared window-filter contract
from .settings import _connect  # type: ignore[attr-defined]


log = logging.getLogger("piscope.notable")

_DATA_PATH = Path(__file__).resolve().parent.parent / "data" / "notable_rules.json"
_HEX6_RE = re.compile(r"^[0-9a-f]{6}$")
_PREFIX_RE = re.compile(r"^[A-Z]{3}$")

_UNUSUAL_DEFAULTS = {
    "helicopter": True,
    "low_alt_max_ft": 2000,
    "high_alt_min_ft": 45000,
    "no_callsign_min_polls": 150,
    "no_callsign_ignore_commercial": True,
}


def _load_rules() -> dict[str, Any]:
    """Load + validate the rules file. Malformed entries are dropped with a
    warning; a missing/unreadable file yields an empty-but-shaped rule set so
    classification degrades to dbFlags-only rather than crashing."""
    empty = {"military_hex_ranges": [], "military_callsign_prefixes": {},
             "unusual": dict(_UNUSUAL_DEFAULTS)}
    try:
        raw = json.loads(_DATA_PATH.read_text(encoding="utf-8"))
    except (FileNotFoundError, json.JSONDecodeError) as exc:
        log.warning("notable_rules.json unavailable (%s); military rules limited to dbFlags", exc)
        return empty

    ranges: list[dict[str, str]] = []
    for entry in raw.get("military_hex_ranges") or []:
        if not isinstance(entry, dict):
            continue
        start = str(entry.get("start") or "").strip().lower()
        end = str(entry.get("end") or "").strip().lower()
        if not (_HEX6_RE.match(start) and _HEX6_RE.match(end)) or start > end:
            log.warning("notable_rules.json: dropping malformed hex range %r", entry)
            continue
        ranges.append({"start": start, "end": end,
                       "label": str(entry.get("label") or f"{start}-{end}")[:80]})

    prefixes: dict[str, str] = {}
    for code, label in (raw.get("military_callsign_prefixes") or {}).items():
        code = str(code).strip().upper()
        if not _PREFIX_RE.match(code) or not isinstance(label, str) or not label:
            log.warning("notable_rules.json: dropping malformed prefix %r", code)
            continue
        prefixes[code] = label[:80]

    unusual = dict(_UNUSUAL_DEFAULTS)
    raw_unusual = raw.get("unusual") or {}
    if isinstance(raw_unusual, dict):
        for key in ("helicopter", "no_callsign_ignore_commercial"):
            unusual[key] = bool(raw_unusual.get(key, _UNUSUAL_DEFAULTS[key]))
        for key in ("low_alt_max_ft", "high_alt_min_ft", "no_callsign_min_polls"):
            try:
                unusual[key] = int(raw_unusual.get(key, _UNUSUAL_DEFAULTS[key]))
            except (TypeError, ValueError):
                log.warning("notable_rules.json: non-numeric unusual.%s — using default", key)

    return {"military_hex_ranges": ranges, "military_callsign_prefixes": prefixes,
            "unusual": unusual}


_RULES: dict[str, Any] = _load_rules()


def rules() -> dict[str, Any]:
    """The active (validated) rule set — served read-only by /api/analytics/rules."""
    return _RULES


def reload_rules() -> dict[str, Any]:
    global _RULES
    _RULES = _load_rules()
    return _RULES


# --- classification -------------------------------------------------------------


def hex_range_label(hex_id: Optional[str]) -> Optional[str]:
    h = (hex_id or "").strip().lower()
    if not _HEX6_RE.match(h):
        return None   # '~'-prefixed TIS-B pseudo-hexes etc. never match
    for r in _RULES["military_hex_ranges"]:
        if r["start"] <= h <= r["end"]:
            return r["label"]
    return None


def military_prefix_label(callsign: Optional[str]) -> Optional[str]:
    cs = (callsign or "").strip().upper()
    # Same airline-style shape contract as airlines.extract_prefix — three
    # letters then a digit, so registrations can't false-positive.
    if len(cs) >= 4 and cs[:3] in _RULES["military_callsign_prefixes"] and cs[3].isdigit():
        return _RULES["military_callsign_prefixes"][cs[:3]]
    return None


def classify_sighting(row: dict[str, Any]) -> list[dict[str, str]]:
    """All rule matches for one sighting-shaped dict (hex, callsign, type_code,
    military, max_alt, min_alt, polls). Returns [{rule, label}, ...] — empty
    when nothing notable."""
    out: list[dict[str, str]] = []
    if row.get("military"):
        out.append({"rule": "military_db_flag", "label": "Military (aircraft DB flag)"})
    range_label = hex_range_label(row.get("hex"))
    if range_label:
        out.append({"rule": "military_hex_range", "label": range_label})
    prefix_label = military_prefix_label(row.get("callsign"))
    if prefix_label:
        out.append({"rule": "military_callsign", "label": prefix_label})

    u = _RULES["unusual"]
    type_code = (row.get("type_code") or "").strip().upper()
    if u["helicopter"] and type_code and \
            categorize.category_for({"type_code": type_code}) == "helicopter":
        out.append({"rule": "helicopter", "label": f"Helicopter ({type_code})"})
    min_alt = row.get("min_alt")
    if min_alt is not None and min_alt < u["low_alt_max_ft"]:
        out.append({"rule": "low_altitude", "label": f"Low altitude ({min_alt:,} ft)"})
    max_alt = row.get("max_alt")
    if max_alt is not None and max_alt > u["high_alt_min_ft"]:
        out.append({"rule": "high_altitude", "label": f"High altitude ({max_alt:,} ft)"})
    if row.get("callsign") is None and (row.get("polls") or 0) >= u["no_callsign_min_polls"]:
        # Airliners with no decoded ident are fringe-reception artifacts, not
        # anomalies — skipped by default (no_callsign_ignore_commercial).
        is_commercial = (u["no_callsign_ignore_commercial"] and type_code
                         and categorize.category_for({"type_code": type_code}) == "commercial")
        if not is_commercial:
            out.append({"rule": "no_callsign", "label": "No callsign (sustained contact)"})
    return out


_MILITARY_RULES = {"military_db_flag", "military_hex_range", "military_callsign"}


def military_where() -> tuple[str, list[Any]]:
    """SQL filter for military/government sightings: live dbFlags bit OR a hex
    allocation range OR a military callsign prefix. Unlike the unusual rules,
    this SQL is EXACT (not a superset) — fixed-width lowercase hex sorts like
    its numeric value, and the GLOB+IN prefix test matches classify_sighting's
    logic precisely — so analytics.overview uses it directly for the military
    totals chip, keeping the chip and the notable panel counting the same
    aircraft by construction."""
    ors = ["military = 1"]
    params: list[Any] = []
    for r in _RULES["military_hex_ranges"]:
        ors.append("hex BETWEEN ? AND ?")
        params.extend((r["start"], r["end"]))
    prefixes = list(_RULES["military_callsign_prefixes"])
    if prefixes:
        marks = ",".join("?" * len(prefixes))
        ors.append(f"(substr(callsign, 1, 3) IN ({marks}) "
                   "AND callsign GLOB '[A-Z][A-Z][A-Z][0-9]*')")
        params.extend(prefixes)
    return "(" + " OR ".join(ors) + ")", params


def _candidate_filter() -> tuple[str, list[Any]]:
    """SQL OR-filter selecting every sighting that COULD match a rule, so the
    Python classification pass only touches candidates. Mirrors
    classify_sighting — keep the two in sync when adding rules."""
    mil_sql, params = military_where()
    ors = [mil_sql]
    u = _RULES["unusual"]
    if u["helicopter"]:
        helis = sorted(categorize.types_for_category("helicopter"))
        if helis:
            marks = ",".join("?" * len(helis))
            ors.append(f"type_code IN ({marks})")
            params.extend(helis)
    ors.append("(min_alt IS NOT NULL AND min_alt < ?)")
    params.append(u["low_alt_max_ft"])
    ors.append("(max_alt IS NOT NULL AND max_alt > ?)")
    params.append(u["high_alt_min_ft"])
    ors.append("(callsign IS NULL AND polls >= ?)")
    params.append(u["no_callsign_min_polls"])
    return "(" + " OR ".join(ors) + ")", params


def notable_in_window(since_ts: Optional[float], limit: int = 80) -> dict[str, Any]:
    """Aircraft in the window matching any rule, grouped military vs unusual
    (anything with a military-class reason lands in military, with its other
    reasons attached), plus emergency-squawk events from the events pipeline."""
    limit = max(1, min(int(limit or 80), 200))
    sight_where, sight_params = _sightings_where(since_ts)
    cand_where, cand_params = _candidate_filter()

    with _connect() as conn:
        rows = conn.execute(
            "SELECT hex, date, first_ts, last_ts, polls, callsign, registration, "
            "type_code, military, max_alt, min_alt, max_gs, max_range_nm "
            f"FROM aircraft_sightings WHERE {sight_where} AND {cand_where} "
            "ORDER BY last_ts DESC LIMIT 2000",
            [*sight_params, *cand_params],
        ).fetchall()

        if since_ts is None:
            ev_rows = conn.execute(
                "SELECT ts, hex, callsign, registration, distance_nm, payload FROM events "
                "WHERE kind = 'emergency' ORDER BY ts DESC LIMIT 25").fetchall()
        else:
            ev_rows = conn.execute(
                "SELECT ts, hex, callsign, registration, distance_nm, payload FROM events "
                "WHERE kind = 'emergency' AND ts >= ? ORDER BY ts DESC LIMIT 25",
                (since_ts,),
            ).fetchall()

    # Merge per-day rows into one entry per hex, unioning reasons across days.
    by_hex: dict[str, dict[str, Any]] = {}
    for r in rows:
        reasons = classify_sighting(dict(r))
        if not reasons:
            continue   # candidate filter is a superset; Python pass is authoritative
        entry = by_hex.get(r["hex"])
        if entry is None:
            entry = {
                "hex": r["hex"], "callsign": r["callsign"],
                "registration": r["registration"], "type_code": r["type_code"],
                "days_seen": 0, "first_seen": r["first_ts"], "last_seen": r["last_ts"],
                "max_alt": r["max_alt"], "min_alt": r["min_alt"],
                "reasons": [], "_rule_keys": set(),
            }
            by_hex[r["hex"]] = entry
        entry["days_seen"] += 1
        entry["first_seen"] = min(entry["first_seen"], r["first_ts"])
        entry["last_seen"] = max(entry["last_seen"], r["last_ts"])
        for field in ("callsign", "registration", "type_code"):
            if r[field] and not entry[field]:
                entry[field] = r[field]
        for reason in reasons:
            if reason["rule"] not in entry["_rule_keys"]:
                entry["_rule_keys"].add(reason["rule"])
                entry["reasons"].append(reason)

    military, unusual = [], []
    for entry in sorted(by_hex.values(), key=lambda e: -e["last_seen"]):
        rule_keys = entry.pop("_rule_keys")
        (military if rule_keys & _MILITARY_RULES else unusual).append(entry)

    emergencies = []
    for r in ev_rows:
        item = {"ts": r["ts"], "hex": r["hex"], "callsign": r["callsign"],
                "registration": r["registration"], "distance_nm": r["distance_nm"]}
        if r["payload"]:
            try:
                p = json.loads(r["payload"])
                item["squawk"] = p.get("squawk")
                item["type_code"] = p.get("type_code")
            except (json.JSONDecodeError, TypeError):
                pass
        emergencies.append(item)

    return {
        "military": military[:limit],
        "unusual": unusual[:limit],
        "emergencies": emergencies,
        "candidates_scanned": len(rows),
    }


# --- returning aircraft -----------------------------------------------------------


def returning_in_window(since_ts: Optional[float], min_days: int = 2,
                        limit: int = 100) -> list[dict[str, Any]]:
    """Hexes seen on ≥ min_days distinct UTC days (over the whole retained ledger
    — "returning" is inherently cross-window) that were active inside the window.
    `is_new` marks aircraft whose very first sighting falls inside the window,
    i.e. they BECAME returning visitors during it — that's what the weekly digest
    calls out."""
    min_days = max(2, min(int(min_days or 2), 365))
    limit = max(1, min(int(limit or 100), 500))

    with _connect() as conn:
        rows = conn.execute(
            "SELECT hex, COUNT(DISTINCT date) AS days_seen, MIN(first_ts) AS first_seen, "
            "       MAX(last_ts) AS last_seen, SUM(polls) AS total_polls, "
            "       MAX(military) AS military "
            "FROM aircraft_sightings "
            "GROUP BY hex "
            "HAVING days_seen >= ? AND last_seen >= ? "
            "ORDER BY days_seen DESC, last_seen DESC LIMIT ?",
            (min_days, since_ts or 0, limit),
        ).fetchall()
        out = [dict(r) for r in rows]
        latest: dict[str, dict[str, Any]] = {}
        if out:
            # Backfill latest-known identity attrs (callsign rotates daily for
            # airline frames; show the most recent non-null per hex).
            marks = ",".join("?" * len(out))
            attr_rows = conn.execute(
                "SELECT hex, callsign, registration, type_code FROM aircraft_sightings "
                f"WHERE hex IN ({marks}) ORDER BY date ASC",
                [r["hex"] for r in out],
            ).fetchall()
            for r in attr_rows:
                slot = latest.setdefault(r["hex"], {})
                for field in ("callsign", "registration", "type_code"):
                    if r[field]:
                        slot[field] = r[field]

    for r in out:
        attrs = latest.get(r["hex"], {})
        r["callsign"] = attrs.get("callsign")
        r["registration"] = attrs.get("registration")
        r["type_code"] = attrs.get("type_code")
        r["military"] = bool(r["military"])
        r["is_new"] = bool(since_ts and r["first_seen"] >= since_ts)
    return out


def window_since(range_key: str) -> Optional[float]:
    """Translate a 24h/7d/30d/all range key to a since-timestamp, sharing
    analytics.RANGE_SECONDS so the two endpoints can't drift. Raises ValueError
    on unknown keys (mapped to HTTP 400 by the router)."""
    from .analytics import RANGE_SECONDS
    if range_key not in RANGE_SECONDS:
        raise ValueError(f"range must be one of {'/'.join(RANGE_SECONDS)}")
    span = RANGE_SECONDS[range_key]
    return (time.time() - span) if span else None
