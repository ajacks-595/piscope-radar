"""Ollama provider — speaks the Ollama HTTP API.

Works against vanilla Ollama, LiteLLM, and llama.cpp's server. The endpoint
URL is admin-only (anyone who can reach the settings UI already has full
admin access on the Pi), but we still require `http(s)://` to block file://,
gopher://, etc.
"""
from __future__ import annotations

import logging
from typing import Any, Optional

from .. import settings as settings_store
from .._http import get_client, validate_external_url


log = logging.getLogger("piscope.ai.ollama")


def _validated_url() -> Optional[str]:
    """Configured Ollama base URL (trailing slash trimmed), or None if it's unset, the wrong
    scheme, or points at a blocked address. The SSRF guard rejects link-local / cloud-metadata
    while still allowing LAN and loopback — Ollama commonly runs on a LAN box or the same host."""
    url = (settings_store.get("ollama_url") or "").strip().rstrip("/")
    if not url.startswith(("http://", "https://")):
        return None
    try:
        validate_external_url(url)
    except ValueError:
        return None
    return url


def is_configured() -> bool:
    return bool(settings_store.get("ollama_enabled")) and _validated_url() is not None


async def ping() -> dict[str, Any]:
    url = (settings_store.get("ollama_url") or "").strip()
    model = (settings_store.get("ollama_model") or "").strip()
    if not url.startswith(("http://", "https://")):
        return {"ok": False, "error": "URL must start with http:// or https://"}
    try:
        validate_external_url(url)
    except ValueError as exc:
        return {"ok": False, "error": f"URL blocked: {exc}"}
    try:
        client = await get_client()
        r = await client.get(f"{url.rstrip('/')}/api/tags", timeout=5.0)
        if r.status_code != 200:
            return {"ok": False, "error": f"HTTP {r.status_code}"}
        models = [m.get("name") for m in (r.json().get("models") or [])]
        model_present = (not model) or any(m == model or m.startswith(f"{model}:") for m in models)
        return {
            "ok": True,
            "models": models,
            "model_present": model_present,
            "configured_model": model,
        }
    except Exception as exc:
        return {"ok": False, "error": f"{type(exc).__name__}: {exc}"}


async def generate(prompt: str, *, num_predict: int = 360, temperature: float = 0.5) -> Optional[str]:
    """Single-shot Ollama call. Returns text or None on failure.

    Uses /api/chat so each model's chat template is applied — required for
    Gemma 4 family, harmless for older models. `think: false` keeps reasoning
    models from burning the predict budget on internal monologue.
    """
    url = _validated_url()
    if url is None:
        return None
    model = (settings_store.get("ollama_model") or "gemma4:latest").strip()
    body = {
        "model": model,
        "messages": [{"role": "user", "content": prompt}],
        "stream": False,
        "think": False,
        # `0` forces unload right after the response so a shared Mac/NAS isn't
        # holding 5+ GB of model RAM between calls. Cold-load latency on the
        # next call is the ~1s tradeoff; the keep_alive setting overrides this
        # for users who prefer steady RAM in exchange for snappy follow-ups.
        "keep_alive": settings_store.get("ollama_keep_alive") or 0,
        "options": {
            "temperature": temperature,
            "top_p": 0.9,
            "num_predict": num_predict,
            "stop": ["\n\n\n"],
        },
    }
    client = await get_client()
    r = await client.post(f"{url}/api/chat", json=body, timeout=25.0)
    if r.status_code != 200:
        log.info("ollama returned %d: %s", r.status_code, r.text[:200])
        return None
    data = r.json()
    message = data.get("message") or {}
    text = (message.get("content") or "").strip()
    if not text:
        # Some models drop content into "thinking" if think:false isn't honoured.
        text = (message.get("thinking") or "").strip()
    return text or None
