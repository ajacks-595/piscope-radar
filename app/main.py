from __future__ import annotations

import hashlib
import logging
import re
import time
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI
from fastapi.responses import FileResponse, HTMLResponse, RedirectResponse, Response
from fastapi.staticfiles import StaticFiles

from .routers import api as api_router
from .routers import ws as ws_router
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
VERSION = "1.5.0"

app = FastAPI(title="PiScope Radar", version=VERSION, lifespan=lifespan)


@app.get("/piscope/api/version")
async def version() -> dict[str, str]:
    return {"version": VERSION}

# Deliberately NO CORS middleware. Browsers enforce same-origin by default; without permissive
# CORS, a malicious cross-origin page cannot POST to /piscope/api/settings (which has no auth).
# If you need genuine cross-origin access (rare; Tailscale, custom DNS), add an explicit allow list
# here rather than re-introducing a wildcard.

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

def _csp_header() -> str:
    """Build the Content-Security-Policy header value from the user's
    frame_ancestors setting. Defaults to `'self'` (no cross-origin embedding)
    so a fresh deployment is safe by default."""
    raw = (settings_store.get("frame_ancestors") or "'self'").strip()
    # The setting is a free-form comma-separated string. Strip whitespace,
    # drop blanks, but otherwise pass tokens through verbatim — the admin
    # writes valid CSP source expressions (`'self'`, schemes, origins).
    tokens = [t.strip() for t in raw.split(",") if t.strip()]
    if not tokens:
        tokens = ["'self'"]
    return "frame-ancestors " + " ".join(tokens)


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
    # `no-store` is stronger than `no-cache` — the browser won't keep the HTML at all, so an
    # upgrade always delivers the freshly-versioned asset URLs on the very next navigation.
    return HTMLResponse(html, headers={
        "Cache-Control": "no-store, must-revalidate",
        "Content-Security-Policy": _csp_header(),
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
        },
    )


@app.get("/piscope/health")
async def health() -> dict[str, str]:
    return {"status": "ok"}
