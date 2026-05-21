"""Claude-CLI provider — POSTs prompts to a small HTTP shim that wraps
`claude -p` on a host where Claude Code is installed and authenticated.

This is the "use my Claude Code subscription instead of an API key" path.
The shim itself lives in `tools/claude-shim/` in this repo; deploy it on
a LAN box (typically a dev machine), point `claude_cli_url` at it, and the
Pi never needs Claude Code installed locally.

Shim contract
-------------
POST {claude_cli_url}/generate
    Request:  {"prompt": "...", "num_predict": 360, "temperature": 0.5}
    Response: {"text": "..."}  on success
              {"error": "..."} on failure (non-200 status)

GET {claude_cli_url}/health
    Response: {"ok": true, "model": "claude-...", "version": "..."}

Authorization
-------------
If `claude_cli_token` is set, sent as `Authorization: Bearer <token>` on
every request. Recommended even on a LAN — the shim shells out to a CLI
that has full access to the operator's account.
"""
from __future__ import annotations

import logging
from typing import Any, Optional

from .. import settings as settings_store
from .._http import get_client


log = logging.getLogger("piscope.ai.claude_cli")


def _url() -> str:
    return (settings_store.get("claude_cli_url") or "").strip().rstrip("/")


def _headers() -> dict[str, str]:
    h = {"content-type": "application/json"}
    token = (settings_store.get("claude_cli_token") or "").strip()
    if token:
        h["Authorization"] = f"Bearer {token}"
    return h


def is_configured() -> bool:
    if not bool(settings_store.get("claude_cli_enabled")):
        return False
    url = _url()
    return url.startswith(("http://", "https://"))


async def ping() -> dict[str, Any]:
    url = _url()
    if not url.startswith(("http://", "https://")):
        return {"ok": False, "error": "URL must start with http:// or https://"}
    try:
        client = await get_client()
        r = await client.get(f"{url}/health", headers=_headers(), timeout=8.0)
        if r.status_code == 401:
            return {"ok": False, "error": "401 unauthorized — token mismatch?"}
        if r.status_code != 200:
            return {"ok": False, "error": f"HTTP {r.status_code}: {r.text[:120]}"}
        data = r.json()
        return {
            "ok": bool(data.get("ok", True)),
            "model": data.get("model") or "",
            "version": data.get("version") or "",
            "shim_url": url,
        }
    except Exception as exc:
        return {"ok": False, "error": f"{type(exc).__name__}: {exc}"}


async def generate(prompt: str, *, num_predict: int = 360, temperature: float = 0.5) -> Optional[str]:
    """Single-shot call to the shim. Returns text or None."""
    url = _url()
    if not url.startswith(("http://", "https://")):
        return None
    try:
        client = await get_client()
        r = await client.post(
            f"{url}/generate",
            headers=_headers(),
            json={
                "prompt": prompt,
                "num_predict": num_predict,
                "temperature": temperature,
            },
            # Claude CLI cold-starts can take a few seconds; generation itself
            # is usually quick. 60s gives headroom without hanging the request
            # indefinitely.
            timeout=60.0,
        )
        if r.status_code != 200:
            log.info("claude_cli shim returned %d: %s", r.status_code, r.text[:200])
            return None
        data = r.json()
        text = (data.get("text") or "").strip()
        return text or None
    except Exception as exc:
        log.info("claude_cli generate failed: %s", exc)
        return None
