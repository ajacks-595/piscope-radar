"""Claude-CLI shim — small HTTP daemon that wraps `claude -p` so a remote
service (PiScope, SOC dashboard, anything) can use the operator's Claude
Code subscription without itself running Claude Code.

Stdlib-only: no pip dependencies, no venv required. Works on anything with
Python 3.9+.

Contract
--------
POST /generate
    Auth:    Authorization: Bearer <SHIM_BEARER_TOKEN>  (if SHIM_BEARER_TOKEN set)
    Body:    {"prompt": "...", "num_predict"?: int, "temperature"?: float}
    Returns: 200 {"text": "..."} on success
             4xx/5xx {"error": "..."} on failure

GET /health
    Returns: 200 {"ok": true, "model": "...", "version": "..."}

Security
--------
The shim has full Claude Code account access. Defence in depth:
  1. Bind to a LAN address (default 0.0.0.0; override with SHIM_BIND_HOST).
  2. Bearer token via SHIM_BEARER_TOKEN env var (mandatory in production).
  3. IP allow-list via SHIM_ALLOW_IPS (comma-separated; e.g. "10.0.0.231").
  4. Prompt size cap (SHIM_MAX_PROMPT_BYTES, default 32 KiB).

Configuration via environment variables
---------------------------------------
SHIM_BIND_HOST           default 0.0.0.0
SHIM_BIND_PORT           default 8090
SHIM_BEARER_TOKEN        required for /generate in production
SHIM_ALLOW_IPS           comma-separated allow-list; empty = allow all
SHIM_CLAUDE_BIN          default "claude" (uses $PATH)
SHIM_CLAUDE_MODEL        passed as --model; empty = Claude Code's default
SHIM_MAX_PROMPT_BYTES    default 32768
SHIM_TIMEOUT_SECONDS     default 60
"""
from __future__ import annotations

import hmac
import json
import logging
import os
import shutil
import subprocess
import sys
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any, Optional


logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)-7s %(name)s: %(message)s",
)
log = logging.getLogger("claude-shim")


# --- Config -----------------------------------------------------------------

BIND_HOST = os.environ.get("SHIM_BIND_HOST", "0.0.0.0")
BIND_PORT = int(os.environ.get("SHIM_BIND_PORT") or 8090)
CLAUDE_BIN = os.environ.get("SHIM_CLAUDE_BIN", "claude")
CLAUDE_MODEL = (os.environ.get("SHIM_CLAUDE_MODEL") or "").strip()
BEARER_TOKEN = (os.environ.get("SHIM_BEARER_TOKEN") or "").strip()
ALLOW_IPS = {ip.strip() for ip in (os.environ.get("SHIM_ALLOW_IPS") or "").split(",") if ip.strip()}
MAX_PROMPT_BYTES = int(os.environ.get("SHIM_MAX_PROMPT_BYTES") or 32 * 1024)
TIMEOUT_SECONDS = int(os.environ.get("SHIM_TIMEOUT_SECONDS") or 60)
MAX_REQUEST_BYTES = MAX_PROMPT_BYTES + 4096   # JSON overhead


# --- Subprocess helpers -----------------------------------------------------

def _claude_version() -> Optional[str]:
    bin_path = shutil.which(CLAUDE_BIN)
    if not bin_path:
        return None
    try:
        out = subprocess.run(
            [bin_path, "--version"],
            capture_output=True, text=True, timeout=5,
        )
        return (out.stdout or "").strip().splitlines()[0] if out.stdout else None
    except Exception:
        return None


def _run_claude(prompt: str) -> tuple[Optional[str], Optional[str]]:
    """Returns (stdout_text, error_or_None)."""
    bin_path = shutil.which(CLAUDE_BIN)
    if not bin_path:
        return None, f"claude binary not on PATH ({CLAUDE_BIN!r})"
    # `--print` makes it a one-shot non-interactive call. Prompt comes from
    # stdin so we don't have to worry about argv length limits or shell escaping.
    #
    # NOTE: do NOT add `--bare`. Per `claude --help`, --bare disables OAuth and
    # keychain reads — auth becomes ANTHROPIC_API_KEY only. The whole point of
    # this shim is to piggyback on the operator's OAuth login, so --bare would
    # defeat the purpose. Side effects from hooks/MCP are mostly harmless for a
    # headless one-shot prompt; if you need to suppress them, set them off in
    # ~/.claude/settings.json rather than reaching for --bare.
    args = [bin_path, "--print"]
    if CLAUDE_MODEL:
        args += ["--model", CLAUDE_MODEL]
    try:
        proc = subprocess.run(
            args,
            input=prompt,
            capture_output=True,
            text=True,
            timeout=TIMEOUT_SECONDS,
        )
    except subprocess.TimeoutExpired:
        return None, f"claude timed out after {TIMEOUT_SECONDS}s"
    except FileNotFoundError:
        return None, "claude binary disappeared mid-call"
    if proc.returncode != 0:
        err = (proc.stderr or "").strip()[:500] or f"exit {proc.returncode}"
        log.warning("claude exited %d: %s", proc.returncode, err)
        return None, f"claude exited {proc.returncode}: {err}"
    text = (proc.stdout or "").strip()
    if not text:
        return None, "claude returned empty output"
    return text, None


# --- HTTP handler -----------------------------------------------------------

class Handler(BaseHTTPRequestHandler):
    # Suppress the noisy default per-request stderr log; we log structured
    # events from the handlers themselves.
    def log_message(self, fmt: str, *args: Any) -> None:  # noqa: A003
        log.info("%s - %s", self.address_string(), fmt % args)

    def _send_json(self, status: int, body: dict[str, Any]) -> None:
        payload = json.dumps(body).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(payload)))
        self.end_headers()
        self.wfile.write(payload)

    def _client_ip(self) -> str:
        return (self.client_address[0] if self.client_address else "") or ""

    def _check_ip(self) -> Optional[tuple[int, dict[str, Any]]]:
        if not ALLOW_IPS:
            return None
        peer = self._client_ip()
        if peer not in ALLOW_IPS:
            log.warning("rejected request from %s (not in allow-list)", peer)
            return 403, {"error": "IP not allowed"}
        return None

    def _check_auth(self) -> Optional[tuple[int, dict[str, Any]]]:
        if not BEARER_TOKEN:
            return None
        got = self.headers.get("Authorization", "")
        # Constant-time compare — this token guards full Claude Code account
        # access, so don't leak its length/prefix via early-exit `!=` timing.
        # Compare as bytes so a non-ASCII Authorization header can't raise.
        expected = f"Bearer {BEARER_TOKEN}"
        if not hmac.compare_digest(got.encode("utf-8", "replace"), expected.encode("utf-8")):
            return 401, {"error": "invalid or missing bearer token"}
        return None

    def do_GET(self) -> None:  # noqa: N802
        if self.path == "/health":
            ip_err = self._check_ip()
            if ip_err:
                self._send_json(*ip_err); return
            bin_path = shutil.which(CLAUDE_BIN)
            if not bin_path:
                self._send_json(200, {"ok": False, "error": f"claude binary not on PATH ({CLAUDE_BIN!r})"})
                return
            self._send_json(200, {
                "ok": True,
                "version": _claude_version() or "",
                "model": CLAUDE_MODEL or "(default)",
                "bin": bin_path,
            })
            return
        self._send_json(404, {"error": "not found"})

    def do_POST(self) -> None:  # noqa: N802
        if self.path != "/generate":
            self._send_json(404, {"error": "not found"})
            return
        guard = self._check_ip() or self._check_auth()
        if guard:
            self._send_json(*guard); return

        # Length check first so we never read an unbounded body into memory.
        try:
            length = int(self.headers.get("Content-Length") or "0")
        except ValueError:
            self._send_json(400, {"error": "invalid Content-Length"}); return
        if length <= 0:
            self._send_json(400, {"error": "missing body"}); return
        if length > MAX_REQUEST_BYTES:
            self._send_json(413, {"error": f"body too large: {length} > {MAX_REQUEST_BYTES}"}); return

        try:
            raw = self.rfile.read(length)
            body = json.loads(raw.decode("utf-8"))
        except (json.JSONDecodeError, UnicodeDecodeError) as exc:
            self._send_json(400, {"error": f"invalid JSON: {exc}"}); return

        prompt = body.get("prompt")
        if not isinstance(prompt, str) or not prompt.strip():
            self._send_json(400, {"error": "prompt required (non-empty string)"}); return
        if len(prompt.encode("utf-8")) > MAX_PROMPT_BYTES:
            self._send_json(413, {"error": f"prompt too large (> {MAX_PROMPT_BYTES} bytes)"}); return

        text, err = _run_claude(prompt)
        if err:
            # 502 — we're an upstream proxy and the upstream (claude) failed.
            self._send_json(502, {"error": err})
            return
        self._send_json(200, {"text": text})


# --- Entrypoint -------------------------------------------------------------

def main() -> int:
    if not BEARER_TOKEN:
        log.warning("SHIM_BEARER_TOKEN not set — /generate is OPEN. Set it before exposing to LAN.")
    log.info("claude-shim listening on %s:%d (claude=%r, model=%r, allow_ips=%s)",
             BIND_HOST, BIND_PORT, CLAUDE_BIN, CLAUDE_MODEL or "(default)",
             ",".join(sorted(ALLOW_IPS)) or "ALL")
    server = ThreadingHTTPServer((BIND_HOST, BIND_PORT), Handler)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        log.info("shutting down")
    finally:
        server.server_close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
