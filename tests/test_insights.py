"""Polar coverage, heatmap bucketing, leaderboard, type-sighting ledger, notes."""
from __future__ import annotations

import sys
import pathlib

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))


def test_polar_coverage_records_max_range(temp_db):
    from app.services import insights
    insights.update_polar(55.5, -2.75, hex_id="a1", lat=56.5, lon=-2.75, distance_nm=60.0)
    insights.update_polar(55.5, -2.75, hex_id="a2", lat=56.5, lon=-2.75, distance_nm=30.0)
    bins = insights.polar_coverage()
    assert len(bins) == 360
    # The due-north bearing (0) should hold the MAX of the two (60, not 30).
    north = bins[0]
    assert north["max_nm"] == 60.0


def test_heatmap_bucket_and_flush(temp_db):
    from app.services import insights
    from app.services import settings as s
    b1 = insights.heatmap_bucket(55.52, -2.75)
    b2 = insights.heatmap_bucket(55.52, -2.75)
    assert b1 == b2   # same coarse bucket
    with s.batch() as conn:
        insights.flush_heatmap_batch({b1: 3}, conn=conn)
    pts = insights.heatmap_points(top_n=100)
    assert len(pts) == 1
    assert pts[0][2] == 3   # hits


def test_record_type_sighting_new_detection(temp_db):
    from app.services import insights
    assert insights.record_type_sighting("B789") is True    # first time
    assert insights.record_type_sighting("B789") is False   # seen before
    lb = insights.leaderboard(limit=10)
    row = next(r for r in lb if r["type_code"] == "B789")
    assert row["sightings"] == 2


def test_flush_type_sightings_batch(temp_db):
    from app.services import insights
    from app.services import settings as s
    insights._KNOWN_TYPES = None  # reset cache for isolation
    with s.batch() as conn:
        new = insights.flush_type_sightings({"A320": 3, "B738": 1}, conn=conn)
    assert new == {"A320", "B738"}
    # second flush: not new, sightings accumulate
    with s.batch() as conn:
        new2 = insights.flush_type_sightings({"A320": 2}, conn=conn)
    assert new2 == set()
    lb = {r["type_code"]: r["sightings"] for r in insights.leaderboard(limit=10)}
    assert lb["A320"] == 5


def test_notes_crud(temp_db):
    from app.services import insights
    assert insights.get_note("abc123") is None
    insights.set_note("ABC123", "spotted over the bridge")
    assert insights.get_note("abc123") == "spotted over the bridge"   # hex lowercased
    assert insights.all_notes()["abc123"] == "spotted over the bridge"
    insights.set_note("abc123", "")   # empty deletes
    assert insights.get_note("abc123") is None
