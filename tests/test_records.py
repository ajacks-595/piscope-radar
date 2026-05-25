"""All-time records (bulk min/max logic) + bookmarks CRUD."""
from __future__ import annotations

import sys
import pathlib

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))


def _ac(**kw):
    from app.models.aircraft import Aircraft
    return Aircraft(**kw)


def test_update_records_bulk_min_max(temp_db):
    from app.services import records
    fleet = [
        _ac(hex="a1", distance_nm=12.0, altitude_baro=35000, ground_speed=480, on_ground=False),
        _ac(hex="a2", distance_nm=3.5, altitude_baro=900, ground_speed=120, on_ground=False),
        _ac(hex="a3", distance_nm=88.0, altitude_baro=41000, ground_speed=510, on_ground=False),
    ]
    records.update_records_bulk(fleet)
    recs = {r["category"]: r for r in records.all_records()}
    assert recs["closest_pass"]["value"] == 3.5     # min distance
    assert recs["longest_range"]["value"] == 88.0   # max distance
    assert recs["fastest"]["value"] == 510          # max speed
    assert recs["highest"]["value"] == 41000        # max altitude
    assert recs["lowest_alt"]["value"] == 900       # min altitude (>500 filter)


def test_records_only_improve(temp_db):
    from app.services import records
    records.update_records_bulk([_ac(hex="a1", distance_nm=10.0)])
    records.update_records_bulk([_ac(hex="a2", distance_nm=50.0)])  # worse for closest
    recs = {r["category"]: r for r in records.all_records()}
    assert recs["closest_pass"]["value"] == 10.0   # not overwritten by the farther one


def test_bookmarks_crud(temp_db):
    from app.services import records
    assert records.list_bookmarks() == []
    records.add_bookmark("ABC123", label="my fave", callsign="BAW1")
    assert records.has_bookmark("abc123") is True
    bms = records.list_bookmarks()
    assert len(bms) == 1 and bms[0]["label"] == "my fave"
    records.remove_bookmark("abc123")
    assert records.has_bookmark("abc123") is False
