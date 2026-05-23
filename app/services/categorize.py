"""Aircraft category resolver for the dashboard summary API (iter 9.2).

Maps an aircraft dict (as held in `feed_service`'s in-memory contact set) to
one of:

    "military"   — live `military` flag from the feed, or known-military type code
    "helicopter" — rotorcraft type code (R44, EC135, AW139, …)
    "commercial" — scheduled-airline airframe (B738, A320, E190, ATR72, …)
    "ga"         — general aviation + business/corporate jets/turboprops
    "unknown"    — type_code absent or not in the curated table

The lookup data lives in `app/data/type_categories.json`. Add entries there
as new types show up; no code change needed. The file is loaded once at
import time and held in memory — re-import the module (or restart the
service) to pick up edits.

We deliberately do NOT heuristically guess for unknowns. Defaulting unknowns
to "ga" undercounts commercial; defaulting to "commercial" overcounts and
misclassifies corporate jets. "unknown" is honest, and the summary API
returns it as its own bucket so the dashboard can decide how to render it.
"""
from __future__ import annotations

import json
import logging
from pathlib import Path
from typing import Any, Optional


log = logging.getLogger("piscope.categorize")


_DATA_PATH = Path(__file__).resolve().parent.parent / "data" / "type_categories.json"

CATEGORIES = ("commercial", "military", "helicopter", "ga", "unknown")


def _load_table() -> dict[str, str]:
    """Load type_code -> category from disk. Returns an empty dict if the
    file is missing or malformed — callers fall back to 'unknown' for
    everything, which is safe."""
    try:
        raw = json.loads(_DATA_PATH.read_text(encoding="utf-8"))
    except (FileNotFoundError, json.JSONDecodeError) as exc:
        log.warning("type_categories.json unavailable (%s); categories will be 'unknown'", exc)
        return {}
    cats = raw.get("categories") or {}
    # Validate values are in our known set; drop bad entries with a warning.
    cleaned: dict[str, str] = {}
    for k, v in cats.items():
        if not isinstance(k, str) or not isinstance(v, str):
            continue
        if v not in CATEGORIES:
            log.warning("type_categories.json: %r maps to unknown category %r — dropping", k, v)
            continue
        cleaned[k.upper()] = v
    return cleaned


_TABLE: dict[str, str] = _load_table()


def category_for(ac: dict[str, Any]) -> str:
    """Return the dashboard category string for a single aircraft dict.

    Precedence:
      1. Live `military` flag from the feed (most authoritative — based on
         hex-range and operator/callsign rules in feed.py).
      2. Live `is_emergency_squawk` does NOT affect category — emergency is
         orthogonal and surfaced as its own counter in the summary.
      3. Type-code lookup in the curated table.
      4. 'unknown' fallback.
    """
    if ac.get("military"):
        return "military"
    type_code = ac.get("type_code")
    if isinstance(type_code, str) and type_code:
        cat = _TABLE.get(type_code.strip().upper())
        if cat:
            return cat
    return "unknown"


def is_emergency(ac: dict[str, Any]) -> bool:
    """Convenience predicate. The summary API counts emergencies separately
    from categories (an emergency commercial flight should count in BOTH
    `commercial` and `emergency`), so this is its own helper.

    Watch out: the live ADS-B `emergency` field is a *string* defaulting to
    "none". `bool("none")` is True — using `.get("emergency")` directly would
    flag every contact as emergency. We require either a known emergency
    squawk (7500/7600/7700) or an explicit non-"none" emergency-field value.
    """
    if ac.get("is_emergency_squawk"):
        return True
    em = ac.get("emergency")
    if isinstance(em, str) and em and em.lower() not in ("none", "no", "false", "0"):
        return True
    return False


def reload_table() -> int:
    """Re-read the categories file from disk. Returns the number of entries
    loaded. Useful for an admin endpoint or interactive editing."""
    global _TABLE
    _TABLE = _load_table()
    return len(_TABLE)


def table_size() -> int:
    return len(_TABLE)
