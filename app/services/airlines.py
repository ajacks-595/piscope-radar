"""Callsign-prefix → operator resolver for the analytics layer (analytics feature, phase 1).

ICAO flight IDs are a 3-letter airline designator followed by a flight number
("BAW123", "RRR4567"). This module extracts that prefix and resolves it against
the bundled `app/data/airline_codes.json` (full OpenFlights list + hand-edited
overrides — regenerate with tools/gen-airline-codes.py).

Registration-style callsigns used by GA traffic ("GABCD", "N123AB") deliberately
do NOT resolve: the prefix pattern requires three LETTERS followed by a DIGIT,
which registrations don't match (N-numbers have a digit by position 2-3; stripped
UK regs have a letter at position 4). They surface as "unresolved" in the
operator breakdown rather than being misattributed.

Same conventions as categorize.py: the file is loaded once at import and held in
memory; `reload_table()` re-reads it for interactive editing. Overrides win over
the generated airlines section so corrections survive regeneration.
"""
from __future__ import annotations

import json
import logging
import re
from pathlib import Path
from typing import Any, Optional


log = logging.getLogger("piscope.airlines")


_DATA_PATH = Path(__file__).resolve().parent.parent / "data" / "airline_codes.json"

# Three letters then a digit — anchored; anything else (regs, military stand-ins
# like "@@@@@@", blank) is "no operator prefix".
_PREFIX_RE = re.compile(r"^([A-Z]{3})\d")


def _load_table() -> dict[str, dict[str, Any]]:
    """Load {icao_prefix: operator-dict} from disk, overrides last so they win.
    Returns an empty dict if the file is missing or malformed — callers then
    resolve nothing, which is safe (operators just show as unresolved)."""
    try:
        raw = json.loads(_DATA_PATH.read_text(encoding="utf-8"))
    except (FileNotFoundError, json.JSONDecodeError) as exc:
        log.warning("airline_codes.json unavailable (%s); operators will be unresolved", exc)
        return {}
    cleaned: dict[str, dict[str, Any]] = {}
    for section in ("airlines", "overrides"):
        entries = raw.get(section) or {}
        if not isinstance(entries, dict):
            continue
        for code, info in entries.items():
            if not isinstance(code, str) or not isinstance(info, dict):
                continue
            code = code.strip().upper()
            name = info.get("name")
            if len(code) != 3 or not code.isalpha() or not isinstance(name, str) or not name:
                log.warning("airline_codes.json: dropping malformed entry %r in %s", code, section)
                continue
            cleaned[code] = {
                "icao": code,
                "name": name,
                "iata": info.get("iata"),
                "callsign": info.get("callsign"),
                "country": info.get("country"),
                "active": bool(info.get("active", False)),
            }
    return cleaned


_TABLE: dict[str, dict[str, Any]] = _load_table()


def extract_prefix(callsign: Optional[str]) -> Optional[str]:
    """Return the 3-letter ICAO designator prefix of an airline-style callsign,
    or None when the callsign doesn't follow the <AAA><digits> pattern."""
    if not callsign:
        return None
    m = _PREFIX_RE.match(callsign.strip().upper())
    return m.group(1) if m else None


def operator_for_prefix(prefix: Optional[str]) -> Optional[dict[str, Any]]:
    """Resolve a 3-letter designator to its operator dict, or None if unknown."""
    if not prefix:
        return None
    return _TABLE.get(prefix.strip().upper())


def operator_for_callsign(callsign: Optional[str]) -> Optional[dict[str, Any]]:
    """Convenience: extract_prefix + operator_for_prefix in one call."""
    return operator_for_prefix(extract_prefix(callsign))


def reload_table() -> int:
    """Re-read the codes file from disk. Returns the number of entries loaded."""
    global _TABLE
    _TABLE = _load_table()
    return len(_TABLE)


def table_size() -> int:
    return len(_TABLE)
