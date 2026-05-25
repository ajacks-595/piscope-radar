"""Dashboard summary: overhead projection, numeric filters, highlight ranking, cache."""
from __future__ import annotations

import sys
import pathlib

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))

OBS = dict(observer_lat=55.50, observer_lon=-2.75)


def test_overhead_imminent_flags_inbound_aircraft():
    from app.services import dashboard
    # Aircraft due south of the observer, tracking north (0°) at 400 kt along the
    # same meridian → passes through the observer well inside 60 s.
    ac = [{"hex": "in1", "type_code": "B738", "lat": 55.40, "lon": -2.75,
           "altitude_baro": 20000, "ground_speed": 400, "heading": 0}]
    data = dashboard.build_summary(ac, radius_km=100, overhead_threshold_s=60,
                                   overhead_radius_km=2.0, **OBS)
    assert data["counts"]["overhead_imminent"] == 1
    assert data["overhead_imminent"][0]["icao"] == "in1"
    assert data["overhead_imminent"][0]["eta_overhead_s"] is not None


def test_overhead_ignores_outbound_aircraft():
    from app.services import dashboard
    # Same position but heading south (180°) — moving away, never overhead.
    ac = [{"hex": "out1", "type_code": "B738", "lat": 55.40, "lon": -2.75,
           "altitude_baro": 20000, "ground_speed": 400, "heading": 180}]
    data = dashboard.build_summary(ac, radius_km=100, **OBS)
    assert data["counts"]["overhead_imminent"] == 0


def test_numeric_filters():
    from app.services import dashboard
    ac = [
        {"hex": "lo", "type_code": "C172", "lat": 55.5, "lon": -2.75, "altitude_baro": 1500, "ground_speed": 90},
        {"hex": "hi", "type_code": "B738", "lat": 55.5, "lon": -2.75, "altitude_baro": 36000, "ground_speed": 470},
    ]
    only_high = dashboard.build_summary(ac, radius_km=50, min_alt=10000, **OBS)
    assert only_high["counts"]["total"] == 1
    assert only_high["nearest"]["icao"] == "hi"
    only_fast = dashboard.build_summary(ac, radius_km=50, min_speed=200, **OBS)
    assert only_fast["counts"]["total"] == 1


def test_highlights_rank_emergency_first():
    from app.services import dashboard
    ac = [
        {"hex": "near", "type_code": "B738", "lat": 55.50, "lon": -2.75, "altitude_baro": 35000, "ground_speed": 450, "heading": 90},
        {"hex": "emerg", "type_code": "A320", "lat": 55.9, "lon": -3.4, "altitude_baro": 12000, "ground_speed": 300, "heading": 90, "is_emergency_squawk": True},
    ]
    data = dashboard.build_summary(ac, radius_km=200, top=5, **OBS)
    # The emergency aircraft outranks the closer normal one.
    assert data["highlights"][0]["icao"] == "emerg"
    assert "emergency" in data["highlights"][0]["tags"]


def test_response_cache_ttl():
    from app.services import dashboard
    key = ("k", 1)
    assert dashboard.cache_get(key) is None
    dashboard.cache_set(key, {"hello": "world"})
    assert dashboard.cache_get(key) == {"hello": "world"}


def test_watchlist_match_tagging():
    from app.services import dashboard
    ac = [{"hex": "a1", "callsign": "NATO01", "type_code": "B738", "lat": 55.5, "lon": -2.75}]
    data = dashboard.build_summary(ac, radius_km=50, watchlist={"NATO01"}, **OBS)
    assert data["counts"]["watchlist"] == 1
    assert "watchlist" in data["nearest"]["tags"]
