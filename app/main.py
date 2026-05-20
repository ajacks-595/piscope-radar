from __future__ import annotations

import logging
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI
from fastapi.responses import FileResponse, HTMLResponse, RedirectResponse
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
VERSION = "1.6.0"

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
    return HTMLResponse(html, headers={"Cache-Control": "no-store, must-revalidate"})


@app.get("/piscope/sw.js")
async def service_worker() -> FileResponse:
    """Service worker has to be served with `Service-Worker-Allowed: /piscope` so its scope
    can extend above its own URL path. Without this the PWA install silently fails."""
    return FileResponse(
        STATIC_DIR / "sw.js",
        media_type="application/javascript",
        headers={"Service-Worker-Allowed": "/piscope", "Cache-Control": "no-cache"},
    )


@app.get("/piscope/health")
async def health() -> dict[str, str]:
    return {"status": "ok"}
