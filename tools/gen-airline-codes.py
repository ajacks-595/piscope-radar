#!/usr/bin/env python3
"""Regenerate app/data/airline_codes.json from the OpenFlights airlines dataset.

Usage:
    python3 tools/gen-airline-codes.py [path/to/airlines.dat]

Input:  airlines.dat from https://github.com/jpatokal/openflights (data/airlines.dat),
        CSV columns: id, name, alias, iata, icao, callsign, country, active.
        Nulls are the literal token \\N. License: Open Database License (ODbL).
Output: app/data/airline_codes.json — keyed by 3-letter ICAO designator.

Rules:
  - Keep every row whose ICAO designator is exactly three A-Z letters. The OpenFlights
    "active" flag is preserved but NOT used to filter: their own docs call it unreliable,
    and nearly all military operators (very much flying) are marked "N".
  - On duplicate ICAO codes (reassigned designators — ~34 in the dataset), prefer the
    active="Y" row; if still tied, the higher OpenFlights id (more recently added) wins.
  - The "overrides" section of an EXISTING output file is preserved verbatim across
    regeneration — that's the hand-edited surface for corrections and post-dataset
    additions (e.g. CNV / MMF). Overrides take precedence over "airlines" at load time
    (see app/services/airlines.py).

Stdlib only, matching the repo's no-new-dependencies rule.
"""
from __future__ import annotations

import csv
import json
import re
import sys
from datetime import datetime, timezone
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
OUT_PATH = REPO_ROOT / "app" / "data" / "airline_codes.json"
ICAO_RE = re.compile(r"^[A-Z]{3}$")

# Hand-curated supplements seeded on first generation only; afterwards the existing
# file's overrides section (which the user may have edited) is what gets preserved.
SEED_OVERRIDES = {
    "CNV": {"name": "United States Navy", "iata": None, "callsign": "CONVOY",
            "country": "United States", "active": True},
    "MMF": {"name": "NATO Multinational MRTT Unit", "iata": None, "callsign": "MULTI",
            "country": "Netherlands", "active": True},
}


def _clean(v: str) -> str | None:
    v = (v or "").strip()
    return None if v in ("", "\\N", "-", "N/A") else v


def main() -> int:
    src = Path(sys.argv[1]) if len(sys.argv) > 1 else Path("/tmp/airlines.dat")
    if not src.exists():
        print(f"input not found: {src}", file=sys.stderr)
        return 1

    rows = []
    with src.open(encoding="utf-8", newline="") as f:
        for raw in csv.reader(f):
            if len(raw) != 8:
                continue
            of_id, name, _alias, iata, icao, callsign, country, active = raw
            icao = (_clean(icao) or "").upper()
            name = _clean(name)
            if not ICAO_RE.match(icao) or not name:
                continue
            try:
                of_id_n = int(of_id)
            except ValueError:
                of_id_n = 0
            rows.append({
                "icao": icao, "of_id": of_id_n, "name": name,
                "iata": _clean(iata), "callsign": _clean(callsign),
                "country": _clean(country), "active": active.strip().upper() == "Y",
            })

    # Collision resolution: prefer active, then higher OpenFlights id.
    by_icao: dict[str, dict] = {}
    collisions = 0
    for r in sorted(rows, key=lambda r: (r["active"], r["of_id"])):
        if r["icao"] in by_icao:
            collisions += 1
        by_icao[r["icao"]] = r   # later (preferred) entries overwrite earlier ones

    airlines = {
        icao: {"name": r["name"], "iata": r["iata"], "callsign": r["callsign"],
               "country": r["country"], "active": r["active"]}
        for icao, r in sorted(by_icao.items())
    }

    # Preserve hand-edited overrides across regeneration.
    overrides = dict(SEED_OVERRIDES)
    if OUT_PATH.exists():
        try:
            existing = json.loads(OUT_PATH.read_text(encoding="utf-8"))
            if isinstance(existing.get("overrides"), dict):
                overrides = existing["overrides"]
        except (json.JSONDecodeError, OSError) as exc:
            print(f"warning: could not read existing overrides ({exc}); seeding defaults",
                  file=sys.stderr)

    out = {
        "_meta": {
            "schema_version": 1,
            "description": "ICAO 3-letter airline designator -> operator. Generated from the "
                           "OpenFlights airlines dataset (ODbL) by tools/gen-airline-codes.py; "
                           "do not hand-edit the 'airlines' section — put corrections and "
                           "additions in 'overrides', which wins at load time and survives "
                           "regeneration.",
            "source": "https://github.com/jpatokal/openflights (data/airlines.dat)",
            "license": "ODbL (OpenFlights database)",
            "generated_at": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
            "airline_count": len(airlines),
            "collisions_resolved": collisions,
        },
        "overrides": overrides,
        "airlines": airlines,
    }
    OUT_PATH.write_text(json.dumps(out, ensure_ascii=False, indent=1) + "\n", encoding="utf-8")
    print(f"wrote {OUT_PATH}: {len(airlines)} airlines, "
          f"{len(overrides)} overrides, {collisions} collisions resolved")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
