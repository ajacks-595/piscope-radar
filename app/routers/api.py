from __future__ import annotations

import logging
from typing import Any

import httpx
from fastapi import APIRouter, Body, HTTPException, Response
from fastapi.responses import JSONResponse

import io
import json
import re
import sqlite3
import time
import zipfile
from pathlib import Path

from fastapi import UploadFile, File

from ..services import adsbdb, events as events_store, flightaware, hexdb, insights as insights_store, planespotters
from ..services import records as records_store
from ..services import settings as settings_store
from ..services import ai as ai_svc
from ..services.ai import ollama as _ollama_provider
from ..services.ai import cloud_api as _cloud_api_provider
from ..services import digest as digest_svc
from ..services._http import LRUCache, get_client, reset_client
from ..services.feed import feed_service


log = logging.getLogger("piscope.api")

router = APIRouter(prefix="/api")


# --- aircraft snapshot (REST fallback for clients that don't speak WS) -------


@router.get("/aircraft")
async def aircraft_snapshot() -> dict[str, Any]:
    return feed_service.snapshot()


# --- enrichment endpoints ----------------------------------------------------


@router.get("/enrich/hexdb/{hex_id}")
async def enrich_hexdb(hex_id: str) -> dict[str, Any]:
    data = await hexdb.lookup(hex_id)
    if data is None:
        raise HTTPException(status_code=502, detail="hexdb lookup failed")
    return data or {}


@router.get("/enrich/adsbdb/{callsign}")
async def enrich_adsbdb(callsign: str) -> dict[str, Any]:
    data = await adsbdb.lookup(callsign)
    if data is None:
        raise HTTPException(status_code=502, detail="adsbdb lookup failed")
    return data or {}


@router.get("/enrich/photo/{hex_id}")
async def enrich_photo(hex_id: str) -> dict[str, Any]:
    data = await planespotters.lookup(hex_id)
    if data is None:
        raise HTTPException(status_code=502, detail="planespotters lookup failed")
    return data or {}


# --- FlightAware (on-demand, budget-gated) ----------------------------------


@router.get("/flightaware/budget")
async def fa_budget() -> dict[str, Any]:
    return settings_store.fa_budget_status()


@router.post("/flightaware/{callsign}")
async def fa_lookup(callsign: str, confirm_over_budget: bool = False) -> dict[str, Any]:
    budget = settings_store.fa_budget_status()
    if budget.get("over_budget") and not confirm_over_budget:
        return {
            "blocked": "over_budget",
            "budget": budget,
            "message": "Monthly budget reached; resend with confirm_over_budget=true to override.",
        }
    result = await flightaware.lookup(callsign)
    result["budget"] = settings_store.fa_budget_status()
    return result


# --- settings ----------------------------------------------------------------


@router.get("/settings")
async def get_settings() -> dict[str, Any]:
    return settings_store.get_all(redact=True)


@router.post("/settings")
async def post_settings(values: dict[str, Any] = Body(...)) -> dict[str, Any]:
    # Reject empty bodies — accidental wipes.
    if not values:
        raise HTTPException(status_code=400, detail="No settings provided")
    # If the contact URL is changing, recycle the shared httpx client so the new User-Agent
    # gets used on subsequent calls, and drop the planespotters cache so previously-failed
    # lookups (e.g. 403s under the old UA) get retried with the new one.
    contact_changing = "contact_url" in values and values.get("contact_url") != settings_store.get("contact_url")
    settings_store.set_many(values)
    if contact_changing:
        await reset_client()
        planespotters._CACHE.clear()
        hexdb._CACHE.clear()
        adsbdb._CACHE.clear()
    return settings_store.get_all(redact=True)


@router.post("/settings/fa-key")
async def post_fa_key(body: dict[str, str] = Body(...)) -> dict[str, Any]:
    key = (body.get("key") or "").strip()
    settings_store.set_one("fa_api_key", key)
    return {"ok": True, "fa_api_key_set": bool(key)}


# --- connection test --------------------------------------------------------


@router.post("/test-connection")
async def test_connection(body: dict[str, str] = Body(default={})) -> Any:
    url = (body.get("url") or settings_store.get("tar1090_base_url") or "").strip()
    if not url:
        raise HTTPException(status_code=400, detail="No tar1090 URL provided")
    try:
        result = await feed_service.test_connection(url)
    except Exception as exc:
        return JSONResponse(status_code=200, content={"ok": False, "error": str(exc)})
    return result


# --- OpenAIP aviation tile proxy --------------------------------------------
# Keeps the API key server-side. The tile cache is bounded to keep Pi memory in check —
# 1024 tiles × ~30 KB ≈ 30 MB worst case at standard zoom.

_TILE_CACHE = LRUCache(max_size=1024)
# Reject obviously bogus coords before they hit upstream.
_MAX_ZOOM = 14
_MAX_INDEX = 1 << _MAX_ZOOM


@router.get("/tiles/openaip/{z}/{x}/{y}.png")
async def openaip_tile(z: int, x: int, y: int) -> Response:
    if not (0 <= z <= _MAX_ZOOM and 0 <= x < _MAX_INDEX and 0 <= y < _MAX_INDEX):
        raise HTTPException(status_code=400, detail="Tile coordinates out of range")
    api_key = (settings_store.get("openaip_api_key") or "").strip()
    if not api_key:
        raise HTTPException(status_code=503, detail="OpenAIP key not configured")
    key = (z, x, y)
    cached = _TILE_CACHE.get(key)
    if cached is not None:
        return Response(content=cached, media_type="image/png", headers={"Cache-Control": "public, max-age=86400"})
    url = f"https://api.tiles.openaip.net/api/data/openaip/{z}/{x}/{y}.png?apiKey={api_key}"
    try:
        client = await get_client()
        r = await client.get(url, timeout=10.0)
    except httpx.HTTPError as exc:
        log.warning("OpenAIP fetch failed for %s/%s/%s: %s", z, x, y, type(exc).__name__)
        raise HTTPException(status_code=502, detail="OpenAIP request failed")
    if r.status_code == 404:
        # Tile doesn't exist (out of coverage / zoom range). Return a transparent 1×1 so Leaflet doesn't fall back to error.
        return Response(content=b"", status_code=204)
    if r.status_code != 200:
        raise HTTPException(status_code=r.status_code, detail="OpenAIP returned non-200")
    data = r.content
    _TILE_CACHE.set(key, data)
    return Response(content=data, media_type="image/png", headers={"Cache-Control": "public, max-age=86400"})


@router.post("/settings/openaip-key")
async def set_openaip_key(body: dict[str, str] = Body(...)) -> dict[str, Any]:
    key = (body.get("key") or "").strip()
    settings_store.set_one("openaip_api_key", key)
    # Clear cache so re-keying takes effect immediately for re-fetches.
    _TILE_CACHE.clear()
    return {"ok": True, "openaip_api_key_set": bool(key)}


# --- Events / stats / health / replay ---------------------------------------


@router.get("/events")
async def list_events(limit: int = 100, kind: str | None = None) -> dict[str, Any]:
    if kind not in (None, "military", "emergency", "watchlist"):
        raise HTTPException(status_code=400, detail="kind must be one of military/emergency/watchlist")
    return {"events": events_store.recent_events(limit=limit, kind=kind)}


@router.get("/stats")
async def stats(days: int = 7) -> dict[str, Any]:
    return events_store.get_stats(days=days)


@router.get("/health")
async def health() -> dict[str, Any]:
    return feed_service.health()


@router.get("/replay/timeline")
async def replay_timeline(max_points: int = 600) -> dict[str, Any]:
    pts = events_store.snapshot_timeline(max_points=max_points)
    return {"timestamps": pts, "earliest": pts[0] if pts else None, "latest": pts[-1] if pts else None}


@router.get("/replay/at")
async def replay_at(ts: float) -> dict[str, Any]:
    snap = events_store.snapshot_nearest(ts)
    if snap is None:
        raise HTTPException(status_code=404, detail="No snapshot found")
    return snap


# --- Insights: coverage / heatmap / leaderboard / notes ---------------------


_HEX_RE = re.compile(r"^[0-9a-f]{6}$")


@router.get("/coverage")
async def coverage() -> dict[str, Any]:
    return {"bins": insights_store.polar_coverage()}


@router.get("/heatmap")
async def heatmap(top: int = 5000) -> dict[str, Any]:
    pts = insights_store.heatmap_points(top_n=top)
    return {"points": [{"lat": p[0], "lon": p[1], "hits": p[2]} for p in pts]}


@router.get("/leaderboard")
async def leaderboard(limit: int = 20) -> dict[str, Any]:
    return {"types": insights_store.leaderboard(limit=limit)}


@router.get("/notes")
async def list_notes() -> dict[str, Any]:
    return {"notes": insights_store.all_notes()}


@router.get("/notes/{hex_id}")
async def get_note(hex_id: str) -> dict[str, Any]:
    if not _HEX_RE.match(hex_id.lower()):
        raise HTTPException(status_code=400, detail="Invalid hex")
    return {"hex": hex_id.lower(), "note": insights_store.get_note(hex_id) or ""}


@router.put("/notes/{hex_id}")
async def put_note(hex_id: str, body: dict[str, str] = Body(...)) -> dict[str, Any]:
    if not _HEX_RE.match(hex_id.lower()):
        raise HTTPException(status_code=400, detail="Invalid hex")
    insights_store.set_note(hex_id, body.get("note") or "")
    return {"ok": True, "hex": hex_id.lower(), "note": insights_store.get_note(hex_id) or ""}


# --- Webhooks management ----------------------------------------------------


@router.get("/webhooks")
async def list_webhooks() -> dict[str, Any]:
    try:
        endpoints = json.loads(settings_store.get("webhooks_json") or "[]")
    except Exception:
        endpoints = []
    return {"webhooks": endpoints if isinstance(endpoints, list) else []}


@router.post("/webhooks")
async def save_webhooks(body: dict[str, Any] = Body(...)) -> dict[str, Any]:
    endpoints = body.get("webhooks") or []
    if not isinstance(endpoints, list):
        raise HTTPException(status_code=400, detail="webhooks must be a list")
    clean: list[dict[str, Any]] = []
    for ep in endpoints:
        if not isinstance(ep, dict):
            continue
        url = (ep.get("url") or "").strip()
        kind_ep = (ep.get("kind") or "generic").lower()
        types = ep.get("types") or []
        if not url.startswith(("http://", "https://")):
            continue
        if kind_ep not in {"discord", "slack", "ntfy", "generic"}:
            kind_ep = "generic"
        if not isinstance(types, list):
            types = []
        types = [t for t in types if t in {"emergency", "military", "watchlist", "rare"}]
        clean.append({"kind": kind_ep, "url": url, "types": types, "label": (ep.get("label") or "")[:60]})
    settings_store.set_one("webhooks_json", json.dumps(clean))
    return {"webhooks": clean}


@router.post("/webhooks/test")
async def test_webhook(body: dict[str, Any] = Body(...)) -> dict[str, Any]:
    """Send a sample notification to a single webhook so the user can verify their setup."""
    from ..services import webhooks as webhooks_service
    ep = {
        "kind": (body.get("kind") or "generic").lower(),
        "url": body.get("url") or "",
        "types": ["emergency", "military", "watchlist", "rare"],
    }
    if not ep["url"].startswith(("http://", "https://")):
        raise HTTPException(status_code=400, detail="Invalid URL")
    sample_ac = {"display_name": "SAMPLE", "hex": "abcdef", "type_code": "A320",
                 "distance_nm": 12.3, "squawk": "1234", "altitude_baro": 35000}
    await webhooks_service._post_one(ep, "Test alert from PiScope Radar — webhook is working.", sample_ac)
    return {"ok": True}


# --- Saved views ------------------------------------------------------------


@router.get("/views")
async def list_views() -> dict[str, Any]:
    try:
        views = json.loads(settings_store.get("saved_views_json") or "[]")
    except Exception:
        views = []
    return {"views": views if isinstance(views, list) else []}


@router.post("/views")
async def save_views(body: dict[str, Any] = Body(...)) -> dict[str, Any]:
    views = body.get("views") or []
    if not isinstance(views, list):
        raise HTTPException(status_code=400, detail="views must be a list")
    clean = []
    for v in views[:50]:
        if not isinstance(v, dict):
            continue
        name = (v.get("name") or "").strip()[:60]
        try:
            lat = float(v.get("lat"))
            lon = float(v.get("lon"))
            zoom = max(1, min(int(v.get("zoom") or 6), 18))
        except (TypeError, ValueError):
            continue
        if not name or not (-90 <= lat <= 90) or not (-180 <= lon <= 180):
            continue
        clean.append({"name": name, "lat": lat, "lon": lon, "zoom": zoom})
    settings_store.set_one("saved_views_json", json.dumps(clean))
    return {"views": clean}


# --- Prometheus-style metrics + DB export/import ---------------------------


@router.get("/metrics", include_in_schema=False)
async def prometheus_metrics() -> Response:
    h = feed_service.health()
    lines = [
        "# HELP piscope_uptime_seconds Process uptime in seconds.",
        "# TYPE piscope_uptime_seconds gauge",
        f"piscope_uptime_seconds {h['uptime_seconds']}",
        "# HELP piscope_polls_total Total poll cycles since start.",
        "# TYPE piscope_polls_total counter",
        f"piscope_polls_total {h['polls']}",
        "# HELP piscope_errors_total Poll cycles that ended in an error.",
        "# TYPE piscope_errors_total counter",
        f"piscope_errors_total {h['errors']}",
        "# HELP piscope_last_poll_duration_ms Duration of the most recent poll cycle.",
        "# TYPE piscope_last_poll_duration_ms gauge",
        f"piscope_last_poll_duration_ms {h['last_poll_duration_ms']}",
        "# HELP piscope_aircraft Current aircraft in coverage.",
        "# TYPE piscope_aircraft gauge",
        f"piscope_aircraft {h['aircraft_count']}",
        "# HELP piscope_subscribers Connected WebSocket clients.",
        "# TYPE piscope_subscribers gauge",
        f"piscope_subscribers {h['subscriber_count']}",
        "# HELP piscope_unique_today Unique aircraft seen today.",
        "# TYPE piscope_unique_today gauge",
        f"piscope_unique_today {h['daily']['unique_aircraft']}",
        "# HELP piscope_max_range_nm_today Maximum range observed today (nm).",
        "# TYPE piscope_max_range_nm_today gauge",
        f"piscope_max_range_nm_today {h['daily']['max_range_nm']}",
    ]
    for name, feed in (h.get("feeds") or {}).items():
        safe = re.sub(r"[^a-z0-9_]", "_", name.lower()) or "feed"
        ok = 1 if feed.get("ok") else 0
        lines.append(f'piscope_feed_ok{{feed="{safe}"}} {ok}')
        if feed.get("duration_ms") is not None:
            lines.append(f'piscope_feed_duration_ms{{feed="{safe}"}} {feed["duration_ms"]}')
        if feed.get("rows") is not None:
            lines.append(f'piscope_feed_rows{{feed="{safe}"}} {feed["rows"]}')
    body = "\n".join(lines) + "\n"
    return Response(content=body, media_type="text/plain; version=0.0.4; charset=utf-8")


# --- Records + bookmarks ---------------------------------------------------


@router.get("/records")
async def list_records() -> dict[str, Any]:
    return {"records": records_store.all_records()}


@router.get("/bookmarks")
async def list_bookmarks() -> dict[str, Any]:
    return {"bookmarks": records_store.list_bookmarks()}


@router.post("/bookmarks/{hex_id}")
async def add_bookmark(hex_id: str, body: dict[str, Any] = Body(default={})) -> dict[str, Any]:
    if not _HEX_RE.match(hex_id.lower()):
        raise HTTPException(status_code=400, detail="Invalid hex")
    try:
        bm = records_store.add_bookmark(
            hex_id,
            label=body.get("label") or "",
            callsign=body.get("callsign"),
            registration=body.get("registration"),
            type_code=body.get("type_code"),
        )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    return bm


@router.delete("/bookmarks/{hex_id}")
async def delete_bookmark(hex_id: str) -> dict[str, Any]:
    if not _HEX_RE.match(hex_id.lower()):
        raise HTTPException(status_code=400, detail="Invalid hex")
    records_store.remove_bookmark(hex_id)
    return {"ok": True}


@router.get("/export")
async def export_db() -> Response:
    """Return the entire database as a downloadable ZIP. Settings + events + history."""
    db_path = Path(settings_store.DB_PATH)
    if not db_path.exists():
        raise HTTPException(status_code=404, detail="No database file yet")
    # SQLite's online backup API gives us a consistent file even with the feed loop writing.
    bio = io.BytesIO()
    with sqlite3.connect(db_path) as src, sqlite3.connect(":memory:") as mem:
        src.backup(mem)
        rows = list(mem.iterdump())
    sql_dump = "\n".join(rows)
    with zipfile.ZipFile(bio, "w", zipfile.ZIP_DEFLATED) as zf:
        zf.writestr("piscope.sql", sql_dump)
        zf.writestr("exported_at.txt", time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()) + "\n")
    bio.seek(0)
    fname = f"piscope-backup-{time.strftime('%Y%m%d-%H%M%S')}.zip"
    return Response(
        content=bio.getvalue(),
        media_type="application/zip",
        headers={"Content-Disposition": f'attachment; filename="{fname}"'},
    )


@router.post("/import")
async def import_db(file: UploadFile = File(...)) -> dict[str, Any]:
    """Restore from a PiScope Radar backup .zip (the file produced by /api/export).

    Strategy: load the SQL dump into a brand-new SQLite file in a temp location, atomically
    move it over the live DB, and ask the feed service to re-init. Caller should reload the UI.
    """
    payload = await file.read(20 * 1024 * 1024)   # 20 MB cap; way more than we need
    if not payload:
        raise HTTPException(status_code=400, detail="Empty file")
    try:
        with zipfile.ZipFile(io.BytesIO(payload)) as zf:
            # Accept either the new piscope.sql or the legacy skywatch.sql layout so
            # users can restore backups taken before the rename.
            sql_name = "piscope.sql" if "piscope.sql" in zf.namelist() else ("skywatch.sql" if "skywatch.sql" in zf.namelist() else None)
            if sql_name is None:
                raise HTTPException(status_code=400, detail="Zip is missing piscope.sql / skywatch.sql")
            sql = zf.read(sql_name).decode("utf-8", errors="replace")
    except zipfile.BadZipFile:
        raise HTTPException(status_code=400, detail="Not a valid zip file")
    # Build a fresh DB at a sibling path, then atomically swap.
    db_path = Path(settings_store.DB_PATH)
    tmp_path = db_path.with_suffix(".import.tmp")
    if tmp_path.exists():
        tmp_path.unlink()
    try:
        with sqlite3.connect(tmp_path) as fresh:
            fresh.executescript(sql)
            fresh.commit()
    except sqlite3.Error as exc:
        tmp_path.unlink(missing_ok=True)
        raise HTTPException(status_code=400, detail=f"Could not restore: {exc}")
    # Pause feed writes for a moment by stopping it, then swap files, then restart.
    await feed_service.stop()
    try:
        db_path.unlink(missing_ok=True)
        tmp_path.rename(db_path)
        # Make sure the schema is at the version we expect (older backups may need migration).
        settings_store.init_db()
    finally:
        await feed_service.start()
    return {"ok": True, "message": "Database restored; please refresh the page."}


# --- AI explain (iteration 5) -----------------------------------------------

# Strict allow-list of payload fields. Anything else in the body is dropped before the
# call into ollama_svc.explain — we never let raw user keys flow into the prompt builder.
_EXPLAIN_ALLOWED_FIELDS = {
    # Identifiers / enrichment
    "hex", "callsign", "type_code", "registration", "airline_name", "operator",
    "origin_name", "origin_municipality", "origin_iata", "origin_icao", "origin_country_iso",
    "destination_name", "destination_municipality", "destination_iata", "destination_icao", "destination_country_iso",
    # Live state — added in iteration 6 so the brief can reference current flight phase
    "altitude_baro", "ground_speed", "heading", "vertical_rate", "distance_nm",
    "on_ground", "squawk", "is_emergency_squawk", "military", "watchlist_match",
}


@router.get("/explain/status")
async def explain_status() -> dict[str, Any]:
    """Cheap predicate the frontend uses to decide whether to show the "Explain" button.
    Doesn't actually call the provider — only reports the configured-ness of the feature.
    Legacy `model`/`url_set` fields kept for back-compat; new code reads `provider`."""
    return {
        "configured": ai_svc.is_configured(),
        "provider": ai_svc.active_provider_name(),
        "model": settings_store.get("ollama_model") or "",
        "url_set": bool((settings_store.get("ollama_url") or "").strip()),
    }


@router.post("/explain")
async def explain(body: dict[str, Any] = Body(...)) -> dict[str, Any]:
    """Generate a short natural-language brief for one aircraft."""
    if not ai_svc.is_configured():
        raise HTTPException(status_code=503, detail="AI explanations not configured")
    payload = {k: body[k] for k in body.keys() if k in _EXPLAIN_ALLOWED_FIELDS}
    if not payload.get("hex"):
        raise HTTPException(status_code=400, detail="hex required")
    result = await ai_svc.explain(payload)
    # 200 with an envelope (including "unavailable") rather than 5xx — the frontend renders
    # an inline error instead of a console-spew red 500.
    return result


@router.post("/ollama/test")
async def ollama_test() -> dict[str, Any]:
    """Settings → AI test-connection button. Returns reachability + a list of models so
    the user can confirm their configured model exists. Explicitly pings the Ollama
    provider regardless of the active provider — this is the per-provider Ollama test."""
    return await _ollama_provider.ping()


@router.post("/cloud-api/test")
async def cloud_api_test() -> dict[str, Any]:
    """Settings → Cloud API test-connection button. Pings the cloud_api provider with
    the currently-configured vendor + key, returns the vendor's models list."""
    return await _cloud_api_provider.ping()


# --- Daily digest (iteration 5) ---------------------------------------------


@router.get("/digest")
async def digest_latest() -> dict[str, Any]:
    """Return the most-recently-persisted digest. The frontend uses this to render the
    Stats → Today card. If nothing has been generated yet, returns `null`."""
    latest = digest_svc.get_latest()
    return {"digest": latest}


@router.post("/digest/run")
async def digest_run() -> dict[str, Any]:
    """Build and deliver a digest right now — used by the "Send test digest" button."""
    try:
        d = await digest_svc.run_digest(with_ai=True)
        return {"ok": True, "digest": d}
    except Exception as exc:
        log.exception("digest run failed")
        raise HTTPException(status_code=500, detail=f"{type(exc).__name__}: {exc}")
