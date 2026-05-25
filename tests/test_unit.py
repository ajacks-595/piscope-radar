"""Unit tests for the pure-logic helpers — no app server, no DB."""
from __future__ import annotations

import sys
import pathlib

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))


# --- haversine_nm -----------------------------------------------------------

def test_haversine_nm_one_degree_lon_at_equator():
    from app.models.aircraft import haversine_nm
    # 1° of longitude at the equator ≈ 60 nm.
    d = haversine_nm(0.0, 0.0, 0.0, 1.0)
    assert 59.0 < d < 61.0


def test_haversine_nm_zero_distance():
    from app.models.aircraft import haversine_nm
    assert haversine_nm(55.5, -2.75, 55.5, -2.75) == 0.0


# --- Aircraft.to_json (iter-10 flat literal) --------------------------------

def test_to_json_shape_and_derived_fields():
    from app.models.aircraft import Aircraft
    d = Aircraft(hex="abc123", callsign="BAW1", squawk="7700", baro_rate=1200,
                 altitude_baro=33000).to_json()
    # Derived fields must be present.
    for k in ("heading", "is_emergency_squawk", "display_name", "vertical_trend", "altitude_band"):
        assert k in d
    assert d["is_emergency_squawk"] is True       # 7700 is an emergency squawk
    assert d["display_name"] == "BAW1"
    assert d["vertical_trend"] == "climb"          # baro_rate > 100
    assert d["altitude_band"] == "high"            # 33000 → 25k–35k band
    # Raw fields present.
    assert d["hex"] == "abc123"
    assert d["squawk"] == "7700"


# --- SSRF validator ---------------------------------------------------------

def test_validate_external_url_allows_lan():
    from app.services._http import validate_external_url
    # Private LAN is allowed by design (tar1090 / LAN webhook receivers).
    assert validate_external_url("http://10.0.0.231/tar1090")
    assert validate_external_url("http://192.168.1.50:8123/x")


def test_validate_external_url_blocks_metadata_and_linklocal():
    import pytest
    from app.services._http import validate_external_url
    for bad in ("http://169.254.169.254/latest/meta-data",
                "http://metadata.google.internal/",
                "http://169.254.1.1/"):
        with pytest.raises(ValueError):
            validate_external_url(bad)


def test_validate_external_url_blocks_bad_scheme():
    import pytest
    from app.services._http import validate_external_url
    for bad in ("ftp://example.com", "file:///etc/passwd", "notaurl", ""):
        with pytest.raises(ValueError):
            validate_external_url(bad)


# --- Categorization ---------------------------------------------------------

def test_category_for_known_types():
    from app.services import categorize
    assert categorize.category_for({"type_code": "B738"}) == "commercial"
    assert categorize.category_for({"type_code": "R44"}) == "helicopter"
    assert categorize.category_for({"type_code": "C172"}) == "ga"


def test_category_for_military_flag_wins():
    from app.services import categorize
    # Live military flag overrides the type table.
    assert categorize.category_for({"type_code": "B738", "military": True}) == "military"


def test_category_for_unknown_type():
    from app.services import categorize
    assert categorize.category_for({"type_code": "ZZZZ"}) == "unknown"
    assert categorize.category_for({}) == "unknown"


def test_is_emergency_handles_none_string():
    from app.services import categorize
    # The "none" string must NOT count as an emergency (it's truthy).
    assert categorize.is_emergency({"emergency": "none"}) is False
    assert categorize.is_emergency({"is_emergency_squawk": True}) is True
    assert categorize.is_emergency({"emergency": "general"}) is True


# --- dashboard.build_summary ------------------------------------------------

def test_build_summary_counts_and_filters():
    from app.services import dashboard
    observer = {"lat": 55.5, "lon": -2.75}
    aircraft = [
        {"hex": "a1", "type_code": "B738", "lat": 55.5, "lon": -2.75, "altitude_baro": 35000, "ground_speed": 450, "heading": 90},
        {"hex": "a2", "type_code": "R44", "lat": 55.6, "lon": -2.75, "altitude_baro": 1200, "ground_speed": 90, "heading": 180},
        {"hex": "a3", "type_code": "B739", "military": True, "lat": 55.5, "lon": -2.8, "altitude_baro": 20000, "ground_speed": 400, "heading": 270},
        {"hex": "far", "type_code": "A320", "lat": 0.0, "lon": 0.0},  # far outside radius
    ]
    data = dashboard.build_summary(aircraft, observer_lat=observer["lat"], observer_lon=observer["lon"],
                                   radius_km=100, top=5)
    counts = data["counts"]
    assert counts["total"] == 3                  # 'far' excluded by radius
    assert counts["commercial"] == 1
    assert counts["helicopter"] == 1
    assert counts["military"] == 1
    assert data["nearest"]["icao"] == "a1"       # co-located with observer
    # filter to commercial only
    data2 = dashboard.build_summary(aircraft, observer_lat=observer["lat"], observer_lon=observer["lon"],
                                    radius_km=100, filters="commercial")
    assert data2["counts"]["total"] == 1


def test_haversine_km_matches_known():
    from app.services.dashboard import haversine_km
    # 1° latitude ≈ 111 km.
    d = haversine_km(0.0, 0.0, 1.0, 0.0)
    assert 110.0 < d < 112.0


# --- AI prompt sanitisers ---------------------------------------------------

def test_common_sanitisers():
    from app.services.ai import _common
    # _bounded_int clamps + rejects out of range
    assert _common._bounded_int(35000, -2000, 65000) == 35000
    assert _common._bounded_int(99999999, -2000, 65000) is None
    assert _common._bounded_int("notanumber", 0, 10) is None
    # _safe_text strips control chars + caps length
    assert _common._safe_text("hi\x00\x07there") == "hithere"
    assert len(_common._safe_text("x" * 200, 80)) == 80


def test_build_followup_prompt_threads_history():
    from app.services.ai import _common
    prompt = _common.build_followup_prompt(
        {"hex": "abc123", "type_code": "B789"},
        [{"role": "assistant", "content": "It's a 787."},
         {"role": "user", "content": "What engines?"}],
        "And the range?",
        max_turns=5,
    )
    assert "And the range?" in prompt
    assert "It's a 787." in prompt
    assert "B789" in prompt
