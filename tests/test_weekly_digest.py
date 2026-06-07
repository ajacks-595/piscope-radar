"""Tests for the weekly summary (analytics feature, phase 4): schedule math,
builder/renderer, the mattermost webhook kind, and the manual-trigger endpoint."""
from __future__ import annotations

import time
from datetime import datetime

from app.services import digest as digest_svc
from app.services import webhooks
from app.services.analytics import SightingsBuffer, _utc_date
from app.services.settings import _connect  # type: ignore[attr-defined]
from app.models.aircraft import Aircraft


# --- schedule math ----------------------------------------------------------------


def test_seconds_until_weekly_exact():
    # Wed 2026-06-03 12:00 → next Sunday 19:00 is 4 days + 7 h away.
    now = datetime(2026, 6, 3, 12, 0)
    secs = digest_svc._seconds_until_weekly("sun", "19:00", now=now)
    assert secs == (4 * 24 + 7) * 3600


def test_seconds_until_weekly_same_day():
    now = datetime(2026, 6, 7, 12, 0)   # a Sunday
    assert digest_svc._seconds_until_weekly("sun", "19:00", now=now) == 7 * 3600
    # Time already past today → next week.
    assert digest_svc._seconds_until_weekly("sun", "08:00", now=now) == \
        7 * 86400 - 4 * 3600


def test_seconds_until_weekly_garbage_falls_back():
    now = datetime(2026, 6, 7, 12, 0)   # Sunday; fallback = sun 19:00
    assert digest_svc._seconds_until_weekly("blursday", "nonsense", now=now) == 7 * 3600
    # Accepts full day names via 3-char prefix.
    assert digest_svc._seconds_until_weekly("Sunday", "19:00", now=now) == 7 * 3600


# --- builder + renderer -----------------------------------------------------------


def _seed_week(now: float) -> None:
    buf = SightingsBuffer()
    buf.observe_poll([
        Aircraft(hex="43c0aa", callsign="RRR4567", type_code="A400",
                 altitude_baro=24000, distance_nm=60.0),
        Aircraft(hex="aaa001", callsign="GROTOR", type_code="R44",
                 altitude_baro=1500, distance_nm=8.0),
        Aircraft(hex="bbb002", callsign="BAW123", type_code="A320",
                 altitude_baro=35000, ground_speed=440.0, distance_nm=90.0),
    ], now - 3600)
    with _connect() as conn:
        buf.flush(conn, force=True)
        # Second day for the returning aircraft, first seen inside the window.
        conn.execute(
            "INSERT INTO aircraft_sightings(hex, date, first_ts, last_ts, polls, callsign) "
            "VALUES('bbb002', ?, ?, ?, 40, 'BAW456')",
            (_utc_date(now - 86400), now - 86400, now - 86000))
        conn.execute(
            "INSERT INTO daily_stats(date, total_polls, unique_aircraft, max_range_nm, "
            "emergencies, military_seen) VALUES(?, 500, 3, 90.0, 0, 1)",
            (_utc_date(now - 3600),))
        conn.execute(
            "INSERT INTO records(category, hex, callsign, value, recorded_at) "
            "VALUES('fastest', 'bbb002', 'BAW123', 440.0, ?)", (now - 3600,))
        conn.execute(
            "INSERT INTO events(ts, kind, hex, callsign, payload) "
            "VALUES(?, 'emergency', 'ccc003', 'XYZ12', '{\"squawk\": \"7600\"}')",
            (now - 7200,))
        conn.commit()


def test_build_weekly_digest_content(temp_db):
    now = time.time()
    _seed_week(now)
    d = digest_svc.build_weekly_digest()

    assert d["window_days"] == 7
    assert d["totals"]["unique_aircraft"] == 3
    assert {x["type_code"] for x in d["top_types"]} == {"A400", "R44", "A320"}
    op_names = {x["name"] for x in d["top_operators"]}
    assert "British Airways" in op_names and "Royal Air Force" in op_names
    assert d["notable_military"][0]["hex"] == "43c0aa"      # UK mil hex + RRR callsign
    assert {r["rule"] for r in d["notable_military"][0]["reasons"]} >= \
        {"military_hex_range", "military_callsign"}
    assert d["notable_unusual"][0]["type_code"] == "R44"
    assert d["emergencies"][0]["squawk"] == "7600"
    assert d["new_returning"][0]["hex"] == "bbb002"
    assert d["records_broken"][0]["category"] == "fastest"


def test_render_weekly_text(temp_db):
    now = time.time()
    _seed_week(now)
    text = digest_svc.render_weekly_text(digest_svc.build_weekly_digest())
    assert text.startswith("📊 **PiScope Radar — Weekly Summary**")
    for fragment in ("**Traffic:**", "3 unique aircraft", "**Top types:**",
                     "**Top operators:**", "British Airways",
                     "**Military/government:** RRR4567 (A400)",
                     "**Unusual:** GROTOR", "squawk 7600",
                     # Latest-day callsign wins the identity backfill (BAW123 is
                     # today's row; BAW456 was yesterday's).
                     "**New returning aircraft:** BAW123 (2 days)",
                     "**Records broken:**"):
        assert fragment in text, f"missing: {fragment}"


def test_render_weekly_text_quiet_week(temp_db):
    text = digest_svc.render_weekly_text(digest_svc.build_weekly_digest())
    assert "_A quiet week" in text


# --- webhook kind + delivery shape --------------------------------------------------


def test_mattermost_body_is_slack_shaped():
    ctype, body = webhooks._body_for("mattermost", "hello channel", {})
    assert ctype == "application/json"
    assert body == {"text": "hello channel"}


def test_weekly_digest_message_passthrough():
    text = webhooks._format_text("weekly_digest", {"message": "📊 **Weekly** body"})
    assert text == "📊 **Weekly** body"


def test_webhook_save_accepts_mattermost_and_weekly(client):
    r = client.post("/piscope/api/webhooks", json={"webhooks": [{
        "kind": "mattermost",
        "url": "https://mm.example.com/hooks/abc123",
        "types": ["weekly_digest", "digest", "bogus_type"],
    }]})
    assert r.status_code == 200
    saved = r.json()["webhooks"][0]
    assert saved["kind"] == "mattermost"
    assert saved["types"] == ["weekly_digest", "digest"]   # bogus filtered, order kept


# --- endpoint -----------------------------------------------------------------------


def test_weekly_run_endpoint(client, temp_db):
    now = time.time()
    _seed_week(now)
    r = client.post("/piscope/api/digest/weekly/run")
    assert r.status_code == 200
    data = r.json()
    assert data["ok"] is True
    assert data["digest"]["rendered_text"].startswith("📊")
    assert data["digest"]["delivery"]["webhook"] == "queued"
