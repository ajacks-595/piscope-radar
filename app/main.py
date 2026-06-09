from __future__ import annotations

import hashlib
import logging
import re
import secrets
import time
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Optional

from fastapi import FastAPI
from fastapi.responses import FileResponse, HTMLResponse, PlainTextResponse, RedirectResponse, Response
from fastapi.staticfiles import StaticFiles

from .routers import api as api_router
from .routers import ws as ws_router
from .services import hostguard
from .services import settings as settings_store
from .services import digest as digest_svc
from .services._http import close_client
from .services.feed import feed_service


logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)-7s %(name)s: %(message)s",
)
log = logging.getLogger("piscope")


BASE_DIR = Path(__file__).resolve().parent.parent
STATIC_DIR = BASE_DIR / "static"


@asynccontextmanager
async def lifespan(app: FastAPI):  # noqa: ARG001
    settings_store.init_db()
    await feed_service.start()
    digest_svc.start_scheduler()
    log.info("PiScope Radar started — open http://127.0.0.1:8765/piscope")
    try:
        yield
    finally:
        await digest_svc.stop_scheduler()
        await feed_service.stop()
        await close_client()


# Version stamp. Bump whenever you ship a notable user-facing change — the frontend reads
# this via /piscope/api/version and pops a "✨ What's new" toast on first load after a bump.
VERSION = "1.7.0"

app = FastAPI(title="PiScope Radar", version=VERSION, lifespan=lifespan)


@app.get("/piscope/api/version")
async def version() -> dict[str, str]:
    return {"version": VERSION}

# Deliberately NO CORS middleware. Browsers enforce same-origin by default; without permissive
# CORS, a malicious cross-origin page cannot POST to /piscope/api/settings (which has no auth).
# If you need genuine cross-origin access (rare; Tailscale, custom DNS), add an explicit allow list
# here rather than re-introducing a wildcard.


# --- Host guard + baseline security headers (iteration 13) -------------------
# Same-origin policy is the only privilege boundary on this no-auth box, and DNS
# rebinding bypasses it: a hostile page re-points its own public hostname at the
# Pi's private IP and then reads/writes the API "same-origin" (this also defeats
# the WS Origin==Host check). The rebound request still carries the attacker's
# public FQDN in Host, so rejecting non-LAN-shaped Hosts (421) closes the hole.
# See services/hostguard.py for what passes; `allowed_hosts` extends it and
# `host_guard_enabled=false` disables it. The header half adds the cheap,
# break-nothing response headers to EVERY response (previously only the two
# HTML/SW routes had nosniff, and static/API responses had none).

_HOST_GUARD_LOGGED: set[str] = set()


@app.middleware("http")
async def _host_guard_and_headers(request, call_next):
    if hostguard.enabled():
        host = request.headers.get("host", "")
        if not hostguard.host_allowed(host):
            if host not in _HOST_GUARD_LOGGED and len(_HOST_GUARD_LOGGED) < 100:
                _HOST_GUARD_LOGGED.add(host)
                log.warning("host guard rejected Host: %r (add to allowed_hosts "
                            "or set host_guard_enabled=false if legitimate)", host)
            return PlainTextResponse(
                "Misdirected request: this PiScope instance does not serve that host name.\n",
                status_code=421,
            )
    response = await call_next(request)
    response.headers.setdefault("X-Content-Type-Options", "nosniff")
    response.headers.setdefault("Referrer-Policy", "no-referrer")
    response.headers.setdefault("Permissions-Policy", "geolocation=(), camera=(), microphone=()")
    # Versioned static assets (the ?v=<VERSION> stamp from the index route) are
    # immutable by construction — a release bump changes the URL. Long-cache them
    # so reloads stop re-validating every asset against the Pi.
    if request.url.path.startswith("/piscope/static/") and "v" in request.query_params:
        response.headers.setdefault("Cache-Control", "public, max-age=31536000, immutable")
    return response


app.include_router(ws_router.router, prefix="/piscope")
app.include_router(api_router.router, prefix="/piscope")

if STATIC_DIR.exists():
    app.mount("/piscope/static", StaticFiles(directory=STATIC_DIR), name="static")


@app.get("/")
async def root() -> RedirectResponse:
    return RedirectResponse(url="/piscope")


# --- Security headers (iter 9.4) --------------------------------------------
# Frame-ancestors gates which parent origins can iframe the embed-mode page.
# Read fresh from settings on each request — cheap, and means an admin's
# `frame_ancestors` change takes effect without restart.

def _csp_header(script_nonce: Optional[str] = None) -> str:
    """Build the ENFORCED Content-Security-Policy header value.

    Base directives are universally safe: `frame-ancestors` (from the user's
    setting; defaults to `'self'` — no cross-origin embedding) plus
    `object-src 'none'` + `base-uri 'self'`. We deliberately do NOT set
    `default-src`, leaving styles / images / fonts / tiles / WebSocket
    connections unrestricted.

    When `script_nonce` is given (the served HTML), `script-src` is enforced
    too: `'self'` + the Leaflet CDN + the per-response nonce for the single
    inline bootstrap script — no `'unsafe-inline'`, so an injected <script>
    is blocked. Promoted from Report-Only in iteration 13 after a clean
    real-browser verification pass on the Pi (iteration 12)."""
    raw = (settings_store.get("frame_ancestors") or "'self'").strip()
    # The setting is a comma-separated string of CSP source expressions; the
    # settings layer restricts it to CSP-safe characters at write time.
    tokens = [t.strip() for t in raw.split(",") if t.strip()]
    if not tokens:
        tokens = ["'self'"]
    directives = [
        "frame-ancestors " + " ".join(tokens),
        "object-src 'none'",
        "base-uri 'self'",
    ]
    if script_nonce:
        directives.append(f"script-src 'self' https://unpkg.com 'nonce-{script_nonce}'")
    return "; ".join(directives)


# --- Service-worker cache tag (iter 9.4) ------------------------------------
# Used to be a hand-bumped constant in sw.js. Now the response body is
# rewritten on every fetch to substitute a content-hash-derived tag for the
# placeholder, so any deploy that touches static assets naturally invalidates
# the cache without anyone remembering to edit sw.js. The tag is cached for
# a short TTL so we don't re-hash the static dir on every poll.

_SHELL_FILES = ("index.html", "app.js", "app.css", "themes.css", "radar.js",
                "manifest.webmanifest", "sw.js")
_SW_CACHE_TAG_CACHE: dict[str, tuple[float, str]] = {}
_SW_CACHE_TAG_TTL_S = 60.0


def _shell_cache_tag() -> str:
    """Stable hash of (VERSION + concatenated shell-file contents). Bumps on
    any code change without a manual edit; consistent across processes so
    multiple uvicorn workers don't fight."""
    now = time.time()
    cached = _SW_CACHE_TAG_CACHE.get("tag")
    if cached and cached[0] > now:
        return cached[1]
    h = hashlib.sha256()
    h.update(VERSION.encode("utf-8"))
    for fname in _SHELL_FILES:
        p = STATIC_DIR / fname
        if p.exists():
            try:
                h.update(p.read_bytes())
            except OSError:
                pass
    tag = f"piscope-shell-{VERSION.replace('.', '_')}-{h.hexdigest()[:10]}"
    _SW_CACHE_TAG_CACHE["tag"] = (now + _SW_CACHE_TAG_TTL_S, tag)
    return tag


@app.get("/piscope")
async def index() -> HTMLResponse:
    # Stamp the running VERSION onto every same-origin static asset reference so a release
    # bump automatically invalidates the browser's HTTP cache. Without this, users have to
    # hard-reload after upgrades to see new JS/CSS — and most won't bother.
    html = (STATIC_DIR / "index.html").read_text(encoding="utf-8")
    for asset in ("themes.css", "app.css", "radar.js", "app.js"):
        html = html.replace(f'"/piscope/static/{asset}"', f'"/piscope/static/{asset}?v={VERSION}"')
    # Stamp a per-response nonce onto the single inline bootstrap <script> so it's
    # allowed under the script-src nonce policy. The external scripts (Leaflet CDN,
    # local radar.js/app.js) match 'self'/host-source and need no nonce; an injected
    # inline <script> has no nonce and is blocked. There is exactly one bare
    # "<script>" in index.html (the others are "<script src=...>").
    nonce = secrets.token_urlsafe(16)
    html = html.replace("<script>", f'<script nonce="{nonce}">', 1)
    # `no-store` is stronger than `no-cache` — the browser won't keep the HTML at all, so an
    # upgrade always delivers the freshly-versioned asset URLs on the very next navigation.
    return HTMLResponse(html, headers={
        "Cache-Control": "no-store, must-revalidate",
        # script-src is ENFORCED as of iteration 13 (was Report-Only through two
        # releases; the iteration-12 real-browser pass on the Pi was violation-free).
        "Content-Security-Policy": _csp_header(script_nonce=nonce),
        "X-Content-Type-Options": "nosniff",
    })


# Match `const CACHE = 'piscope-shell-vN';` (or any string) and replace just
# the cache name. Anchored to avoid catching anything else.
_SW_CACHE_RE = re.compile(r"(const\s+CACHE\s*=\s*)['\"][^'\"]*['\"]")


@app.get("/piscope/sw.js")
async def service_worker() -> Response:
    """Service worker has to be served with `Service-Worker-Allowed: /piscope` so its scope
    can extend above its own URL path. Without this the PWA install silently fails.

    Iter 9.4: the response body is rewritten to substitute the content-hash
    cache tag for the literal `CACHE` constant, so deploys never serve a
    stale shell cache to clients who already had the old SW installed."""
    src = (STATIC_DIR / "sw.js").read_text(encoding="utf-8")
    tag = _shell_cache_tag()
    src = _SW_CACHE_RE.sub(lambda m: f"{m.group(1)}'{tag}'", src, count=1)
    return Response(
        content=src,
        media_type="application/javascript",
        headers={
            "Service-Worker-Allowed": "/piscope",
            "Cache-Control": "no-cache",
            "Content-Security-Policy": _csp_header(),
            "X-Content-Type-Options": "nosniff",
        },
    )


@app.get("/piscope/health")
async def health() -> Response:
    """Liveness + feed-freshness probe for uptime monitors. Returns 200 while the
    feed has polled recently, 503 (status "degraded") once the last successful
    poll is older than a few poll intervals — so a monitor hitting /health alerts
    when the receiver/poller wedges even though the process is still up."""
    import json as _json
    h = feed_service.health()
    interval = max(1, int(settings_store.get("poll_interval") or 2))
    last_poll = feed_service.last_poll_at
    age = (time.time() - last_poll) if last_poll else None
    # Stale once we've missed ~5 polls (min 30 s). last_poll==0 means "still
    # starting up" — treat as ok so a monitor doesn't flap during boot.
    stale_after = max(30.0, interval * 5)
    degraded = age is not None and age > stale_after
    body = {
        "status": "degraded" if degraded else "ok",
        "feed_state": h.get("connection_state"),
        "last_poll_age_s": round(age, 1) if age is not None else None,
        "aircraft": h.get("aircraft_count"),
        "uptime_seconds": h.get("uptime_seconds"),
        "version": VERSION,
    }
    return Response(
        content=_json.dumps(body),
        media_type="application/json",
        status_code=503 if degraded else 200,
    )
