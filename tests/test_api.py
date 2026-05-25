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
