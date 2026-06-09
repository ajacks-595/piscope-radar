"""Tests for the analytics feature (phase 1): schema v3 migration, the
SightingsBuffer write path, the airline-code resolver, the overview() query
layer, and the /api/analytics endpoint."""
from __future__ import annotations

import time
from datetime import datetime, timezone

import pytest

from app.models.aircraft import Aircraft
from app.services import airlines
from app.services import analytics
from app.services.analytics import SightingsBuffer, _utc_date
from app.services.settings import _connect  # type: ignore[attr-defined]


def _ac(hex_id="abc123", **kw) -> Aircraft:
    return Aircraft(hex=hex_id, **kw)


def _flush(buf: SightingsBuffer, force: bool = True) -> None:
    with _connect() as conn:
        buf.flush(conn, force=force)
        conn.commit()


def _rows(sql: str, params: tuple = ()) -> list:
    with _connect() as conn:
        return conn.execute(sql, params).fetchall()


# --- migration ----------------------------------------------------------------


def test_migration_creates_analytics_tables(temp_db):
    names = {r["name"] for r in _rows(
        "SELECT name FROM sqlite_master WHERE type='table'")}
    assert {"aircraft_sightings", "hourly_stats"} <= names
    assert _rows("PRAGMA user_version")[0][0] == 3
    indexes = {r["name"] for r in _rows(
        "SELECT name FROM sqlite_master WHERE type='index'")}
    assert "idx_sightings_date" in indexes


def test_migration_is_idempotent(temp_db):
    from app.services import settings as settings_store
    settings_store.init_db()   # second run on the same DB must not raise
    settings_store.init_db()
    assert _rows("PRAGMA user_version")[0][0] == 3


# --- SightingsBuffer ------------------------------------------------------------


def test_buffer_creates_and_merges_sighting_rows(temp_db):
    buf = SightingsBuffer()
    ts = time.time()
    buf.observe_poll([_ac(callsign="baw123 ", type_code="a320", altitude_baro=10000,
                          ground_speed=400.0, distance_nm=50.0)], ts)
    _flush(buf)
    row = _rows("SELECT * FROM aircraft_sightings")[0]
    assert row["hex"] == "abc123"
    assert row["date"] == _utc_date(ts)
    assert row["callsign"] == "BAW123"          # normalised: stripped + uppercased
    assert row["type_code"] == "A320"
    assert row["polls"] == 1
    assert row["max_alt"] == 10000 and row["min_alt"] == 10000
    assert row["max_gs"] == 400.0
    assert row["max_range_nm"] == 50.0 and row["min_range_nm"] == 50.0

    # Second flush: extremes widen (frame-continuous steps — the spike gate
    # ignores jumps > 5,000 ft/poll), polls accumulate, None never erases a value.
    buf.observe_poll([_ac(altitude_baro=14000, ground_speed=250.0, distance_nm=120.0)],
                     ts + 10)
    buf.observe_poll([_ac(altitude_baro=9500, distance_nm=5.0)], ts + 20)
    _flush(buf)
    row = _rows("SELECT * FROM aircraft_sightings")[0]
    assert row["polls"] == 3
    assert row["callsign"] == "BAW123"           # COALESCE kept the stored value
    assert row["max_alt"] == 14000 and row["min_alt"] == 9500
    assert row["max_gs"] == 400.0
    assert row["max_range_nm"] == 120.0 and row["min_range_nm"] == 5.0
    assert row["first_ts"] == pytest.approx(ts)
    assert row["last_ts"] == pytest.approx(ts + 20)


def test_buffer_rejects_implausible_extremes(temp_db):
    """A single garbage frame (decode glitch) must never poison a sighting's
    min/max — observed live: an A319 'at' 72,000 ft, an ATR 'doing' 1,885 kts."""
    buf = SightingsBuffer()
    ts = time.time()
    buf.observe_poll([_ac(altitude_baro=37000, ground_speed=450.0, distance_nm=80.0)], ts)
    buf.observe_poll([_ac(altitude_baro=90800, ground_speed=1885.4, distance_nm=4000.0)], ts + 2)
    _flush(buf)
    row = _rows("SELECT max_alt, max_gs, max_range_nm FROM aircraft_sightings")[0]
    assert row["max_alt"] == 37000
    assert row["max_gs"] == 450.0
    assert row["max_range_nm"] == 80.0


def test_buffer_altitude_spike_gate(temp_db):
    """In-envelope single-frame spikes (live example: A339 'at' 46,675 ft) must
    not register as extremes; the gate self-heals within one frame."""
    buf = SightingsBuffer()
    ts = time.time()
    buf.observe_poll([_ac(altitude_baro=37000)], ts)
    buf.observe_poll([_ac(altitude_baro=46675)], ts + 2)   # spike — gated
    buf.observe_poll([_ac(altitude_baro=37200)], ts + 4)   # lag frame (vs spike) — gated
    buf.observe_poll([_ac(altitude_baro=37400)], ts + 6)   # continuous again — accepted
    _flush(buf)
    row = _rows("SELECT max_alt FROM aircraft_sightings")[0]
    assert row["max_alt"] == 37400                          # never 46675

    # Coverage gap: extremes lag exactly one frame, then catch up.
    buf2 = SightingsBuffer()
    buf2.observe_poll([_ac("bbb222", altitude_baro=10000)], ts)
    buf2.observe_poll([_ac("bbb222", altitude_baro=25000)], ts + 600)  # gap jump — gated
    buf2.observe_poll([_ac("bbb222", altitude_baro=25100)], ts + 602)  # accepted
    _flush(buf2)
    row = _rows("SELECT max_alt FROM aircraft_sightings WHERE hex='bbb222'")[0]
    assert row["max_alt"] == 25100


def test_records_reject_implausible_extremes(temp_db):
    from app.services import records as records_store
    legit = _ac("aaa111", altitude_baro=41000, ground_speed=520.0, distance_nm=200.0)
    junk = _ac("bbb222", altitude_baro=126500, ground_speed=1885.4, distance_nm=3000.0)
    records_store.update_records_bulk([legit, junk])
    recs = {r["category"]: r for r in records_store.all_records()}
    assert recs["highest"]["value"] == 41000
    assert recs["fastest"]["value"] == 520.0
    assert recs["longest_range"]["value"] == 200.0
    # Direct (non-bulk) path applies the same envelope.
    records_store.update_records(junk)
    recs = {r["category"]: r for r in records_store.all_records()}
    assert recs["highest"]["value"] == 41000


def test_buffer_min_alt_floor_and_ground_skip(temp_db):
    buf = SightingsBuffer()
    ts = time.time()
    # 100 ft is below the 500 ft floor (ground-squitter guard) → min_alt stays None;
    # max_alt still records it.
    buf.observe_poll([_ac(altitude_baro=100)], ts)
    # On-ground reports contribute no altitude at all.
    buf.observe_poll([_ac(altitude_baro=12000, on_ground=True)], ts + 1)
    _flush(buf)
    row = _rows("SELECT max_alt, min_alt FROM aircraft_sightings")[0]
    assert row["max_alt"] == 100
    assert row["min_alt"] is None


def test_buffer_splits_rows_across_utc_days(temp_db):
    buf = SightingsBuffer()
    ts = datetime(2026, 6, 1, 23, 59, 30, tzinfo=timezone.utc).timestamp()
    buf.observe_poll([_ac()], ts)
    buf.observe_poll([_ac()], ts + 60)           # crosses midnight
    _flush(buf)
    rows = _rows("SELECT date, polls FROM aircraft_sightings ORDER BY date")
    assert [(r["date"], r["polls"]) for r in rows] == [
        ("2026-06-01", 1), ("2026-06-02", 1)]


def test_buffer_flush_cadence(temp_db):
    buf = SightingsBuffer()
    ts = time.time()
    with _connect() as conn:
        for i in range(analytics.SIGHTINGS_FLUSH_POLLS - 1):
            buf.observe_poll([_ac()], ts + i)
            buf.flush(conn)
        assert not _rows("SELECT * FROM aircraft_sightings")   # not due yet
        buf.observe_poll([_ac()], ts + 99)
        buf.flush(conn)                                        # 15th poll → due
        conn.commit()
    rows = _rows("SELECT polls FROM aircraft_sightings")
    assert len(rows) == 1 and rows[0]["polls"] == analytics.SIGHTINGS_FLUSH_POLLS


def test_hourly_row_counters_and_bands(temp_db):
    buf = SightingsBuffer()
    ts = datetime(2026, 6, 3, 14, 10, tzinfo=timezone.utc).timestamp()
    buf.observe_poll([
        _ac("aaa111", altitude_baro=5000, distance_nm=80.0),        # low
        _ac("bbb222", altitude_baro=36000, military=True),          # very_high
        _ac("ccc333"),                                              # no alt → no band
        _ac("ddd444", on_ground=True),                              # ground
    ], ts)
    _flush(buf)
    buf.observe_poll([_ac("aaa111", altitude_baro=5500)], ts + 2)
    _flush(buf)
    row = _rows("SELECT * FROM hourly_stats")[0]
    assert row["hour"] == "2026-06-03T14"
    assert row["unique_aircraft"] == 4
    assert row["obs"] == 5                       # 4 + 1 across the two polls
    assert row["military"] == 1
    assert row["alt_low"] == 2 and row["alt_very_high"] == 1 and row["alt_ground"] == 1
    assert row["alt_mid"] == 0                   # ccc333 (unknown alt) counted nowhere
    assert row["max_range_nm"] == 80.0


def test_hourly_unique_survives_restart_without_lowering(temp_db):
    ts = datetime(2026, 6, 3, 9, 5, tzinfo=timezone.utc).timestamp()
    buf = SightingsBuffer()
    buf.observe_poll([_ac("aaa111"), _ac("bbb222"), _ac("ccc333")], ts)
    _flush(buf)
    # Simulated restart: a fresh buffer (empty sets) sees only one aircraft.
    buf2 = SightingsBuffer()
    buf2.observe_poll([_ac("aaa111")], ts + 30)
    _flush(buf2)
    row = _rows("SELECT unique_aircraft FROM hourly_stats")[0]
    assert row["unique_aircraft"] == 3           # MAX() guard kept the larger count


def test_hour_rollover_resets_sets(temp_db):
    buf = SightingsBuffer()
    ts = datetime(2026, 6, 3, 9, 59, tzinfo=timezone.utc).timestamp()
    buf.observe_poll([_ac("aaa111"), _ac("bbb222")], ts)
    _flush(buf)
    buf.observe_poll([_ac("aaa111")], ts + 120)   # next hour
    _flush(buf)
    rows = _rows("SELECT hour, unique_aircraft FROM hourly_stats ORDER BY hour")
    assert [(r["hour"], r["unique_aircraft"]) for r in rows] == [
        ("2026-06-03T09", 2), ("2026-06-03T10", 1)]


def test_prune_old_analytics(temp_db):
    buf = SightingsBuffer()
    old = time.time() - 400 * 86400
    buf.observe_poll([_ac("aaa111")], old)
    _flush(buf)
    buf.observe_poll([_ac("bbb222")], time.time())
    _flush(buf)
    removed = analytics.prune_old_analytics(365)
    assert removed == 2                          # one sighting + one hourly row
    assert len(_rows("SELECT * FROM aircraft_sightings")) == 1
    assert analytics.prune_old_analytics(0) == 0          # disabled
    assert analytics.prune_old_analytics("garbage") == 0  # defensive


# --- restart-safe daily counters (phase 2) ---------------------------------------


def test_seen_hexes_for_date(temp_db):
    buf = SightingsBuffer()
    ts = time.time()
    buf.observe_poll([_ac("aaa111"), _ac("bbb222")], ts)
    _flush(buf)
    assert analytics.seen_hexes_for_date(_utc_date(ts)) == {"aaa111", "bbb222"}
    assert analytics.seen_hexes_for_date("1999-01-01") == set()


def test_feed_seeds_daily_counters_after_restart(temp_db):
    from app.services import events as events_store
    from app.services.feed import FeedService

    svc = FeedService()   # fresh instance — counters start empty
    ts = time.time()
    buf = SightingsBuffer()
    buf.observe_poll([_ac("aaa111"), _ac("bbb222")], ts)
    _flush(buf)
    events_store.record_event("emergency", hex="aaa111")
    events_store.record_event("military", hex="ccc333")

    assert not svc._daily_unique
    svc._seed_daily_counters()
    assert svc._daily_unique == {"aaa111", "bbb222"}
    assert svc._daily_emergencies == {"aaa111"}
    assert svc._daily_military == {"ccc333"}


def test_distinct_event_hexes_since_filters_kind_and_time(temp_db):
    from app.services import events as events_store
    events_store.record_event("military", hex="aaa111")
    events_store.record_event("rare", hex="bbb222")
    assert events_store.distinct_event_hexes_since(time.time() - 60, "military") == {"aaa111"}
    assert events_store.distinct_event_hexes_since(time.time() + 60, "military") == set()


# --- airline resolver -----------------------------------------------------------


def test_airline_table_loads_full_list():
    assert airlines.table_size() > 3000


def test_airline_prefix_resolution():
    assert airlines.operator_for_callsign("BAW123")["name"] == "British Airways"
    assert airlines.operator_for_callsign("rrr4567")["name"] == "Royal Air Force"
    # Overrides win / post-dataset additions resolve.
    assert airlines.operator_for_callsign("CNV7012")["name"] == "United States Navy"


def test_airline_rejects_registration_style_callsigns():
    assert airlines.extract_prefix("GABCD") is None      # UK reg, letter at pos 4
    assert airlines.extract_prefix("N123AB") is None     # N-number, digit at pos 2
    assert airlines.extract_prefix("") is None
    assert airlines.extract_prefix(None) is None
    assert airlines.operator_for_callsign("@@@@@@") is None


def test_airline_reload_table():
    n = airlines.reload_table()
    assert n == airlines.table_size() > 3000


# --- overview() query layer ------------------------------------------------------


def _seed_window_data(now: float) -> None:
    """One military A320 (BAW), one untyped GA reg, an old out-of-window row,
    plus matching daily/event/record rows."""
    buf = SightingsBuffer()
    buf.observe_poll([
        _ac("aaa111", callsign="BAW123", type_code="A320", altitude_baro=35000,
            ground_speed=420.0, distance_nm=90.0, military=True),
        _ac("bbb222", callsign="GABCD", altitude_baro=2000, distance_nm=10.0),
    ], now - 3600)
    _flush(buf)
    with _connect() as conn:
        conn.execute(
            "INSERT INTO aircraft_sightings(hex, date, first_ts, last_ts, polls, callsign, max_range_nm) "
            "VALUES('old001', '2000-01-01', 946684800, 946684800, 5, 'XXX111', 30.0)")
        conn.execute(
            "INSERT INTO daily_stats(date, total_polls, unique_aircraft, max_range_nm, emergencies, military_seen) "
            "VALUES(?, 100, 2, 90.0, 0, 1)", (_utc_date(now - 3600),))
        conn.execute(
            "INSERT INTO events(ts, kind, hex) VALUES(?, 'military', 'aaa111')",
            (now - 3600,))
        conn.execute(
            "INSERT INTO records(category, hex, value, recorded_at) "
            "VALUES('fastest', 'aaa111', 420.0, ?)", (now - 3600,))
        conn.commit()


def test_overview_window_aggregates(temp_db):
    now = time.time()
    _seed_window_data(now)
    data = analytics.overview("7d")

    assert data["range"] == "7d"
    assert data["totals"]["unique_aircraft"] == 2        # old001 outside window
    assert data["totals"]["military_unique"] == 1
    assert data["totals"]["events_by_kind"] == {"military": 1}
    assert data["totals"]["aircraft_days"] == 2

    types = {t["type_code"]: t for t in data["types"]}
    assert types["A320"]["unique_aircraft"] == 1
    assert types["A320"]["category"] == "commercial"

    ops = {o["prefix"]: o for o in data["operators"]}
    assert ops["BAW"]["name"] == "British Airways"
    assert "GAB" not in ops                              # reg-style callsign filtered
    cov = data["operator_coverage"]
    assert cov["with_callsign"] == 2 and cov["airline_style"] == 1 and cov["resolved"] == 1

    assert data["ranges"]["max_nm"] == 90.0
    assert data["ranges"]["histogram"][0]["count"] >= 1
    assert data["altitude_bands"]["very_high"] == 1 and data["altitude_bands"]["low"] == 1

    assert data["traffic"]["daily"][-1]["unique_aircraft"] == 2
    assert data["traffic"]["matrix"]                      # at least one cell
    assert data["traffic"]["busiest_hour"] is not None
    assert data["traffic"]["busiest_day"]["unique_aircraft"] == 2

    broken = {r["category"] for r in data["records"]["broken_in_window"]}
    assert "fastest" in broken
    assert data["meta"]["operator_table_size"] > 3000
    assert data["types_lifetime"] is None                # only populated for range=all


def test_overview_all_includes_backfill_and_lifetime(temp_db):
    now = time.time()
    _seed_window_data(now)
    data = analytics.overview("all")
    assert data["totals"]["unique_aircraft"] == 3        # old001 now included
    assert data["types_lifetime"] is not None            # seen_types view ships on "all"
    assert data["meta"]["sightings_coverage_start"] == "2000-01-01"
    assert data["records"]["broken_in_window"] == []     # meaningless without a window


def test_overview_rejects_unknown_range(temp_db):
    with pytest.raises(ValueError):
        analytics.overview("3y")


# --- endpoint -------------------------------------------------------------------


def test_analytics_endpoint_shape(client):
    r = client.get("/piscope/api/analytics?range=7d")
    assert r.status_code == 200
    data = r.json()
    for key in ("range", "traffic", "types", "operators", "operator_coverage",
                "altitude_bands", "ranges", "records", "totals", "meta"):
        assert key in data
    assert data["range"] == "7d"


def test_analytics_endpoint_default_and_bad_range(client):
    assert client.get("/piscope/api/analytics").json()["range"] == "7d"
    r = client.get("/piscope/api/analytics?range=bogus")
    assert r.status_code == 400


# --- per-aircraft history + CSV export (phase 5) -----------------------------


def test_aircraft_history_aggregates_across_days(temp_db):
    buf = SightingsBuffer()
    base = datetime(2026, 6, 1, 12, 0, tzinfo=timezone.utc).timestamp()
    # Same hex on two distinct UTC days.
    buf.observe_poll([_ac(callsign="baw9 ", type_code="a320", altitude_baro=10000,
                          ground_speed=400.0, distance_nm=50.0)], base)
    _flush(buf)
    buf2 = SightingsBuffer()
    buf2.observe_poll([_ac(altitude_baro=12000, distance_nm=80.0)], base + 86400)
    _flush(buf2)

    hist = analytics.aircraft_history("abc123")
    assert hist is not None
    assert hist["days_seen"] == 2
    assert hist["callsign"] == "BAW9"
    assert hist["extremes"]["max_alt"] == 12000
    assert hist["extremes"]["max_range_nm"] == 80.0
    assert len(hist["days"]) == 2
    assert analytics.aircraft_history("ffffff") is None


def test_aircraft_history_endpoint(client):
    buf = SightingsBuffer()
    buf.observe_poll([_ac(callsign="test1", type_code="b738", altitude_baro=20000)],
                     time.time())
    _flush(buf)
    r = client.get("/piscope/api/analytics/aircraft/abc123")
    assert r.status_code == 200 and r.json()["days_seen"] == 1
    assert client.get("/piscope/api/analytics/aircraft/ffffff").status_code == 404
    assert client.get("/piscope/api/analytics/aircraft/NOTHEX").status_code == 400


def test_csv_export(temp_db):
    buf = SightingsBuffer()
    buf.observe_poll([_ac(callsign="csv1", type_code="a319", altitude_baro=8000,
                          distance_nm=30.0)], time.time())
    _flush(buf)
    sightings = analytics.export_csv("sightings")
    assert sightings.splitlines()[0].startswith("hex,date,callsign")
    assert "abc123" in sightings and "A319" in sightings
    daily = analytics.export_csv("daily")
    assert daily.splitlines()[0].startswith("date,total_polls")
    with pytest.raises(ValueError):
        analytics.export_csv("bogus")


def test_csv_export_endpoint(client):
    r = client.get("/piscope/api/analytics/export?kind=daily")
    assert r.status_code == 200
    assert r.headers["content-type"].startswith("text/csv")
    assert "attachment" in r.headers.get("content-disposition", "")
    assert client.get("/piscope/api/analytics/export?kind=bogus").status_code == 400
