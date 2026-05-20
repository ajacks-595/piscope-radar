"""Outbound webhook fan-out for events (military / emergency / watchlist / rare) plus
the receiver-health system events (feed_down / feed_recovered).

Configured by the user as a JSON list in the `webhooks_json` setting. Each entry:
    { "kind": "discord" | "slack" | "ntfy" | "generic",
      "url": "https://...",
      "types": ["emergency", "military", "watchlist", "rare", "feed_down", "feed_recovered"] }

Posts run via `fire_and_forget` so the feed loop never blocks waiting on a third party.
"""
from __future__ import annotations

import asyncio
import json
import logging
from datetime import datetime, timezone
from typing import Any

from . import settings as settings_store
from ._http import get_client


log = logging.getLogger("piscope.webhooks")


def _format_text(kind: str, ac: dict[str, Any]) -> str:
    when = datetime.now(timezone.utc).strftime("%H:%M UTC")
    # System events (watchdog) don't have aircraft metadata — render the supplied message verbatim
    # rather than the aircraft template.
    if kind in ("feed_down", "feed_recovered"):
        label = "📡 Receiver offline" if kind == "feed_down" else "✅ Receiver back online"
        return f"{label}: {ac.get('message', '(no detail)')} at {when}"
    name = ac.get("display_name") or ac.get("hex", "?")
    type_code = ac.get("type_code") or "?"
    dist = ac.get("distance_nm")
    label = {
        "emergency": "🚨 Emergency squawk",
        "military":  "🛡️ Military aircraft",
        "watchlist": "⭐ Watchlist match",
        "rare":      "✨ Rare type sighted",
    }.get(kind, kind.title())
    dist_str = f"{dist:.0f} nm" if isinstance(dist, (int, float)) else "—"
    extras = []
    if ac.get("squawk"):
        extras.append(f"squawk {ac['squawk']}")
    if ac.get("altitude_baro"):
        extras.append(f"FL{ac['altitude_baro']//100}")
    extra_str = (" · " + " · ".join(extras)) if extras else ""
    return f"{label}: **{name}** ({type_code}, {dist_str}){extra_str} at {when}"


def _body_for(kind_endpoint: str, message: str, ac: dict[str, Any]) -> tuple[str, dict[str, Any]]:
    """Return (content_type, body_dict) tailored to the endpoint flavour."""
    if kind_endpoint == "discord":
        return "application/json", {"content": message}
    if kind_endpoint == "slack":
        return "application/json", {"text": message}
    if kind_endpoint == "ntfy":
        # ntfy uses plain text in the body and headers for title/tags.
        return "text/plain", message  # special-cased below
    # generic — POST the full payload as JSON
    return "application/json", {"message": message, "aircraft": ac}


async def _post_one(endpoint: dict[str, Any], message: str, ac: dict[str, Any]) -> None:
    url = endpoint.get("url")
    kind_endpoint = (endpoint.get("kind") or "generic").lower()
    if not isinstance(url, str) or not url.startswith(("http://", "https://")):
        return
    try:
        content_type, body = _body_for(kind_endpoint, message, ac)
        client = await get_client()
        if kind_endpoint == "ntfy":
            # ntfy expects text/plain body + optional Title / Tags headers.
            headers = {
                "Content-Type": "text/plain; charset=utf-8",
                "Title": "PiScope Radar alert",
                "Tags": ac.get("hex", ""),
            }
            await client.post(url, content=body, headers=headers, timeout=6.0)
        else:
            await client.post(url, json=body, headers={"Content-Type": content_type}, timeout=6.0)
    except Exception as exc:
        # Webhooks failing must NEVER break the feed loop.
        log.info("webhook to %s failed: %s", url, type(exc).__name__)


def fan_out(kind: str, ac_payload: dict[str, Any]) -> None:
    """Fire-and-forget broadcast. Reads the current webhook list, fans out to each that
    subscribed to this event kind. Returns immediately; failed posts are silently dropped."""
    try:
        raw = settings_store.get("webhooks_json") or "[]"
        endpoints = json.loads(raw)
    except Exception:
        endpoints = []
    if not isinstance(endpoints, list) or not endpoints:
        return
    message = _format_text(kind, ac_payload)
    interested = [e for e in endpoints if isinstance(e, dict) and (kind in (e.get("types") or []))]
    if not interested:
        return
    # Schedule the posts without awaiting — we don't want the feed loop to block.
    try:
        loop = asyncio.get_running_loop()
    except RuntimeError:
        return
    for ep in interested:
        loop.create_task(_post_one(ep, message, ac_payload))
