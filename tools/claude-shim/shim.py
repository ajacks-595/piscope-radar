"""Claude-CLI shim — small HTTP daemon that wraps `claude -p` so a remote
service (PiScope, SOC dashboard, anything) can use the operator's Claude
Code subscription without itself running Claude Code.

Contract
--------
POST /generate
    Auth:    Authorization: Bearer <SHIM_BEARER_TOKEN>  (if SHIM_BEARER_TOKEN set)
    Body:    {"prompt": "...", "num_predict"?: 360, "temperature"?: 0.5}
    Returns: 200 {"text": "..."} on success
             4xx/5xx {"detail": "..."} on failure

GET /health
    Returns: {"ok": true, "model": "...", "version": "..."}

Security
--------
The shim has full Claude Code account access. Defence in depth:
  1. Bind to a LAN address (default 0.0.0.0; override with SHIM_BIND_HOST).
     A reverse proxy or firewall in front is recommended.
  2. Bearer token via SHIM_BEARER_TOKEN env var (mandatory in production).
  3. IP allow-list via SHIM_ALLOW_IPS (comma-separated; e.g. "10.0.0.231").
     Empty means allow all.
  4. Prompt size cap (SHIM_MAX_PROMPT_BYTES, default 32 KiB).

Configuration via environment variables
---------------------------------------
SHIM_BIND_HOST           default 0.0.0.0
SHIM_BIND_PORT           default 8090
SHIM_BEARER_TOKEN        required for /generate; recommended even on LAN
SHIM_ALLOW_IPS           comma-separated allow-list; empty = allow all
SHIM_CLAUDE_BIN          default "claude" (uses $PATH)
SHIM_CLAUDE_MODEL        passed as --model; empty = Claude Code's default
SHIM_MAX_PROMPT_BYTES    default 32768
SHIM_TIMEOUT_SECONDS     default 60
"""
from __future__ import annotations

import asyncio
import logging
import os
import shutil
from typing import Any, Optional

from fastapi import FastAPI, Header, HTTPException, Request
from pydantic import BaseModel, Field


logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)-7s %(name)s: %(message)s",
)
log = logging.getLogger("claude-shim")


# --- Config -----------------------------------------------------------------

CLAUDE_BIN = os.environ.get("SHIM_CLAUDE_BIN", "claude")
CLAUDE_MODEL = (os.environ.get("SHIM_CLAUDE_MODEL") or "").strip()
BEARER_TOKEN = (os.environ.get("SHIM_BEARER_TOKEN") or "").strip()
ALLOW_IPS = {ip.strip() for ip in (os.environ.get("SHIM_ALLOW_IPS") or "").split(",") if ip.strip()}
MAX_PROMPT_BYTES = int(os.environ.get("SHIM_MAX_PROMPT_BYTES") or 32 * 1024)
TIMEOUT_SECONDS = int(os.environ.get("SHIM_TIMEOUT_SECONDS") or 60)


app = FastAPI(title="claude-shim", version="1.0.0")


# --- Models -----------------------------------------------------------------

class GenerateBody(BaseModel):
    prompt: str = Field(..., min_length=1)
    # The shim accepts these for shape-compatibility with our other AI providers,
    # but `claude -p` doesn't expose them as flags. They're recorded for future use
    # (e.g. if we later switch to the Messages API under the hood).
    num_predict: Optional[int] = Field(default=None, ge=1, le=4096)
    temperature: Optional[float] = Field(default=None, ge=0.0, le=2.0)


# --- Guards -----------------------------------------------------------------


def _check_ip(request: Request) -> None:
    if not ALLOW_IPS:
        return
    # request.client.host respects X-Forwarded-For only if you've configured a
    # ProxyHeadersMiddleware; we deliberately don't, so this is the *direct*
    # peer. Reverse-proxy this with care.
    peer = (request.client.host if request.client else "") or ""
    if peer not in ALLOW_IPS:
        log.warning("rejected request from %s (not in allow-list)", peer)
        raise HTTPException(status_code=403, detail="IP not allowed")


def _check_auth(authorization: Optional[str]) -> None:
    if not BEARER_TOKEN:
        return  # no token configured → open access (only use for localhost dev!)
    expected = f"Bearer {BEARER_TOKEN}"
    if not authorization or authorization != expected:
        raise HTTPException(status_code=401, detail="invalid or missing bearer token")


# --- Endpoints --------------------------------------------------------------


@app.get("/health")
async def health() -> dict[str, Any]:
    """Cheap probe. Reports the claude binary's version and whichever model
    we'd use for generation. Does NOT actually invoke claude — it just shells
    out to `claude --version` so it stays fast."""
    bin_path = shutil.which(CLAUDE_BIN)
    if not bin_path:
        return {"ok": False, "error": f"claude binary not on PATH ({CLAUDE_BIN!r})"}
    try:
        proc = await asyncio.create_subprocess_exec(
            bin_path, "--version",
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        out, _ = await asyncio.wait_for(proc.communicate(), timeout=5)
        version = out.decode("utf-8", "replace").strip().splitlines()[0] if out else ""
    except Exception as exc:
        return {"ok": False, "error": f"{type(exc).__name__}: {exc}"}
    return {
        "ok": True,
        "version": version,
        "model": CLAUDE_MODEL or "(default)",
        "bin": bin_path,
    }


@app.post("/generate")
async def generate(
    body: GenerateBody,
    request: Request,
    authorization: Optional[str] = Header(default=None),
) -> dict[str, Any]:
    _check_ip(request)
    _check_auth(authorization)

    prompt_bytes = len(body.prompt.encode("utf-8"))
    if prompt_bytes > MAX_PROMPT_BYTES:
        raise HTTPException(
            status_code=413,
            detail=f"prompt too large: {prompt_bytes} > {MAX_PROMPT_BYTES} bytes",
        )

    bin_path = shutil.which(CLAUDE_BIN)
    if not bin_path:
        raise HTTPException(status_code=500, detail=f"claude binary not on PATH ({CLAUDE_BIN!r})")

    # `--bare` skips hooks, MCP, plugin sync, auto-memory — keeps the shim
    # fast and avoids surprising side effects. `--print` makes it a one-shot
    # non-interactive call. Prompt comes from stdin so we don't have to worry
    # about argv length limits or shell escaping.
    args = [bin_path, "--bare", "--print"]
    if CLAUDE_MODEL:
        args += ["--model", CLAUDE_MODEL]

    try:
        proc = await asyncio.create_subprocess_exec(
            *args,
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        try:
            stdout, stderr = await asyncio.wait_for(
                proc.communicate(input=body.prompt.encode("utf-8")),
                timeout=TIMEOUT_SECONDS,
            )
        except asyncio.TimeoutError:
            proc.kill()
            await proc.wait()
            raise HTTPException(status_code=504, detail=f"claude timed out after {TIMEOUT_SECONDS}s")
    except FileNotFoundError:
        raise HTTPException(status_code=500, detail="claude binary disappeared mid-call")

    if proc.returncode != 0:
        err = stderr.decode("utf-8", "replace").strip()[:500] or "unknown error"
        log.warning("claude exited %d: %s", proc.returncode, err)
        raise HTTPException(status_code=502, detail=f"claude exited {proc.returncode}: {err}")

    text = stdout.decode("utf-8", "replace").strip()
    if not text:
        raise HTTPException(status_code=502, detail="claude returned empty output")
    return {"text": text}


# --- Entrypoint -------------------------------------------------------------

if __name__ == "__main__":
    import uvicorn
    host = os.environ.get("SHIM_BIND_HOST", "0.0.0.0")
    port = int(os.environ.get("SHIM_BIND_PORT") or 8090)
    if not BEARER_TOKEN:
        log.warning("SHIM_BEARER_TOKEN not set — /generate is OPEN. Set it before exposing to LAN.")
    uvicorn.run(app, host=host, port=port, log_level="info")
