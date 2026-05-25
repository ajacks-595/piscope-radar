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
