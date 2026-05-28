"""API tests via FastAPI's TestClient — route wiring, response shapes, validation."""
from __future__ import annotations

import sys
import pathlib

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))


# Parameter-free, side-effect-free GET routes that read only from the (empty) temp DB.
# Excludes enrichment routes (hit external services) and routes needing query/path params.
SMOKE_GETS = [
    "/piscope/api/version",
    "/piscope/api/health",
    "/piscope/api/events",
    "/piscope/api/stats",
    "/piscope/api/records",
    "/piscope/api/bookmarks",
    "/piscope/api/coverage",
    "/piscope/api/leaderboard",
    "/piscope/api/notes",
    "/piscope/api/webhooks",
    "/piscope/api/views",
    "/piscope/api/digest",
    "/piscope/api/metrics",
    "/piscope/api/explain/status",
    "/piscope/api/dashboard/categorize",
    "/piscope/api/dashboard/events/stats",
]


def test_smoke_get_routes_non_5xx(client):
    for path in SMOKE_GETS:
        r = client.get(path)
        assert r.status_code < 500, f"{path} returned {r.status_code}: {r.text[:200]}"


def test_version_shape(client):
    r = client.get("/piscope/api/version")
    assert r.status_code == 200
    assert "version" in r.json()


def test_dashboard_summary_envelope(client):
    r = client.get("/piscope/api/dashboard/summary", params={"lat": 55.5, "lon": -2.75, "radius_km": 50})
    assert r.status_code == 200
    body = r.json()
    assert body["success"] is True
    assert body["error"] is None
    data = body["data"]
    assert "counts" in data and "total" in data["counts"]
    assert data["observer"] == {"lat": 55.5, "lon": -2.75}


def test_dashboard_summary_rejects_bad_coords(client):
    r = client.get("/piscope/api/dashboard/summary", params={"lat": 999, "lon": 0})
    assert r.status_code == 400
    assert r.json()["success"] is False


def test_settings_whitelist_drops_unknown_keys(client):
    # set_many should silently drop keys not in DEFAULTS.
    r = client.post("/piscope/api/settings", json={"theme": "nord", "bogus_key_xyz": 123})
    assert r.status_code == 200
    got = client.get("/piscope/api/settings").json()
    assert got["theme"] == "nord"
    assert "bogus_key_xyz" not in got


def test_settings_rejects_empty_body(client):
    r = client.post("/piscope/api/settings", json={})
    assert r.status_code == 400


def test_secret_keys_redacted(client):
    # Store a secret, confirm it comes back redacted with a _set flag rather than the value.
    client.post("/piscope/api/settings/openaip-key", json={"key": "supersecret123"})
    got = client.get("/piscope/api/settings").json()
    assert got["openaip_api_key"] == "***"
    assert got["openaip_api_key_set"] is True


def test_explain_requires_hex_422(client):
    # AircraftBrief makes hex required → Pydantic 422 (validation happens before the
    # is_configured 503 check because the body fails to parse).
    r = client.post("/piscope/api/explain", json={"callsign": "BAW1"})
    assert r.status_code == 422


def test_explain_not_configured_503(client):
    # Fresh DB → no AI provider configured → 503.
    r = client.post("/piscope/api/explain", json={"hex": "abc123"})
    assert r.status_code == 503


def test_followup_question_too_long_422(client):
    r = client.post("/piscope/api/explain/followup", json={
        "aircraft": {"hex": "abc123"},
        "history": [],
        "question": "x" * 600,   # over the 500-char cap
    })
    assert r.status_code == 422


def test_webhook_test_invalid_url_400(client):
    r = client.post("/piscope/api/webhooks/test", json={"kind": "discord", "url": "not-a-url"})
    assert r.status_code == 400


def test_aircraft_snapshot_shape(client):
    r = client.get("/piscope/api/aircraft")
    assert r.status_code == 200
    body = r.json()
    assert body["type"] == "aircraft_update"
    assert "aircraft" in body


def test_bookmarks_roundtrip_via_api(client):
    r = client.post("/piscope/api/bookmarks/abc123", json={"label": "fave", "callsign": "BAW1"})
    assert r.status_code == 200
    listed = client.get("/piscope/api/bookmarks").json()["bookmarks"]
    assert any(b["hex"] == "abc123" for b in listed)
    assert client.delete("/piscope/api/bookmarks/abc123").status_code == 200
    assert client.get("/piscope/api/bookmarks").json()["bookmarks"] == []


def test_bookmarks_reject_bad_hex(client):
    assert client.post("/piscope/api/bookmarks/NOTAHEX", json={}).status_code == 400


def test_notes_roundtrip_via_api(client):
    r = client.put("/piscope/api/notes/abc123", json={"note": "seen over the bridge"})
    assert r.status_code == 200
    got = client.get("/piscope/api/notes/abc123").json()
    assert got["note"] == "seen over the bridge"


def test_notes_reject_bad_hex(client):
    assert client.put("/piscope/api/notes/ZZZ", json={"note": "x"}).status_code == 400


def test_webhooks_save_coerces_and_cleans(client):
    # Lenient save: a bogus kind coerces to "generic", a non-http url entry is dropped,
    # types are filtered to the known set.
    r = client.post("/piscope/api/webhooks", json={"webhooks": [
        {"kind": "weird", "url": "https://discord.com/x", "types": ["emergency", "nonsense"]},
        {"kind": "discord", "url": "ftp://nope"},   # dropped (bad scheme)
    ]})
    assert r.status_code == 200
    saved = r.json()["webhooks"]
    assert len(saved) == 1
    assert saved[0]["kind"] == "generic"
    assert saved[0]["types"] == ["emergency"]


def test_webhooks_save_keeps_system_event_types(client):
    # Regression for B4: feed_down / feed_recovered / digest must survive the save filter.
    # The UI offers these checkboxes and the watchdog + digest fan out to them; previously
    # they were silently stripped, so those notifications never fired.
    r = client.post("/piscope/api/webhooks", json={"webhooks": [
        {"kind": "discord", "url": "https://discord.com/api/webhooks/x",
         "types": ["emergency", "feed_down", "feed_recovered", "digest", "bogus"]},
    ]})
    assert r.status_code == 200
    saved = r.json()["webhooks"]
    assert len(saved) == 1
    assert set(saved[0]["types"]) == {"emergency", "feed_down", "feed_recovered", "digest"}


def test_metrics_prometheus_format(client):
    r = client.get("/piscope/api/metrics")
    assert r.status_code == 200
    assert "piscope_uptime_seconds" in r.text
    assert r.headers["content-type"].startswith("text/plain")


def test_export_returns_zip_without_secrets(client):
    # Guards the S1 refactor end-to-end: /api/export must still produce a zip, and the
    # bundled SQL must not contain stored secret values.
    import io, zipfile
    client.post("/piscope/api/settings/fa-key", json={"key": "EXPORT-SECRET-FA"})
    r = client.get("/piscope/api/export")
    assert r.status_code == 200
    assert r.headers["content-type"] == "application/zip"
    with zipfile.ZipFile(io.BytesIO(r.content)) as zf:
        sql = zf.read("piscope.sql").decode("utf-8")
    assert "EXPORT-SECRET-FA" not in sql


def test_import_restores_db_and_invalidates_cache(client, monkeypatch):
    # Regression for S2: a real export round-tripped back through import must take effect.
    # Pre-fix the swap was unlink-then-rename (a no-DB window) and the in-memory settings
    # cache was never invalidated, so reads kept returning pre-import values. Stub the feed
    # lifecycle so the real poll loop doesn't start during the test.
    import os
    from app.services import feed as feed_mod
    from app.services import settings as settings_store

    async def _noop():
        return None
    monkeypatch.setattr(feed_mod.feed_service, "stop", _noop)
    monkeypatch.setattr(feed_mod.feed_service, "start", _noop)

    client.post("/piscope/api/settings", json={"theme": "nord"})
    exp = client.get("/piscope/api/export")
    assert exp.status_code == 200
    # Mutate AFTER export so we can prove the import overwrote it and refreshed the cache.
    client.post("/piscope/api/settings", json={"theme": "radar"})

    r = client.post("/piscope/api/import",
                    files={"file": ("backup.zip", exp.content, "application/zip")})
    assert r.status_code == 200, r.text
    assert client.get("/piscope/api/settings").json()["theme"] == "nord"
    # The recovery snapshot of the pre-import DB was written.
    assert os.path.exists(str(settings_store.DB_PATH) + ".pre-import.bak")


def test_dashboard_events_stats_shape(client):
    r = client.get("/piscope/api/dashboard/events/stats")
    assert r.status_code == 200
    body = r.json()
    assert set(body) >= {"subscribers", "ring_size", "latest_event_id"}


def test_dashboard_events_rejects_out_of_range_coords(client):
    # Validation happens before the SSE stream starts, so these return 400 immediately.
    assert client.get("/piscope/api/dashboard/events", params={"lat": 91, "lon": 0}).status_code == 400
    assert client.get("/piscope/api/dashboard/events", params={"lat": 0, "lon": 200}).status_code == 400
    assert client.get("/piscope/api/dashboard/events", params={"lat": 0, "lon": -200}).status_code == 400


def test_require_valid_observer_bounds():
    # Regression for B1: the observer-coord validator must accept ALL valid longitudes,
    # including the western hemisphere [-180, -90) that the old -90 lower bound rejected
    # (San Francisco -122.4, Honolulu -157.8, etc). Tested directly so we never open the
    # (infinite) SSE stream — keeps the suite fast.
    import pytest
    from fastapi import HTTPException
    from app.routers.api import _require_valid_observer

    for lat, lon in [(None, None), (0, 0), (37.6, -122.4), (21.3, -157.8), (-89.9, -179.9), (90, 180), (-90, -180)]:
        _require_valid_observer(lat, lon)   # in range → must not raise

    for lat, lon in [(91, 0), (-91, 0), (0, 200), (0, -200)]:
        with pytest.raises(HTTPException) as ei:
            _require_valid_observer(lat, lon)
        assert ei.value.status_code == 400
