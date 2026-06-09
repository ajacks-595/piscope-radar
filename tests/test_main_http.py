"""Top-level HTTP behaviours: CSP frame-ancestors header + sw.js cache-tag rewrite.

These hit index() / sw.js, which read from static/, so the suite must be run with
static/ present alongside app/ (the case in the repo and in the Pi staging dir).
"""
from __future__ import annotations

import sys
import pathlib

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))


def test_index_default_csp_is_self(client):
    r = client.get("/piscope")
    assert r.status_code == 200
    csp = r.headers.get("content-security-policy")
    # Enforced CSP: frame-ancestors defaults to 'self' (no cross-origin embedding),
    # the always-safe hardening directives, and — since iteration 13 — the
    # nonce'd script-src (promoted from Report-Only after the iter-12 clean
    # real-browser pass).
    assert csp.startswith("frame-ancestors 'self'")
    assert "object-src 'none'" in csp
    assert "base-uri 'self'" in csp
    assert "script-src 'self' https://unpkg.com 'nonce-" in csp
    assert r.headers.get("content-security-policy-report-only") is None
    # The nonce in the header matches the one stamped onto the inline bootstrap.
    nonce = csp.split("'nonce-", 1)[1].split("'", 1)[0]
    assert f'<script nonce="{nonce}">' in r.text


def test_security_headers_on_api_and_static(client):
    r = client.get("/piscope/api/version")
    assert r.headers.get("x-content-type-options") == "nosniff"
    assert r.headers.get("referrer-policy") == "no-referrer"
    assert "geolocation=()" in (r.headers.get("permissions-policy") or "")
    # Versioned static assets are immutable-cached; unversioned ones are not.
    r = client.get("/piscope/static/app.css?v=9.9.9")
    assert "immutable" in (r.headers.get("cache-control") or "")
    r = client.get("/piscope/static/app.css")
    assert "immutable" not in (r.headers.get("cache-control") or "")


def test_host_guard_blocks_public_hostnames(client):
    from app.services import settings as s
    s.set_one("host_guard_enabled", True)   # opt-in; off by default
    # Enabled: a public FQDN in Host (the DNS-rebinding signature) is rejected…
    r = client.get("/piscope/api/version", headers={"host": "evil.example.com"})
    assert r.status_code == 421
    # …while LAN-shaped hosts pass: single-label (TestClient's own "testserver"),
    # private IP literals, mDNS names, loopback with port.
    for ok_host in ("testserver", "10.0.0.231", "piaware.local", "127.0.0.1:8765",
                    "192.168.1.5:8080", "100.71.2.3"):
        r = client.get("/piscope/api/version", headers={"host": ok_host})
        assert r.status_code == 200, ok_host


def test_host_guard_allow_list_and_disable(client):
    from app.services import settings as s
    s.set_one("host_guard_enabled", True)   # opt-in; off by default
    s.set_one("allowed_hosts", "piscope.my-tailnet.ts.net")
    r = client.get("/piscope/api/version", headers={"host": "piscope.my-tailnet.ts.net"})
    assert r.status_code == 200
    r = client.get("/piscope/api/version", headers={"host": "piscope.my-tailnet.ts.net:443"})
    assert r.status_code == 200
    r = client.get("/piscope/api/version", headers={"host": "other.example.net"})
    assert r.status_code == 421
    s.set_one("host_guard_enabled", False)
    r = client.get("/piscope/api/version", headers={"host": "other.example.net"})
    assert r.status_code == 200


def test_index_csp_reflects_frame_ancestors_setting(client):
    from app.services import settings as s
    s.set_one("frame_ancestors", "'self', http://10.0.0.188:8090")
    r = client.get("/piscope")
    csp = r.headers.get("content-security-policy")
    assert "http://10.0.0.188:8090" in csp
    assert csp.startswith("frame-ancestors 'self'")


def test_sw_js_cache_tag_rewritten(client):
    from app.main import VERSION
    r = client.get("/piscope/sw.js")
    assert r.status_code == 200
    body = r.text
    # The literal placeholder must have been replaced with the content-hash tag,
    # which embeds the running version (derived, so version bumps can't break this).
    assert "piscope-shell-rewritten" not in body
    assert f"piscope-shell-{VERSION.replace('.', '_')}-" in body
    assert r.headers.get("service-worker-allowed") == "/piscope"


def test_sw_js_has_csp(client):
    r = client.get("/piscope/sw.js")
    assert "frame-ancestors" in (r.headers.get("content-security-policy") or "")


def test_root_redirects_to_piscope(client):
    r = client.get("/", follow_redirects=False)
    assert r.status_code in (307, 308)
    assert r.headers["location"] == "/piscope"


def test_health_reports_feed_staleness(client):
    import time
    from app.services.feed import feed_service
    # Fresh poll → 200 ok.
    feed_service.last_poll_at = time.time()
    r = client.get("/piscope/health")
    assert r.status_code == 200
    assert r.json()["status"] == "ok"
    # Stale poll → 503 degraded, so an uptime monitor alerts on a wedged feed.
    feed_service.last_poll_at = time.time() - 600
    r = client.get("/piscope/health")
    assert r.status_code == 503
    body = r.json()
    assert body["status"] == "degraded"
    assert body["last_poll_age_s"] > 30
    feed_service.last_poll_at = 0.0   # reset for other tests
