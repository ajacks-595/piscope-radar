"""AI provider façade — call this, not any specific provider module.

Exposes the cache + dedup + sanitisation infra (in `_common`) and the
per-provider HTTP shims (`ollama`, `cloud_api`, `claude_cli`). The active
provider is picked at call time from the `ai_provider` setting; legacy
deployments with `ollama_enabled=true` are auto-migrated by `settings.init_db`.

Public surface
--------------
* `is_configured()`              — fast predicate for /api/explain/status
* `active_provider_name()`       — for the UI / status endpoint
* `async ping()`                 — exercise the configured provider; returns
                                   provider-specific `{ok, ...}` envelopes
* `async generate(prompt)`       — generic single-shot, used by digest
* `async explain(payload)`       — aircraft brief with cache + dedup
* `clear_cache()`                — drop cached explanations
"""
from __future__ import annotations

import asyncio
import logging
from typing import Any, Optional

from . import _common
from . import ollama as _ollama
from . import cloud_api as _cloud_api
from . import claude_cli as _claude_cli
from .. import settings as settings_store


log = logging.getLogger("piscope.ai")


# Provider name → module. Each module must expose:
#   is_configured() -> bool
#   async ping() -> dict
#   async generate(prompt: str) -> Optional[str]
_PROVIDERS: dict[str, Any] = {
    "ollama": _ollama,
    "cloud_api": _cloud_api,
    "claude_cli": _claude_cli,
}


def active_provider_name() -> str:
    """Resolve the configured provider name, with a back-compat fallback.

    `ai_provider` is the iteration-7 setting. Before iteration 7 the only
    provider was Ollama, gated by `ollama_enabled`; migration sets
    `ai_provider='ollama'` in those cases, but we double-belt here so a
    deployment that skipped migration still works.
    """
    name = (settings_store.get("ai_provider") or "").strip().lower()
    if name in _PROVIDERS:
        return name
    if bool(settings_store.get("ollama_enabled")):
        return "ollama"
    return "none"


def _active_module() -> Optional[Any]:
    name = active_provider_name()
    return _PROVIDERS.get(name)


def is_configured() -> bool:
    mod = _active_module()
    return bool(mod and mod.is_configured())


async def ping() -> dict[str, Any]:
    mod = _active_module()
    if mod is None:
        return {"ok": False, "error": "No AI provider selected"}
    return await mod.ping()


async def generate(prompt: str, **kwargs: Any) -> Optional[str]:
    """Bare-prompt single-shot call against the active provider. Used by the
    digest service for the daily AI commentary. No caching or dedup applied —
    the caller's prompts are too one-off for cache hits to ever land."""
    mod = _active_module()
    if mod is None:
        return None
    try:
        return await mod.generate(prompt, **kwargs)
    except Exception as exc:
        log.info("ai.generate failed: %s", exc)
        return None


async def explain(payload: dict[str, Any]) -> dict[str, Any]:
    """Produce a one-paragraph natural-language brief about an aircraft.

    Returns one of:
        {"text": "...", "source": "ai", "provider": "..."}     fresh response
        {"text": "...", "source": "cache", "provider": "..."}  cache hit
        {"source": "unavailable", "error": "...", "provider": "..."}
    """
    provider = active_provider_name()
    if not is_configured():
        return {"source": "unavailable", "error": f"{provider} not configured", "provider": provider}

    key = _common.aircraft_cache_key(payload, provider)
    cached = _common.cache_get(key)
    if cached is not None:
        return {"text": cached, "source": "cache", "provider": provider}

    existing = _common.inflight_get(key)
    if existing is not None:
        try:
            text = await existing
            if text:
                return {"text": text, "source": "cache", "provider": provider}
        except Exception as exc:
            # The in-flight call we were piggybacking on failed; we don't re-raise its
            # error (it already logged at its own site) but record why we fell through.
            log.debug("shared in-flight explain call failed: %s", exc)
        return {"source": "unavailable", "error": "concurrent call failed", "provider": provider}

    loop = asyncio.get_running_loop()
    fut: asyncio.Future[Optional[str]] = loop.create_future()
    _common.inflight_set(key, fut)
    try:
        prompt = _common.build_aircraft_prompt(payload)
        mod = _active_module()
        assert mod is not None   # is_configured guaranteed it
        result = await mod.generate(prompt)
        if result:
            result = _common.cap_response(result)
            _common.cache_set(key, result)
            fut.set_result(result)
            return {"text": result, "source": "ai", "provider": provider}
        fut.set_result(None)
        return {"source": "unavailable", "error": f"no response from {provider}", "provider": provider}
    except Exception as exc:
        if not fut.done():
            fut.set_exception(exc)
        log.info("ai.explain (%s) failed: %s", provider, exc)
        return {"source": "unavailable", "error": f"{type(exc).__name__}: {exc}", "provider": provider}
    finally:
        _common.inflight_pop(key)


def clear_cache() -> None:
    _common.cache_clear()
