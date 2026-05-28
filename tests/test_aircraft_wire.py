"""aircraft_from_wire: parsing tar1090/adsb.lol JSON rows into Aircraft."""
from __future__ import annotations

import sys
import pathlib

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))


def test_basic_mapping():
    from app.models.aircraft import aircraft_from_wire
    ac = aircraft_from_wire({"hex": "4CA7B3", "flight": "RYR1234 ", "r": "EI-DYL",
                             "t": "B738", "lat": 53.1, "lon": -1.2, "alt_baro": 35000,
                             "gs": 450, "squawk": "1000"}, observed_at=123.0)
    assert ac is not None
    assert ac.hex == "4ca7b3"           # lowercased
    assert ac.callsign == "RYR1234"     # trimmed
    assert ac.registration == "EI-DYL"
    assert ac.type_code == "B738"
    assert ac.altitude_baro == 35000
    assert ac.on_ground is False
    assert ac.observed_at == 123.0


def test_ground_alt_baro():
    from app.models.aircraft import aircraft_from_wire
    ac = aircraft_from_wire({"hex": "abc123", "alt_baro": "ground"}, observed_at=1.0)
    assert ac.on_ground is True
    assert ac.altitude_baro is None


def test_military_dbflag():
    from app.models.aircraft import aircraft_from_wire
    mil = aircraft_from_wire({"hex": "43c000", "dbFlags": 1}, observed_at=1.0)
    civ = aircraft_from_wire({"hex": "406abc", "dbFlags": 0}, observed_at=1.0)
    assert mil.military is True
    assert civ.military is False


def test_missing_hex_returns_none():
    from app.models.aircraft import aircraft_from_wire
    assert aircraft_from_wire({"flight": "NOHEX"}, observed_at=1.0) is None
    assert aircraft_from_wire({"hex": ""}, observed_at=1.0) is None


def test_emergency_squawk_property():
    from app.models.aircraft import aircraft_from_wire
    ac = aircraft_from_wire({"hex": "a1", "squawk": "7600"}, observed_at=1.0)
    assert ac.is_emergency_squawk is True
    ac2 = aircraft_from_wire({"hex": "a2", "squawk": "1200"}, observed_at=1.0)
    assert ac2.is_emergency_squawk is False


# --- B2: defensive parsing — a hostile/garbage row must never raise -----------

def test_malformed_row_coerces_bad_numerics_to_none():
    # Pre-fix these raised TypeError downstream (bitwise & on a str, str/float compares,
    # math.radians(str)), aborting the entire poll cycle. Now they coerce to None.
    from app.models.aircraft import aircraft_from_wire
    ac = aircraft_from_wire(
        {"hex": "ABC123", "lat": "not-a-number", "gs": "fast", "dbFlags": "oops",
         "baro_rate": "x", "alt_baro": "garbage", "track": float("nan"),
         "squawk": 7700},
        observed_at=1.0,
    )
    assert ac is not None
    assert ac.lat is None and ac.ground_speed is None
    assert ac.baro_rate is None and ac.altitude_baro is None
    assert ac.track is None                # NaN dropped
    assert ac.military is False            # non-int dbFlags no longer crashes
    assert ac.squawk == "7700"             # numeric squawk stringified → emergency detectable
    assert ac.is_emergency_squawk is True
    assert ac.heading is None              # all heading sources absent/invalid


def test_numeric_string_coords_are_coerced_and_usable():
    # The exact crash path from the review: string lat/lon must become floats so the
    # feed loop's haversine_nm(...) doesn't raise "must be real number, not str".
    from app.models.aircraft import aircraft_from_wire, haversine_nm
    ac = aircraft_from_wire({"hex": "abc123", "lat": "51.5", "lon": "-0.12", "dst": "12.3"},
                            observed_at=1.0)
    assert ac.lat == 51.5 and ac.lon == -0.12 and ac.distance_nm == 12.3
    assert haversine_nm(50.0, 0.0, ac.lat, ac.lon) > 0   # would TypeError pre-fix


def test_non_dict_row_returns_none():
    from app.models.aircraft import aircraft_from_wire
    for bad in ("garbage", None, ["not", "a", "dict"], 42):
        assert aircraft_from_wire(bad, observed_at=1.0) is None
