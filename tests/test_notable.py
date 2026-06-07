"""Tests for notable-aircraft classification + returning-aircraft queries (phase 3)."""
from __future__ import annotations

import time

import pytest

from app.services import notable
from app.services.analytics import _utc_date
from app.services.settings import _connect  # type: ignore[attr-defined]


def _seed_sighting(hex_id: str, ts: float, *, callsign=None, registration=None,
                   type_code=None, military=0, max_alt=None, min_alt=None,
                   polls=10, date=None) -> None:
    with _connect() as conn:
        conn.execute(
            "INSERT OR REPLACE INTO aircraft_sightings(hex, date, first_ts, last_ts, polls, "
            "callsign, registration, type_code, military, max_alt, min_alt) "
            "VALUES(?,?,?,?,?,?,?,?,?,?,?)",
            (hex_id, date or _utc_date(ts), ts, ts + 600, polls,
             callsign, registration, type_code, military, max_alt, min_alt))
        conn.commit()


# --- rule loading + matchers -----------------------------------------------------


def test_rules_load_and_shape():
    r = notable.rules()
    assert r["military_hex_ranges"], "bundled ranges should load"
    assert all(len(x["start"]) == 6 for x in r["military_hex_ranges"])
    assert r["military_callsign_prefixes"]["RRR"].startswith("Royal Air Force")
    assert r["unusual"]["low_alt_max_ft"] == 2000
    assert r["unusual"]["no_callsign_min_polls"] == 150
    assert r["unusual"]["no_callsign_ignore_commercial"] is True
    assert notable.reload_rules()["unusual"]["helicopter"] is True


def test_hex_range_boundaries():
    assert notable.hex_range_label("adffff") is None          # one below US block
    assert "United States" in notable.hex_range_label("ae0000")
    assert "United States" in notable.hex_range_label("AFFFFF")  # case-insensitive
    assert notable.hex_range_label("b00000") is None          # one above
    assert "United Kingdom" in notable.hex_range_label("43c123")
    assert notable.hex_range_label("~aaaa1") is None          # TIS-B pseudo-hex
    assert notable.hex_range_label(None) is None


def test_military_prefix_matching():
    assert "Ascot" in notable.military_prefix_label("RRR4567")
    assert "Reach" in notable.military_prefix_label("rch299")  # case-insensitive
    assert notable.military_prefix_label("RRRX") is None       # needs digit at pos 4
    assert notable.military_prefix_label("GABCD") is None      # registration shape
    assert notable.military_prefix_label("BAW123") is None     # civilian prefix
    assert notable.military_prefix_label(None) is None


def test_classify_sighting_reasons():
    def rules_of(row):
        return {r["rule"] for r in notable.classify_sighting(row)}
    assert rules_of({"hex": "111111", "military": 1}) == {"military_db_flag"}
    assert rules_of({"hex": "ae1234"}) == {"military_hex_range"}
    assert rules_of({"hex": "111111", "callsign": "RRR123"}) == {"military_callsign"}
    assert rules_of({"hex": "111111", "type_code": "R44"}) == {"helicopter"}
    assert rules_of({"hex": "111111", "min_alt": 1200}) == {"low_altitude"}
    assert rules_of({"hex": "111111", "max_alt": 47000}) == {"high_altitude"}
    assert rules_of({"hex": "111111", "callsign": None, "polls": 200}) == {"no_callsign"}
    assert rules_of({"hex": "111111", "callsign": None, "polls": 100}) == set()
    # Commercial airframes with no decoded ident are reception artifacts — skipped.
    assert rules_of({"hex": "111111", "callsign": None, "polls": 200,
                     "type_code": "B789"}) == set()
    assert rules_of({"hex": "111111", "callsign": None, "polls": 200,
                     "type_code": "AW139"}) == {"no_callsign", "helicopter"}
    assert rules_of({"hex": "111111", "callsign": "BAW12", "type_code": "A320",
                     "min_alt": 30000, "max_alt": 38000, "polls": 50}) == set()
    # Combination: a military-hex helicopter at low level matches all three.
    assert rules_of({"hex": "43c001", "type_code": "AW139", "min_alt": 900}) == \
        {"military_hex_range", "helicopter", "low_altitude"}


# --- notable_in_window ------------------------------------------------------------


def test_notable_in_window_buckets_and_merge(temp_db):
    now = time.time()
    _seed_sighting("ae1234", now - 3600, callsign="RCH299", type_code="C17")
    _seed_sighting("aaa001", now - 3600, type_code="R44", callsign="GROTOR")
    _seed_sighting("bbb002", now - 3600, callsign="BAW123", type_code="A320",
                   min_alt=30000, max_alt=38000)            # plain airliner — absent
    _seed_sighting("ccc003", now - 90 * 86400, type_code="R66")   # out of window
    with _connect() as conn:                                 # emergency via events
        conn.execute("INSERT INTO events(ts, kind, hex, callsign, payload) "
                     "VALUES(?, 'emergency', 'ddd004', 'XYZ99', '{\"squawk\": \"7700\"}')",
                     (now - 1800,))
        conn.commit()

    out = notable.notable_in_window(now - 7 * 86400)
    mil_hexes = {e["hex"] for e in out["military"]}
    unusual_hexes = {e["hex"] for e in out["unusual"]}
    assert mil_hexes == {"ae1234"}
    assert unusual_hexes == {"aaa001"}
    assert "bbb002" not in mil_hexes | unusual_hexes
    assert "ccc003" not in mil_hexes | unusual_hexes

    mil = out["military"][0]
    assert {r["rule"] for r in mil["reasons"]} == {"military_hex_range", "military_callsign"}
    assert out["emergencies"][0]["squawk"] == "7700"
    assert out["emergencies"][0]["hex"] == "ddd004"


def test_notable_merges_multi_day_reasons(temp_db):
    now = time.time()
    # Same heli on two days; one day also flew low → reasons union, one entry.
    _seed_sighting("eee005", now - 86400, type_code="EC35", date=_utc_date(now - 86400))
    _seed_sighting("eee005", now - 3600, type_code="EC35", min_alt=800, date=_utc_date(now))
    out = notable.notable_in_window(now - 7 * 86400)
    assert len(out["unusual"]) == 1
    entry = out["unusual"][0]
    assert entry["days_seen"] == 2
    assert {r["rule"] for r in entry["reasons"]} == {"helicopter", "low_altitude"}


def test_military_where_is_exact(temp_db):
    """military_where() must match exactly what classify_sighting calls military —
    it feeds the analytics chip, which must agree with the notable panel."""
    now = time.time()
    _seed_sighting("ae1234", now)                                  # US hex block
    _seed_sighting("aaa001", now, callsign="RRR123")               # RAF prefix
    _seed_sighting("bbb002", now, military=1)                      # dbFlags only
    _seed_sighting("ccc003", now, callsign="BAW123")               # civilian
    _seed_sighting("ddd004", now, callsign="RRRX")                 # prefix, no digit
    sql, params = notable.military_where()
    with _connect() as conn:
        hexes = {r["hex"] for r in conn.execute(
            f"SELECT hex FROM aircraft_sightings WHERE {sql}", params)}
    assert hexes == {"ae1234", "aaa001", "bbb002"}


def test_analytics_military_chip_matches_notable_panel(temp_db):
    """Regression for the live finding: chip read 0 (dbFlags only) while the
    panel listed rule-matched military aircraft."""
    from app.services import analytics
    now = time.time()
    _seed_sighting("ae1234", now, callsign="RCH802", type_code="C17")   # no dbFlags!
    _seed_sighting("ccc003", now, callsign="BAW123", type_code="A320")
    data = analytics.overview("7d")
    panel = notable.notable_in_window(data["since"])
    assert data["totals"]["military_unique"] == 1
    assert data["totals"]["military_unique"] == len(panel["military"])


# --- returning_in_window ----------------------------------------------------------


def test_returning_counts_days_and_filters(temp_db):
    now = time.time()
    # 3 days incl. today; first seen long before the window → not new.
    for days_ago in (10, 1, 0):
        _seed_sighting("aaa111", now - days_ago * 86400, callsign=f"BAW{days_ago}")
    # 2 days, both within the last week → new returning visitor.
    for days_ago in (1, 0):
        _seed_sighting("bbb222", now - days_ago * 86400, military=1)
    # Single day → never returning.
    _seed_sighting("ccc333", now)
    # Multi-day but dormant (not active in window).
    for days_ago in (40, 30):
        _seed_sighting("ddd444", now - days_ago * 86400)

    week = notable.returning_in_window(now - 7 * 86400, min_days=2)
    by_hex = {r["hex"]: r for r in week}
    assert set(by_hex) == {"aaa111", "bbb222"}
    assert by_hex["aaa111"]["days_seen"] == 3
    assert by_hex["aaa111"]["is_new"] is False
    assert by_hex["aaa111"]["callsign"] == "BAW0"      # latest non-null attr wins
    assert by_hex["bbb222"]["is_new"] is True
    assert by_hex["bbb222"]["military"] is True

    everything = notable.returning_in_window(None, min_days=2)
    assert {r["hex"] for r in everything} == {"aaa111", "bbb222", "ddd444"}
    assert notable.returning_in_window(None, min_days=3)[0]["hex"] == "aaa111"
    assert len(notable.returning_in_window(None, min_days=3)) == 1


def test_returning_empty_ledger(temp_db):
    assert notable.returning_in_window(None) == []


# --- endpoints --------------------------------------------------------------------


def test_notable_endpoint_shape(client):
    r = client.get("/piscope/api/analytics/notable?range=7d")
    assert r.status_code == 200
    data = r.json()
    for key in ("military", "unusual", "emergencies", "candidates_scanned"):
        assert key in data
    assert client.get("/piscope/api/analytics/notable?range=nope").status_code == 400


def test_returning_endpoint_shape(client):
    r = client.get("/piscope/api/analytics/returning?range=30d&min_days=2")
    assert r.status_code == 200
    data = r.json()
    assert data["aircraft"] == []
    assert data["min_days"] == 2
    assert client.get("/piscope/api/analytics/returning?range=nope").status_code == 400


def test_rules_endpoint(client):
    r = client.get("/piscope/api/analytics/rules")
    assert r.status_code == 200
    data = r.json()
    assert data["rules"]["military_callsign_prefixes"]["RRR"]
    assert data["source"].endswith("notable_rules.json")
    assert "R44" in data["helicopter_types"]


def test_window_since():
    assert notable.window_since("all") is None
    assert notable.window_since("24h") == pytest.approx(time.time() - 86400, abs=5)
    with pytest.raises(ValueError):
        notable.window_since("1y")
