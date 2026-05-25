"""Event log, daily stats, snapshot ring buffer, watchlist parsing."""
from __future__ import annotations

import sys
import pathlib

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))


def test_record_and_read_events(temp_db):
    from app.services import events
    events.record_event("military", hex="abc123", callsign="NATO01", payload={"type_code": "A400"})
    events.record_event("emergency", hex="def456", callsign="MAYDAY", payload={"squawk": "7700"})
    rows = events.recent_events(limit=10)
    assert len(rows) == 2
    kinds = {r["kind"] for r in rows}
    assert kinds == {"military", "emergency"}
    # payload round-trips back to a dict
    mil = next(r for r in rows if r["kind"] == "military")
    assert mil["payload"]["type_code"] == "A400"


def test_recent_events_kind_filter(temp_db):
    from app.services import events
    events.record_event("military", hex="a1")
    events.record_event("emergency", hex="a2")
    only = events.recent_events(limit=10, kind="emergency")
    assert len(only) == 1
    assert only[0]["kind"] == "emergency"


def test_record_event_rejects_bad_kind(temp_db):
    from app.services import events
    events.record_event("not_a_kind", hex="a1")
    assert events.recent_events(limit=10) == []


def test_daily_stats_roundtrip(temp_db):
    from app.services import events
    events.update_daily_stats(unique_hexes_today=12, max_range_nm_today=180.5,
                              emergencies_today=1, military_today=2)
    stats = events.get_stats(days=7)
    assert stats["days"]
    today = stats["days"][0]
    assert today["unique_aircraft"] == 12
    assert today["emergencies"] == 1


def test_snapshot_record_and_nearest(temp_db):
    from app.services import events
    snap = {"type": "aircraft_update", "aircraft": [{"hex": "a1", "lat": 55.5, "lon": -2.75}],
            "trails": {"a1": [[55.5, -2.75, 1.0]]}, "feeds": {}}
    events.record_snapshot(1000.0, snap)
    events.record_snapshot(2000.0, snap)
    got = events.snapshot_nearest(1900.0)   # closest to 2000
    assert got is not None
    assert got["_replay_ts"] == 2000.0
    # trails are stripped from the persisted snapshot to save space
    assert "trails" not in got


def test_prune_old_snapshots(temp_db):
    from app.services import events
    import time
    events.record_snapshot(time.time() - 10000, {"aircraft": []})  # old
    events.record_snapshot(time.time(), {"aircraft": []})          # fresh
    removed = events.prune_old_snapshots(max_age_seconds=3600)
    assert removed == 1
    assert len(events.snapshot_timeline()) == 1


def test_parse_watchlist():
    from app.services.events import parse_watchlist
    assert parse_watchlist("NATO01, g-eztj ,RRR7011") == ["NATO01", "G-EZTJ", "RRR7011"]
    assert parse_watchlist("") == []
    assert parse_watchlist(None) == []
