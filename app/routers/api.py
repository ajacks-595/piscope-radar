from __future__ import annotations

import asyncio
import logging
from typing import Any, Literal, Optional

import httpx
from fastapi import APIRouter, Body, HTTPException, Request, Response
from fastapi.concurrency import run_in_threadpool
from fastapi.responses import JSONResponse, StreamingResponse
from pydantic import BaseModel, ConfigDict, Field

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
from ..services.ai import claude_cli as _claude_cli_provider
from ..services import digest as digest_svc
from ..services import dashboard as dashboard_svc
from ..services._http import LRUCache, get_client, reset_client
from ..services.feed import feed_service


log = logging.getLogger("piscope.api")

router = APIRouter(prefix="/api")


# --- Request models (iter 11) -----------------------------------------------
# Pydantic models for the endpoints where strict typing is a clean win: automatic
# 422 on malformed input + OpenAPI/Swagger docs. Deliberately NOT applied to
# /api/settings (the DEFAULTS whitelist in settings.py is the right validation
# there — a model duplicating ~50 keys would be brittle) nor to the webhook-save
# list (its lenient coerce-and-clean loop intentionally doesn't 422 a whole list
# over one bad entry).


class WebhookTestBody(BaseModel):
    model_config = ConfigDict(extra="ignore")
    kind: str = "generic"
    url: str


class ChatTurn(BaseModel):
    model_config = ConfigDict(extra="ignore")
    role: Literal["user", "assistant"]
    content: str = Field(..., min_length=1, max_length=2000)


class AircraftBrief(BaseModel):
    """Aircraft fields accepted by /api/explain (+ the followup's `aircraft`). Mirrors the
    old _EXPLAIN_ALLOWED_FIELDS whitelist; `extra='ignore'` drops anything else. Every value
    is independently re-sanitised in ai/_common (regex / range-clamp / printable-strip), so
    this is shape + docs validation, not the security boundary. Keep in sync with the field
    list there if you add inputs."""
    model_config = ConfigDict(extra="ignore")
    hex: str = Field(..., min_length=1)
    callsign: Optional[str] = None
    type_code: Optional[str] = None
    registration: Optional[str] = None
    airline_name: Optional[str] = None
    operator: Optional[str] = None
    origin_name: Optional[str] = None
    origin_municipality: Optional[str] = None
    origin_iata: Optional[str] = None
    origin_icao: Optional[str] = None
    origin_country_iso: Optional[str] = None
    destination_name: Optional[str] = None
    destination_municipality: Optional[str] = None
    destination_iata: Optional[str] = None
    destination_icao: Optional[str] = None
    destination_country_iso: Optional[str] = None
    altitude_baro: Optional[float] = None
    ground_speed: Optional[float] = None
    heading: Optional[float] = None
    vertical_rate: Optional[float] = None
    distance_nm: Optional[float] = None
    on_ground: Optional[bool] = None
    squawk: Optional[str] = None
    is_emergency_squawk: Optional[bool] = None
    military: Optional[bool] = None
    watchlist_match: Optional[bool] = None


class FollowupBody(BaseModel):
    model_config = ConfigDict(extra="ignore")
    aircraft: AircraftBrief
    history: list[ChatTurn] = Field(default_factory=list)
    question: str = Field(..., min_length=1, max_length=500)


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
async def test_webhook(body: WebhookTestBody) -> dict[str, Any]:
    """Send a sample notification to a single webhook so the user can verify their setup."""
    from ..services import webhooks as webhooks_service
    ep = {
        "kind": (body.kind or "generic").lower(),
        "url": body.url or "",
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


def _build_export_zip(db_path: Path) -> bytes:
    """Online-backup the DB to memory, dump SQL, and zip it. Synchronous + CPU/IO-bound
    (whole-DB copy + zlib compression) — run via run_in_threadpool so it never blocks the
    event loop, which would otherwise stall WS heartbeats during a large export (iter 11).

    Secrets (API keys, SMTP password, provider tokens) are stripped from the dump (iter 11):
    backup zips get emailed and cloud-stored, and we don't want plaintext credentials riding
    along. The strip runs on the throwaway in-memory copy, so the live DB is untouched. On
    restore those keys come back blank and must be re-entered in Settings.
    """
    bio = io.BytesIO()
    # SQLite's online backup API gives us a consistent file even with the feed loop writing.
    with sqlite3.connect(db_path) as src, sqlite3.connect(":memory:") as mem:
        src.backup(mem)
        secret_keys = sorted(settings_store.SECRET_KEYS)
        if secret_keys:
            # Values are JSON-encoded in the settings table; '""' is an empty string.
            placeholders = ",".join("?" * len(secret_keys))
            mem.execute(
                f"UPDATE settings SET value = '\"\"' WHERE key IN ({placeholders})",
                tuple(secret_keys),
            )
            mem.commit()
        rows = list(mem.iterdump())
    sql_dump = "\n".join(rows)
    with zipfile.ZipFile(bio, "w", zipfile.ZIP_DEFLATED) as zf:
        zf.writestr("piscope.sql", sql_dump)
        zf.writestr("exported_at.txt", time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()) + "\n")
        zf.writestr(
            "README-RESTORE.txt",
            "PiScope Radar backup.\n\n"
            "For your security, secret values (API keys, SMTP password, AI provider tokens) "
            "have been STRIPPED from this backup. After restoring, re-enter them under "
            "Settings. Everything else (events, records, coverage, settings) restores as-is.\n",
        )
    return bio.getvalue()


@router.get("/export")
async def export_db() -> Response:
    """Return the entire database as a downloadable ZIP. Settings + events + history."""
    db_path = Path(settings_store.DB_PATH)
    if not db_path.exists():
        raise HTTPException(status_code=404, detail="No database file yet")
    content = await run_in_threadpool(_build_export_zip, db_path)
    fname = f"piscope-backup-{time.strftime('%Y%m%d-%H%M%S')}.zip"
    return Response(
        content=content,
        media_type="application/zip",
        headers={"Content-Disposition": f'attachment; filename="{fname}"'},
    )


def _build_restore_db(sql: str, tmp_path: Path) -> None:
    """Execute the backup's SQL dump into a fresh temp DB. Synchronous; threadpool'd."""
    if tmp_path.exists():
        tmp_path.unlink()
    with sqlite3.connect(tmp_path) as fresh:
        fresh.executescript(sql)
        fresh.commit()


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
    # Build a fresh DB at a sibling path, then atomically swap. executescript on a
    # restore can be heavy (whole-DB rebuild) so it runs in a threadpool (iter 11).
    db_path = Path(settings_store.DB_PATH)
    tmp_path = db_path.with_suffix(".import.tmp")
    try:
        await run_in_threadpool(_build_restore_db, sql, tmp_path)
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
# The payload allow-list that used to live here is now the AircraftBrief Pydantic
# model near the top of this file (iter 11) — same fields, with extra='ignore'.


def _provider_model_label() -> str:
    """Best-effort 'what model is this going to call?' string for the UI.
    Returns a vendor-specific default when the user hasn't set one explicitly,
    so the status pill always has something to show."""
    name = ai_svc.active_provider_name()
    if name == "ollama":
        return (settings_store.get("ollama_model") or "").strip()
    if name == "cloud_api":
        from ..services.ai.cloud_api import DEFAULT_MODELS, _vendor
        configured = (settings_store.get("cloud_api_model") or "").strip()
        return configured or DEFAULT_MODELS.get(_vendor(), "")
    if name == "claude_cli":
        # The shim picks the model; we have no model name here.
        return "claude (via shim)"
    return ""


@router.get("/explain/status")
async def explain_status() -> dict[str, Any]:
    """Cheap predicate the frontend uses to decide whether to show the "Explain" button.
    Doesn't actually call the provider — only reports the configured-ness of the feature.

    Response shape:
        {
          "configured": bool,                      # any provider ready?
          "provider": {"name", "configured", "model"},
          "model": "...", "url_set": bool          # legacy Ollama fields, kept for back-compat
        }
    """
    name = ai_svc.active_provider_name()
    return {
        "configured": ai_svc.is_configured(),
        "provider": {
            "name": name,
            "configured": ai_svc.is_configured(),
            "model": _provider_model_label(),
        },
        # Legacy fields — pre-iteration-7 clients (cached HTML on a stale browser) read these.
        # They will be removed once we're confident no one's still on the old shell.
        "model": settings_store.get("ollama_model") or "",
        "url_set": bool((settings_store.get("ollama_url") or "").strip()),
    }


@router.post("/explain")
async def explain(body: AircraftBrief) -> dict[str, Any]:
    """Generate a short natural-language brief for one aircraft."""
    if not ai_svc.is_configured():
        raise HTTPException(status_code=503, detail="AI explanations not configured")
    # Model already validated shape + required hex; extra keys dropped. Nones trimmed so
    # the prompt builder's `.get()` checks behave as before.
    payload = body.model_dump(exclude_none=True)
    result = await ai_svc.explain(payload)
    # 200 with an envelope (including "unavailable") rather than 5xx — the frontend renders
    # an inline error instead of a console-spew red 500.
    return result


@router.post("/explain/followup")
async def explain_followup(body: FollowupBody) -> dict[str, Any]:
    """Follow-up question about an aircraft the user has already seen the brief for.

    Body shape (validated by FollowupBody):
      {
        "aircraft": {hex, callsign, ...},          # same fields as /api/explain
        "history":  [{"role": "user"|"assistant", "content": "..."}, ...],
        "question": "..."                          # 1–500 chars
      }

    Caching deliberately skipped — each conversation is unique and a stale
    answer to a follow-up would be more confusing than helpful. Token cost is
    bounded by the `ai_chat_max_turns` setting (default 5 exchanges).
    """
    if not ai_svc.is_configured():
        raise HTTPException(status_code=503, detail="AI explanations not configured")

    payload = body.aircraft.model_dump(exclude_none=True)
    history = [{"role": t.role, "content": t.content} for t in body.history]

    max_turns = settings_store.get("ai_chat_max_turns") or 5
    try:
        max_turns = int(max_turns)
    except (TypeError, ValueError):
        max_turns = 5
    max_turns = max(1, min(max_turns, 20))

    from ..services.ai._common import build_followup_prompt, cap_response
    prompt = build_followup_prompt(payload, history, body.question, max_turns=max_turns)
    text = await ai_svc.generate(prompt, num_predict=240, temperature=0.5)
    provider = ai_svc.active_provider_name()
    if not text:
        return {"source": "unavailable", "error": f"no response from {provider}", "provider": provider}
    return {"text": cap_response(text), "source": "ai", "provider": provider}


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


@router.post("/claude-cli/test")
async def claude_cli_test() -> dict[str, Any]:
    """Settings → Claude CLI test-connection button. Hits the shim's /health endpoint
    so the user can confirm the shim is reachable and the bearer token (if any) matches."""
    return await _claude_cli_provider.ping()


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


# --- Dashboard integration (iter 9.2) ---------------------------------------
#
# `/api/dashboard/*` is the public-facing surface for external dashboards
# (jacknet-home, anything anyone wants to build) to consume PiScope data
# without iframing the full map. Endpoints under this prefix follow the
# `{success, data, error}` envelope convention shared with similar projects.

@router.get("/dashboard/summary")
async def dashboard_summary(
    lat: float,
    lon: float,
    radius_km: float = 50.0,
    filters: Optional[str] = None,
    top: int = 3,
    min_alt: Optional[float] = None,
    max_alt: Optional[float] = None,
    min_speed: Optional[float] = None,
    overhead_threshold_s: float = 60.0,
    overhead_radius_km: float = 2.0,
) -> Response:
    """Read-only summary for dashboard widgets: counts by category, nearest
    contact, overhead-imminent list, top-N highlights. 5 s response cache."""
    # Sanity-bound inputs to prevent abusive computations / coordinate typos.
    if not (-90 <= lat <= 90) or not (-180 <= lon <= 180):
        return JSONResponse(status_code=400, content={"success": False, "data": None,
                                                       "error": "lat/lon out of range"})
    radius_km = max(0.1, min(float(radius_km), 500.0))
    top = max(1, min(int(top), 10))
    overhead_threshold_s = max(5.0, min(float(overhead_threshold_s), 300.0))
    overhead_radius_km = max(0.1, min(float(overhead_radius_km), 50.0))

    cache_key = (round(lat, 4), round(lon, 4), round(radius_km, 1),
                 filters or "", top,
                 None if min_alt is None else int(min_alt),
                 None if max_alt is None else int(max_alt),
                 None if min_speed is None else int(min_speed),
                 round(overhead_threshold_s, 1), round(overhead_radius_km, 2))
    cached = dashboard_svc.cache_get(cache_key)
    if cached is not None:
        return JSONResponse(content={"success": True, "data": cached, "error": None},
                            headers={"Cache-Control": "public, max-age=5"})

    # Pull live state from feed_service. snapshot() builds full aircraft dicts;
    # we iterate them once to build the summary.
    snap = feed_service.snapshot()
    health = feed_service.health()
    data = dashboard_svc.build_summary(
        snap.get("aircraft") or [],
        observer_lat=lat,
        observer_lon=lon,
        radius_km=radius_km,
        filters=filters,
        top=top,
        min_alt=min_alt,
        max_alt=max_alt,
        min_speed=min_speed,
        overhead_threshold_s=overhead_threshold_s,
        overhead_radius_km=overhead_radius_km,
        feed_total_messages=int(snap.get("total_messages") or 0),
        feed_uptime_seconds=float(health.get("uptime_seconds") or 0.0),
        feed_last_poll_at=snap.get("polled_at"),
        feed_connection_state=str(snap.get("connection_state") or "unknown"),
        daily_unique_count=int((health.get("daily") or {}).get("unique_aircraft") or 0),
        watchlist=dashboard_svc.parse_watchlist(),
    )
    dashboard_svc.cache_set(cache_key, data)
    return JSONResponse(content={"success": True, "data": data, "error": None},
                        headers={"Cache-Control": "public, max-age=5"})


@router.get("/dashboard/categorize")
async def dashboard_categorize_stats() -> dict[str, Any]:
    """Diagnostic for the categorization table. Returns size + currently-loaded
    categories. Useful when extending app/data/type_categories.json."""
    from ..services import categorize
    return {
        "table_size": categorize.table_size(),
        "categories": list(categorize.CATEGORIES),
    }


@router.get("/dashboard/events")
async def dashboard_events(
    request: Request,
    lat: Optional[float] = None,
    lon: Optional[float] = None,
    radius_km: float = 50.0,
) -> StreamingResponse:
    """Server-Sent Events stream of significant aircraft events (iter 9.3).

    Emits: `emergency`, `emergency_resolved`, `military`, `watchlist`, `rare`,
    and periodic `heartbeat`. If `lat`/`lon` are supplied, events with known
    coordinates outside `radius_km` are filtered out — events without coords
    (e.g. emergency_resolved due to coverage loss) always pass through.

    Reconnects via the standard SSE `Last-Event-ID` header replay anything
    that's still in the ring buffer (~500 most recent events).
    """
    from ..services import events_bus
    from ..services.dashboard import haversine_km

    if lat is not None and not (-90 <= lat <= 90):
        raise HTTPException(status_code=400, detail="lat out of range")
    if lon is not None and not (-90 <= lon <= 180):
        raise HTTPException(status_code=400, detail="lon out of range")
    radius_km = max(0.1, min(float(radius_km), 1000.0))

    # Last-Event-ID for replay-after-reconnect. Header is preferred; query
    # `since` is an explicit fallback for clients that can't set it.
    last_id_raw = request.headers.get("Last-Event-ID") or request.query_params.get("since") or "0"
    try:
        last_id = int(last_id_raw)
    except (TypeError, ValueError):
        last_id = 0

    async def stream():
        # Initial server-comment line nudges proxies (and tells the browser
        # the stream is alive immediately).
        yield ":piscope dashboard events stream\n\n"
        # Recommended client retry on disconnect (5 s).
        yield "retry: 5000\n\n"

        sub = events_bus.subscribe(start_after_id=last_id)
        loop_done = False
        while not loop_done:
            try:
                # Race the next bus event against a 25 s heartbeat. asyncio.wait
                # would also work but anext + wait_for is the simpler shape here.
                next_task = asyncio.ensure_future(sub.__anext__())
                try:
                    ev = await asyncio.wait_for(next_task, timeout=25.0)
                except asyncio.TimeoutError:
                    next_task.cancel()
                    yield f"event: heartbeat\ndata: {json.dumps({'as_of': time.time()})}\n\n"
                    continue
                except StopAsyncIteration:
                    loop_done = True
                    break

                # Location filter — events without coords always pass.
                if (lat is not None and lon is not None
                        and ev.lat is not None and ev.lon is not None):
                    d = haversine_km(lat, lon, ev.lat, ev.lon)
                    if d > radius_km:
                        continue

                payload = {
                    "hex": ev.hex,
                    "ts": ev.ts,
                    "lat": ev.lat,
                    "lon": ev.lon,
                    **(ev.data or {}),
                }
                yield f"id: {ev.id}\nevent: {ev.kind}\ndata: {json.dumps(payload)}\n\n"
            except asyncio.CancelledError:
                loop_done = True
            except Exception as exc:
                # Don't kill the stream over one bad event — log + send a comment
                # so the client knows something happened but isn't disconnected.
                log.warning("dashboard SSE stream error: %s", exc)
                yield f": error: {type(exc).__name__}\n\n"

    return StreamingResponse(
        stream(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache, no-transform",
            "X-Accel-Buffering": "no",   # nginx / lighttpd: don't buffer
            "Connection": "keep-alive",
        },
    )


@router.get("/dashboard/events/stats")
async def dashboard_events_stats() -> dict[str, Any]:
    """Bus diagnostics — current subscriber count + ring size + latest event id.
    Cheap, intended for the dashboard agent's debug overlays."""
    from ..services import events_bus
    return {
        "subscribers": events_bus.subscriber_count(),
        "ring_size": events_bus.ring_size(),
        "latest_event_id": events_bus.latest_event_id(),
    }
