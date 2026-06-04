"""Regression tests for the 2026-06 review-fix batch.

One section per finding so a future breakage points straight at what regressed:

  P1-1  /api/import CSRF header + ATTACH-blocking authorizer
  P2-1  blocking DNS resolve offloaded off the event loop
  P2-2  blocking SMTP send offloaded off the event loop
  P3-1  badge squawk tooltip is HTML-escaped (static guard)
  P3-2  events_bus reconnect-replay loses no events under concurrent publish
  P3-3  /api/dashboard/summary recompute is rate-capped
  P3-4  AI endpoints are rate-capped
  P3-5  /piscope ships a nonce'd script-src CSP + nosniff
  P3-6  SSRF guard catches numeric-encoded + Alibaba metadata; LAN still allowed
  P3-7  shim compares the bearer token in constant time (static guard)
"""
from __future__ import annotations

import asyncio
import io
import os
import pathlib
import sys
import threading
import zipfile

import pytest

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))

REPO = pathlib.Path(__file__).resolve().parent.parent


def _zip_with_sql(sql: str) -> bytes:
    bio = io.BytesIO()
    with zipfile.ZipFile(bio, "w", zipfile.ZIP_DEFLATED) as zf:
        zf.writestr("piscope.sql", sql)
    return bio.getvalue()


# --- P1-1: import CSRF header + ATTACH authorizer ---------------------------

def test_import_rejects_missing_csrf_header(client):
    # multipart with no X-PiScope-Import header → 403 before anything is touched.
    r = client.post("/piscope/api/import",
                    files={"file": ("backup.zip", b"whatever", "application/zip")})
    assert r.status_code == 403
    assert "X-PiScope-Import" in r.json()["detail"]


def test_import_authorizer_blocks_attach(client, tmp_path):
    # A malicious dump that tries to ATTACH a DB outside the temp file must be
    # refused by the restore authorizer (→ 400), and the target file must NOT
    # be created.
    evil = tmp_path / "evil_attached.db"
    assert not evil.exists()
    sql = f"ATTACH DATABASE '{evil}' AS evil;\nCREATE TABLE evil.pwned(x);\n"
    r = client.post("/piscope/api/import",
                    files={"file": ("backup.zip", _zip_with_sql(sql), "application/zip")},
                    headers={"X-PiScope-Import": "1"})
    assert r.status_code == 400, r.text
    assert not evil.exists(), "authorizer let ATTACH create a file outside the temp DB"


def test_import_authorizer_allows_normal_dump(client, monkeypatch):
    # A clean dump (no ATTACH) still restores fine through the authorizer.
    from app.services import feed as feed_mod

    async def _noop():
        return None
    monkeypatch.setattr(feed_mod.feed_service, "stop", _noop)
    monkeypatch.setattr(feed_mod.feed_service, "start", _noop)

    client.post("/piscope/api/settings", json={"theme": "nord"})
    exp = client.get("/piscope/api/export")
    r = client.post("/piscope/api/import",
                    files={"file": ("backup.zip", exp.content, "application/zip")},
                    headers={"X-PiScope-Import": "1"})
    assert r.status_code == 200, r.text


# --- P2-1: blocking DNS resolve runs off the event loop ---------------------

def test_validate_external_url_async_resolves_off_loop(monkeypatch):
    from app.services import _http

    main_ident = threading.get_ident()
    seen = {}

    def fake_getaddrinfo(host, *a, **k):
        seen["ident"] = threading.get_ident()
        return [(2, 1, 6, "", ("93.184.216.34", 0))]   # example.com, public IP

    monkeypatch.setattr(_http.socket, "getaddrinfo", fake_getaddrinfo)
    out = asyncio.run(_http.validate_external_url_async("http://example.com/x", resolve=True))
    assert out == "http://example.com/x"
    assert seen["ident"] != main_ident, "getaddrinfo ran on the event-loop thread"


# --- P2-2: blocking SMTP send runs off the event loop -----------------------

def test_digest_email_send_offloaded(temp_db, monkeypatch):
    from app.services import digest as digest_mod
    from app.services import settings as settings_store

    main_ident = threading.get_ident()
    seen = {}

    def fake_send_email(text):
        seen["ident"] = threading.get_ident()
        return True, None

    monkeypatch.setattr(digest_mod, "_send_email", fake_send_email)
    settings_store.set_one("digest_deliver_email", True)

    digest = asyncio.run(digest_mod.run_digest(with_ai=False))
    assert digest["delivery"].get("email") == "ok"
    assert seen["ident"] != main_ident, "_send_email ran on the event-loop thread"


# --- P3-1: badge squawk tooltip escaped (static guard on app.js) ------------

def test_badge_squawk_is_escaped():
    app_js = (REPO / "static" / "app.js").read_text(encoding="utf-8")
    # The EMG badge tooltip must run the squawk through escapeHtml, not interpolate it raw.
    assert 'title="Squawk ${escapeHtml(ac.squawk)}"' in app_js
    assert 'title="Squawk ${ac.squawk}"' not in app_js


# --- P3-2: events_bus replay loses nothing under concurrent publish ---------

def test_events_bus_no_loss_when_publish_races_replay():
    from app.services import events_bus as bus
    bus._ring.clear(); bus._subscribers.clear(); bus._next_id = 1

    async def run():
        e1 = bus.publish("emergency", hex="a1")          # ring: [e1]
        agen = bus.subscribe(start_after_id=0)
        first = await agen.__anext__()                   # attaches queue, replays e1
        # "the gap": publish AFTER the queue is attached. Old code attached the
        # queue only after draining the ring, so this event was lost.
        e2 = bus.publish("military", hex="a2")
        second = await asyncio.wait_for(agen.__anext__(), timeout=1.0)
        third_pub = bus.publish("rare", hex="a3")
        third = await asyncio.wait_for(agen.__anext__(), timeout=1.0)
        await agen.aclose()
        return [first.id, second.id, third.id], [e1.id, e2.id, third_pub.id]

    got, expected = asyncio.run(run())
    assert got == expected == [1, 2, 3]
    # No duplicates.
    assert len(set(got)) == 3


def test_events_bus_replay_dedups_ring_and_queue():
    # Regression-safety: even when an event sits in both the ring snapshot and the
    # live queue, subscribe() yields it exactly once.
    from app.services import events_bus as bus
    bus._ring.clear(); bus._subscribers.clear(); bus._next_id = 1
    bus.publish("emergency", hex="a1")
    bus.publish("military", hex="a2")

    async def run():
        agen = bus.subscribe(start_after_id=0)
        out = []
        out.append(await agen.__anext__())   # e1
        out.append(await agen.__anext__())   # e2
        # Drive a live event so the while-loop is exercised; it must not re-emit e1/e2.
        bus.publish("rare", hex="a3")
        out.append(await asyncio.wait_for(agen.__anext__(), timeout=1.0))
        await agen.aclose()
        return [e.id for e in out]

    ids = asyncio.run(run())
    assert ids == [1, 2, 3]


# --- P3-3 / P3-4: rate caps ------------------------------------------------

def test_ratelimit_allows_then_blocks():
    from app.services import ratelimit
    ratelimit.reset()
    assert all(ratelimit.allow("t", limit=3, window_s=60.0) for _ in range(3))
    assert ratelimit.allow("t", limit=3, window_s=60.0) is False
    # A different bucket is independent.
    assert ratelimit.allow("other", limit=1, window_s=60.0) is True


def test_dashboard_summary_rate_capped(client, monkeypatch):
    from app.routers import api
    monkeypatch.setattr(api, "_DASHBOARD_RECOMPUTES_PER_MIN", 2)
    codes = []
    for i in range(3):
        # Distinct coords each call → cache miss each time → each counts.
        r = client.get("/piscope/api/dashboard/summary",
                       params={"lat": 50.0 + i * 0.5, "lon": -1.0, "radius_km": 25})
        codes.append(r.status_code)
    assert codes[:2] == [200, 200]
    assert codes[2] == 429


def test_dashboard_cache_hits_do_not_consume_rate_budget(client, monkeypatch):
    from app.routers import api
    monkeypatch.setattr(api, "_DASHBOARD_RECOMPUTES_PER_MIN", 1)
    # Same coords every time: first is a miss (counts), the rest are cache hits
    # (must NOT count) — so they keep returning 200, never 429.
    p = {"lat": 51.0, "lon": -1.0, "radius_km": 25}
    assert client.get("/piscope/api/dashboard/summary", params=p).status_code == 200
    for _ in range(5):
        assert client.get("/piscope/api/dashboard/summary", params=p).status_code == 200


def test_explain_rate_capped(client, monkeypatch):
    from app.routers import api
    monkeypatch.setattr(api, "_AI_CALLS_PER_MIN", 2)
    # Not configured → would be 503; the rate check runs first, so the 3rd call
    # is a 429 regardless of provider config.
    codes = [client.post("/piscope/api/explain", json={"hex": "abc123"}).status_code
             for _ in range(3)]
    assert codes[:2] == [503, 503]
    assert codes[2] == 429


# --- P3-5: CSP nonce + nosniff on the served HTML ---------------------------

def test_index_csp_has_nonce_and_hardening(client):
    import re
    r = client.get("/piscope")
    assert r.status_code == 200
    csp = r.headers.get("content-security-policy", "")
    assert "object-src 'none'" in csp
    assert "base-uri 'self'" in csp
    assert "frame-ancestors" in csp
    assert r.headers.get("x-content-type-options") == "nosniff"
    # script-src must carry a nonce, and that exact nonce must be stamped on the
    # one inline bootstrap <script>.
    m = re.search(r"script-src[^;]*'nonce-([A-Za-z0-9_-]+)'", csp)
    assert m, f"no nonce in script-src: {csp!r}"
    nonce = m.group(1)
    assert f'<script nonce="{nonce}">' in r.text


def test_index_csp_nonce_is_per_response(client):
    n1 = client.get("/piscope").headers["content-security-policy"]
    n2 = client.get("/piscope").headers["content-security-policy"]
    assert n1 != n2, "CSP nonce should be unique per response"


# --- P3-6: SSRF guard hardening --------------------------------------------

def test_ssrf_blocks_numeric_encoded_metadata():
    from app.services._http import validate_external_url
    # 2852039166 == 169.254.169.254 (link-local metadata) in the decimal form the
    # libc resolver honours. Must be blocked even though it's not a dotted literal.
    with pytest.raises(ValueError):
        validate_external_url("http://2852039166/latest/meta-data")


def test_ssrf_blocks_alibaba_metadata():
    from app.services._http import validate_external_url
    with pytest.raises(ValueError):
        validate_external_url("http://100.100.100.200/latest/meta-data")


def test_ssrf_still_allows_lan_and_loopback():
    from app.services._http import validate_external_url
    assert validate_external_url("http://10.0.0.231/tar1090")
    assert validate_external_url("http://127.0.0.1:11434/api/tags")
    assert validate_external_url("http://192.168.1.50:8123/x")


# --- P3-7: shim constant-time token compare (static guard) ------------------

def test_shim_uses_constant_time_compare():
    shim = (REPO / "tools" / "claude-shim" / "shim.py").read_text(encoding="utf-8")
    assert "hmac.compare_digest" in shim
    # The old timing-leaky comparison must be gone.
    assert 'if got != f"Bearer {BEARER_TOKEN}"' not in shim
