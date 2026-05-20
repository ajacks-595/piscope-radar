"""Daily digest — a once-per-day rollup of the prior 24 hours of activity.

Drives three delivery channels:
  1. In-app card under Stats → Today (pulled by the frontend from /piscope/api/digest).
  2. Webhook fan-out, reusing webhooks.py with a new kind "digest".
  3. SMTP email (plain text), if configured.

Optionally adds an AI-written commentary section if ollama is reachable. Crucially the
templated digest still works without Ollama, so the feature is useful out of the box.

Schedulers
----------
Runs as a single asyncio background task started from the FastAPI lifespan. We pick
the soonest "digest_local_time" (HH:MM in the host's local timezone) and sleep until then.
No APScheduler dep — one fewer thing to install on the Pi.
"""
from __future__ import annotations

import asyncio
import json
import logging
import smtplib
import ssl
import time
import zlib
from collections import Counter
from datetime import datetime, time as dtime, timedelta
from email.message import EmailMessage
from email.utils import formatdate
from typing import Any, Optional

from . import settings as settings_store
from . import webhooks
from .settings import _connect  # type: ignore[attr-defined]


log = logging.getLogger("piscope.digest")

# Single canonical key for the in-app cache row.
_LATEST_KEY = "digest_latest_json"


# ---------- Aggregation ----------


def _decode_snapshot(payload: str) -> Optional[dict[str, Any]]:
    """feed_snapshots.payload is stored either as plain JSON OR as `Z1` + zlib(JSON).
    Decoders for both layouts so historical rows still parse."""
    if not payload:
        return None
    try:
        # Older rows: plain JSON
        if payload.startswith("{"):
            return json.loads(payload)
        # Newer rows: stored as bytes(zlib...) but written to TEXT column → mojibake.
        # In practice the events module writes the magic prefix as text; treat as bytes.
        raw = payload.encode("latin-1", errors="ignore")
        if raw.startswith(b"Z1"):
            return json.loads(zlib.decompress(raw[2:]).decode("utf-8"))
    except Exception:
        return None
    return None


def build_digest(now_ts: Optional[float] = None, window_hours: int = 24) -> dict[str, Any]:
    """Roll up the last `window_hours` of activity into a JSON-serialisable dict.

    Pure aggregation — does not deliver. Cheap enough to run multiple times per day
    (used by both the scheduler at the configured local time AND by the "send test
    digest now" admin button)."""
    now = float(now_ts or time.time())
    since = now - (window_hours * 3600)

    with _connect() as conn:
        # Event counts by kind in the window.
        event_rows = conn.execute(
            "SELECT kind, COUNT(*) AS n FROM events WHERE ts >= ? GROUP BY kind",
            (since,),
        ).fetchall()
        event_counts: dict[str, int] = {r["kind"]: int(r["n"]) for r in event_rows}

        # Top aircraft callouts in the window (military, emergency, watchlist, rare).
        callout_rows = conn.execute(
            "SELECT ts, kind, hex, callsign, registration, distance_nm, payload "
            "FROM events WHERE ts >= ? AND kind IN ('military','emergency','watchlist','rare') "
            "ORDER BY ts DESC LIMIT 25",
            (since,),
        ).fetchall()
        callouts: list[dict[str, Any]] = []
        for r in callout_rows:
            item = {
                "ts": float(r["ts"]),
                "kind": r["kind"],
                "hex": r["hex"],
                "callsign": r["callsign"],
                "registration": r["registration"],
                "distance_nm": r["distance_nm"],
            }
            if r["payload"]:
                try:
                    p = json.loads(r["payload"])
                    item["type_code"] = p.get("type_code")
                    item["display_name"] = p.get("display_name")
                    if p.get("squawk"):
                        item["squawk"] = p["squawk"]
                except Exception:
                    pass
            callouts.append(item)

        # Type ledger snapshot for the leaderboard. We pull the top N by sightings (lifetime),
        # but separately mark types whose `first_seen` is within the window as "new".
        type_rows = conn.execute(
            "SELECT type_code, first_seen, last_seen, sightings FROM seen_types "
            "WHERE last_seen >= ? ORDER BY sightings DESC LIMIT 10",
            (since,),
        ).fetchall()
        top_types: list[dict[str, Any]] = []
        new_types: list[str] = []
        for r in type_rows:
            tc = r["type_code"]
            top_types.append({
                "type_code": tc,
                "sightings": int(r["sightings"] or 0),
            })
            if r["first_seen"] and float(r["first_seen"]) >= since:
                new_types.append(tc)

        # daily_stats row for today gives unique aircraft count + max range without us
        # having to rescan every snapshot — the feed loop maintains it incrementally.
        today = datetime.now(tz=None).strftime("%Y-%m-%d")
        ds = conn.execute(
            "SELECT total_polls, unique_aircraft, max_range_nm, emergencies, military_seen "
            "FROM daily_stats WHERE date = ?", (today,)
        ).fetchone()

        # Peak msg rate — approximate from snapshot deltas over the window. Cheap if we
        # cap rows; we sample every Nth snapshot.
        snap_rows = conn.execute(
            "SELECT ts, payload FROM feed_snapshots WHERE ts >= ? ORDER BY ts ASC",
            (since,),
        ).fetchmany(2880)  # 24h at 30s granularity ceiling; LIMIT-by-fetchmany is fine for ring buffer
        peak_aircraft = 0
        for r in snap_rows[::6]:  # every ~6th sample to bound CPU
            snap = _decode_snapshot(r["payload"])
            if snap and isinstance(snap.get("aircraft"), list):
                peak_aircraft = max(peak_aircraft, len(snap["aircraft"]))

    return {
        "generated_at": now,
        "window_hours": window_hours,
        "totals": {
            "events": sum(event_counts.values()),
            "by_kind": event_counts,
            "unique_aircraft_today": int(ds["unique_aircraft"] or 0) if ds else 0,
            "max_range_nm_today": float(ds["max_range_nm"] or 0.0) if ds else 0.0,
            "peak_concurrent_aircraft": peak_aircraft,
        },
        "top_types": top_types,
        "new_types_in_window": new_types,
        "callouts": callouts,
    }


# ---------- Rendering ----------


def render_text(digest: dict[str, Any]) -> str:
    """Plain-text rendering — used for SMTP and as the body of webhook messages."""
    when = datetime.fromtimestamp(digest["generated_at"]).strftime("%Y-%m-%d %H:%M")
    t = digest["totals"]
    lines = [
        f"PiScope Radar — Daily Digest ({when} local)",
        "",
        f"  • {t['events']} alerts in last {digest['window_hours']}h",
        f"  • {t['unique_aircraft_today']} unique aircraft observed today",
        f"  • Peak concurrent: {t['peak_concurrent_aircraft']}",
        f"  • Max range today: {t['max_range_nm_today']:.0f} nm",
    ]
    by = t["by_kind"]
    if by:
        bits = ", ".join(f"{k}={v}" for k, v in sorted(by.items(), key=lambda x: -x[1]))
        lines.append(f"  • Alert breakdown: {bits}")

    if digest["top_types"]:
        lines.append("")
        lines.append("Top aircraft types in window:")
        for r in digest["top_types"][:5]:
            lines.append(f"  - {r['type_code']:<6}  ×{r['sightings']}")

    if digest["new_types_in_window"]:
        lines.append("")
        lines.append("✨ New aircraft types first seen in window: " + ", ".join(digest["new_types_in_window"]))

    if digest["callouts"]:
        lines.append("")
        lines.append("Notable sightings:")
        for c in digest["callouts"][:8]:
            name = c.get("display_name") or c.get("callsign") or c.get("hex")
            kind = c["kind"]
            type_code = c.get("type_code") or "?"
            dist = c.get("distance_nm")
            dist_str = f"{dist:.0f}nm" if isinstance(dist, (int, float)) else "?"
            lines.append(f"  - [{kind:<9}] {name} ({type_code}, {dist_str})")

    if digest.get("ai_commentary"):
        lines.append("")
        lines.append("AI commentary:")
        lines.append(digest["ai_commentary"])

    return "\n".join(lines)


# ---------- AI flourish (optional) ----------


async def _maybe_add_ai_commentary(digest: dict[str, Any]) -> None:
    """If Ollama is configured, ask for a short paragraph framing the day's activity.
    Failure is silent — the templated digest is the source of truth."""
    from . import ollama  # local import to avoid hard dep cycle
    if not ollama.is_configured():
        return
    try:
        # Build a compact factual summary and ask for a 2-sentence narration.
        url = (settings_store.get("ollama_url") or "").strip().rstrip("/")
        model = (settings_store.get("ollama_model") or "gemma4:latest").strip()
        t = digest["totals"]
        top = ", ".join(f"{r['type_code']}x{r['sightings']}" for r in digest["top_types"][:3])
        new = ", ".join(digest["new_types_in_window"][:5])
        callout_lines = []
        for c in digest["callouts"][:5]:
            name = c.get("display_name") or c.get("callsign") or c.get("hex")
            callout_lines.append(f"- {c['kind']}: {name} ({c.get('type_code') or '?'})")
        prompt = (
            "You write a friendly 2-sentence summary for an aviation hobbyist's daily ADS-B report. "
            "Use ONLY the facts below. Be specific where you can; no padding, no greetings, no markdown.\n\n"
            f"Alerts in last {digest['window_hours']}h: {t['events']}\n"
            f"Unique aircraft today: {t['unique_aircraft_today']}\n"
            f"Peak concurrent: {t['peak_concurrent_aircraft']}\n"
            f"Max range nm: {t['max_range_nm_today']:.0f}\n"
            f"Top types: {top or 'none'}\n"
            f"New types: {new or 'none'}\n"
            "Notable callouts:\n" + ("\n".join(callout_lines) or "- none") + "\n\n"
            "Summary:"
        )
        from ._http import get_client
        body = {
            "model": model,
            "messages": [{"role": "user", "content": prompt}],
            "stream": False, "think": False,
            "keep_alive": settings_store.get("ollama_keep_alive") or 0,
            "options": {"temperature": 0.5, "top_p": 0.9, "num_predict": 200, "stop": ["\n\n\n"]},
        }
        client = await get_client()
        r = await client.post(f"{url}/api/chat", json=body, timeout=30.0)
        if r.status_code == 200:
            data = r.json()
            text = ((data.get("message") or {}).get("content") or "").strip()
            if not text:
                text = ((data.get("message") or {}).get("thinking") or "").strip()
            if text:
                digest["ai_commentary"] = text[:1000]
    except Exception as exc:
        log.info("digest AI commentary skipped: %s", exc)


# ---------- Delivery ----------


def _persist_in_app(digest: dict[str, Any]) -> None:
    """Stash the latest digest JSON in the settings table so the frontend can fetch it
    without us needing a new table. (`set_one` accepts arbitrary JSON-coercible values.)"""
    try:
        settings_store.set_one(_LATEST_KEY, digest)
    except Exception as exc:
        log.warning("digest persist failed: %s", exc)


def _send_email(text: str) -> tuple[bool, Optional[str]]:
    host = (settings_store.get("smtp_host") or "").strip()
    port = int(settings_store.get("smtp_port") or 587)
    user = (settings_store.get("smtp_user") or "").strip()
    password = settings_store.get("smtp_pass") or ""
    sender = (settings_store.get("smtp_from") or user).strip()
    recipient = (settings_store.get("smtp_to") or "").strip()
    use_starttls = bool(settings_store.get("smtp_use_starttls", True))
    if not host or not sender or not recipient:
        return False, "SMTP not configured"
    msg = EmailMessage()
    msg["Subject"] = "PiScope Radar — Daily Digest"
    msg["From"] = sender
    msg["To"] = recipient
    msg["Date"] = formatdate(localtime=True)
    msg.set_content(text)
    try:
        with smtplib.SMTP(host, port, timeout=15) as s:
            s.ehlo()
            if use_starttls:
                s.starttls(context=ssl.create_default_context())
                s.ehlo()
            if user and password:
                s.login(user, password)
            s.send_message(msg)
        return True, None
    except Exception as exc:
        return False, f"{type(exc).__name__}: {exc}"


def _send_webhook(text: str, digest: dict[str, Any]) -> None:
    payload = {
        "message": text,
        "digest": digest,
    }
    webhooks.fan_out("digest", payload)


async def run_digest(*, with_ai: bool = True) -> dict[str, Any]:
    """Build the digest, optionally enrich with AI, then deliver via every enabled channel.
    Returns the digest dict (with `delivery` populated) so callers — the scheduler and the
    `Send test digest` button — can report success/failure."""
    digest = build_digest()
    if with_ai:
        await _maybe_add_ai_commentary(digest)
    text = render_text(digest)

    delivery: dict[str, Any] = {}
    if settings_store.get("digest_deliver_in_app"):
        _persist_in_app(digest)
        delivery["in_app"] = "ok"
    if settings_store.get("digest_deliver_webhook"):
        try:
            _send_webhook(text, digest)
            delivery["webhook"] = "queued"
        except Exception as exc:
            delivery["webhook"] = f"error: {exc}"
    if settings_store.get("digest_deliver_email"):
        ok, err = _send_email(text)
        delivery["email"] = "ok" if ok else f"error: {err}"

    digest["delivery"] = delivery
    digest["rendered_text"] = text
    return digest


def get_latest() -> Optional[dict[str, Any]]:
    """Return the most-recently-persisted digest for the in-app card. Used by /api/digest."""
    return settings_store.get(_LATEST_KEY)


# ---------- Scheduler ----------


def _seconds_until(hhmm: str) -> float:
    """Return seconds from now until the next local-time HH:MM. If the time has passed
    today, schedules for tomorrow."""
    try:
        hh, mm = hhmm.split(":")
        target = dtime(int(hh), int(mm))
    except Exception:
        target = dtime(7, 30)  # fall back to default if user typed nonsense
    now = datetime.now()
    today_at = datetime.combine(now.date(), target)
    if today_at <= now:
        today_at = today_at + timedelta(days=1)
    return max(60.0, (today_at - now).total_seconds())


_TASK: Optional[asyncio.Task[None]] = None


async def _scheduler_loop() -> None:
    """Sleep until the next `digest_local_time`, then run the digest. Reads the setting
    each iteration so a user change applies on the NEXT cycle (no restart needed)."""
    log.info("digest scheduler started")
    while True:
        try:
            if not settings_store.get("digest_enabled"):
                # Re-check every 10 min if disabled, so toggling it on takes effect promptly.
                await asyncio.sleep(600)
                continue
            wait = _seconds_until(settings_store.get("digest_local_time") or "07:30")
            log.info("digest: next fire in %.0f s", wait)
            await asyncio.sleep(wait)
            await run_digest(with_ai=True)
            # Belt-and-braces: nudge by 60s so we don't accidentally double-fire if the
            # clock barely advanced.
            await asyncio.sleep(60)
        except asyncio.CancelledError:
            log.info("digest scheduler stopping")
            raise
        except Exception as exc:
            log.warning("digest scheduler error (continuing): %s", exc)
            await asyncio.sleep(60)


def start_scheduler() -> None:
    global _TASK
    if _TASK is not None and not _TASK.done():
        return
    _TASK = asyncio.create_task(_scheduler_loop(), name="piscope-digest-scheduler")


async def stop_scheduler() -> None:
    global _TASK
    if _TASK is None:
        return
    _TASK.cancel()
    try:
        await _TASK
    except (asyncio.CancelledError, Exception):
        pass
    _TASK = None
