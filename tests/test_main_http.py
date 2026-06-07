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
    # Enforced CSP: frame-ancestors defaults to 'self' (no cross-origin embedding)
    # plus the always-safe hardening directives. script-src is NOT enforced yet —
    # it ships Report-Only until browser-verified.
    assert csp.startswith("frame-ancestors 'self'")
    assert "object-src 'none'" in csp
    assert "base-uri 'self'" in csp
    assert "script-src" not in csp
    ro = r.headers.get("content-security-policy-report-only")
    assert "script-src 'self' https://unpkg.com 'nonce-" in ro


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
