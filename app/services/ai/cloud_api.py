"""Cloud-API provider — Anthropic, OpenAI, Google, or any OpenAI-compatible host.

One module, four vendor backends behind the same `is_configured / ping /
generate` interface. The vendor is selected by the `cloud_api_vendor`
setting; the API key lives in `cloud_api_key` and is treated as a secret
(redacted in /api/settings). All four share `cloud_api_model` as the
single model-name slot — switching vendor typically means switching model
too, so a per-vendor model cache wasn't worth the UX cost.

The `openai_compatible` vendor speaks the exact OpenAI chat/completions +
`/models` schema (so its request/response handling is shared with `openai`)
but points at a user-supplied base URL in `cloud_api_base_url` — Ollama
Cloud, LiteLLM, vLLM, LocalAI, Groq, Together, OpenRouter, etc. The API key
is optional for it (open self-hosted servers often need none); only a
reachable, non-metadata base URL is required.

Security posture
----------------
* API key is admin-only (same posture as fa_api_key / openaip_api_key — the
  Pi's web UI has no auth; whoever can hit /api/settings already controls
  the box).
* Key is never returned over the wire — `SECRET_KEYS` in settings.py
  replaces it with "***" on read and a `cloud_api_key_set` boolean lets the
  UI know whether one is stored.
* Anthropic/OpenAI/Google endpoints are hard-coded. The `openai_compatible`
  base URL is the only user-redirectable host, so it goes through the shared
  SSRF guard (`validate_external_url`) — link-local / cloud-metadata targets
  are refused; LAN + loopback are allowed on purpose (LiteLLM/vLLM on the LAN).
"""
from __future__ import annotations

import logging
from typing import Any, Optional

from .. import settings as settings_store
from .._http import get_client, validate_external_url


log = logging.getLogger("piscope.ai.cloud_api")


VENDORS = ("anthropic", "openai", "google", "openai_compatible")
DEFAULT_MODELS: dict[str, str] = {
    # Cheap + capable defaults — short briefs don't need flagship-tier models.
    "anthropic": "claude-haiku-4-5",
    "openai": "gpt-4o-mini",
    "google": "gemini-2.5-flash",
    # No universal default for a bring-your-own OpenAI-compatible host — the
    # model name is whatever that endpoint exposes, so the user must set it.
    "openai_compatible": "",
}


def _vendor() -> str:
    v = (settings_store.get("cloud_api_vendor") or "").strip().lower()
    return v if v in VENDORS else "anthropic"


def _key() -> str:
    return (settings_store.get("cloud_api_key") or "").strip()


def _model() -> str:
    v = _vendor()
    configured = (settings_store.get("cloud_api_model") or "").strip()
    return configured or DEFAULT_MODELS.get(v, "")


def _base_url() -> Optional[str]:
    """Validated `openai_compatible` base URL (trailing slash trimmed), or None if
    unset, the wrong scheme, or pointing at a blocked (link-local/metadata) address.
    Mirrors the ollama provider's `_validated_url` — non-resolving so it's safe to
    call inline on the event loop; httpx re-resolves at request time regardless."""
    url = (settings_store.get("cloud_api_base_url") or "").strip().rstrip("/")
    if not url.startswith(("http://", "https://")):
        return None
    try:
        validate_external_url(url)
    except ValueError:
        return None
    return url


def is_configured() -> bool:
    if not bool(settings_store.get("cloud_api_enabled")):
        return False
    v = _vendor()
    if v == "openai_compatible":
        # Key optional (open LAN servers); a reachable base URL is what's required.
        return _base_url() is not None
    return bool(_key()) and v in VENDORS


async def ping() -> dict[str, Any]:
    v = _vendor()
    key = _key()
    base = None
    if v == "openai_compatible":
        base = _base_url()
        if base is None:
            return {"ok": False, "error": "Base URL not set, wrong scheme, or blocked (link-local/metadata)"}
    elif not key:
        return {"ok": False, "error": "API key not set"}
    model = _model()
    try:
        client = await get_client()
        if v == "anthropic":
            r = await client.get(
                "https://api.anthropic.com/v1/models",
                headers={"x-api-key": key, "anthropic-version": "2023-06-01"},
                timeout=8.0,
            )
            if r.status_code != 200:
                return {"ok": False, "error": f"HTTP {r.status_code}: {r.text[:120]}"}
            models = [m.get("id") for m in (r.json().get("data") or [])]
        elif v in ("openai", "openai_compatible"):
            # Shared OpenAI schema. `openai` uses the hard-coded host; `openai_compatible`
            # uses the validated user base URL. The key is optional for the latter, so we
            # only attach the Authorization header when one is actually set.
            endpoint_base = "https://api.openai.com/v1" if v == "openai" else base
            headers = {"Authorization": f"Bearer {key}"} if key else {}
            r = await client.get(f"{endpoint_base}/models", headers=headers, timeout=8.0)
            if r.status_code != 200:
                return {"ok": False, "error": f"HTTP {r.status_code}: {r.text[:120]}"}
            models = [m.get("id") for m in (r.json().get("data") or [])]
        else:  # google
            # Key goes in the x-goog-api-key header, NOT the URL query string —
            # a key in the URL leaks into proxy/access logs and httpx error reprs.
            r = await client.get(
                "https://generativelanguage.googleapis.com/v1beta/models",
                headers={"x-goog-api-key": key},
                timeout=8.0,
            )
            if r.status_code != 200:
                return {"ok": False, "error": f"HTTP {r.status_code}: {r.text[:120]}"}
            # Names look like "models/gemini-2.5-flash" — strip the prefix for UI display.
            models = [(m.get("name") or "").replace("models/", "") for m in (r.json().get("models") or [])]
        model_present = (not model) or any(m == model or m.startswith(f"{model}-") for m in models if m)
        return {
            "ok": True,
            "vendor": v,
            "models": [m for m in models if m][:50],   # cap for UI; OpenAI returns ~100+
            "model_present": model_present,
            "configured_model": model,
        }
    except Exception as exc:
        return {"ok": False, "error": f"{type(exc).__name__}: {exc}"}


async def generate(prompt: str, *, num_predict: int = 360, temperature: float = 0.5) -> Optional[str]:
    """Single-shot call to the configured cloud vendor. Returns text or None
    on any failure — the caller's caching layer treats None as 'unavailable'."""
    v = _vendor()
    key = _key()
    base = None
    if v == "openai_compatible":
        base = _base_url()
        if base is None:
            log.info("openai_compatible base URL not set or blocked")
            return None
    elif not key:
        return None
    model = _model()
    client = await get_client()
    try:
        if v == "anthropic":
            r = await client.post(
                "https://api.anthropic.com/v1/messages",
                headers={
                    "x-api-key": key,
                    "anthropic-version": "2023-06-01",
                    "content-type": "application/json",
                },
                json={
                    "model": model,
                    "max_tokens": num_predict,
                    "temperature": temperature,
                    "messages": [{"role": "user", "content": prompt}],
                },
                timeout=30.0,
            )
            if r.status_code != 200:
                log.info("anthropic returned %d: %s", r.status_code, r.text[:200])
                return None
            data = r.json()
            blocks = data.get("content") or []
            text = "".join(b.get("text", "") for b in blocks if b.get("type") == "text")
            return text.strip() or None
        elif v in ("openai", "openai_compatible"):
            # Shared OpenAI chat/completions schema; `openai` hits the hard-coded host,
            # `openai_compatible` hits the validated base URL with an optional key.
            endpoint_base = "https://api.openai.com/v1" if v == "openai" else base
            headers = {"content-type": "application/json"}
            if key:
                headers["Authorization"] = f"Bearer {key}"
            r = await client.post(
                f"{endpoint_base}/chat/completions",
                headers=headers,
                json={
                    "model": model,
                    "messages": [{"role": "user", "content": prompt}],
                    "max_tokens": num_predict,
                    "temperature": temperature,
                },
                timeout=30.0,
            )
            if r.status_code != 200:
                log.info("%s returned %d: %s", v, r.status_code, r.text[:200])
                return None
            data = r.json()
            choices = data.get("choices") or []
            if not choices:
                return None
            text = ((choices[0].get("message") or {}).get("content") or "").strip()
            return text or None
        else:  # google
            r = await client.post(
                f"https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent",
                headers={"x-goog-api-key": key},
                json={
                    "contents": [{"parts": [{"text": prompt}]}],
                    "generationConfig": {
                        "maxOutputTokens": num_predict,
                        "temperature": temperature,
                    },
                },
                timeout=30.0,
            )
            if r.status_code != 200:
                log.info("google returned %d: %s", r.status_code, r.text[:200])
                return None
            data = r.json()
            cands = data.get("candidates") or []
            if not cands:
                return None
            parts = ((cands[0].get("content") or {}).get("parts") or [])
            text = "".join(p.get("text", "") for p in parts).strip()
            return text or None
    except Exception as exc:
        log.info("cloud_api generate failed (%s): %s", v, exc)
        return None
