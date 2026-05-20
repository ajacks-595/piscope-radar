"""Ollama-compatible AI explain backend.

Generates a short natural-language brief about an aircraft based on its enrichment data.
Designed for a LAN-local Ollama instance, but works against anything that speaks the
Ollama HTTP API (LiteLLM, llama.cpp's server, etc.).

Security posture
----------------
* The only inputs that touch the model prompt are *structured* fields that we already
  enrich (hex, callsign, type code, registration, route metadata). They're whitelisted
  through a strict regex; anything that fails validation is dropped before prompt assembly.
  ADS-B-sourced strings can in principle contain unusual bytes; we never paste raw user
  text into the prompt.
* The endpoint URL is taken from settings (admin-only — anyone who can reach this Pi's
  web UI already has full admin access). We do require `http(s)://` and reject everything
  else, which blocks file://, gopher://, etc.
* Response size is capped (~2 KB) so a misbehaving model can't blow up the cache or eat
  the Pi's RAM. Cache stores SUCCESSFUL responses only — never errors, so a transient
  failure doesn't get pinned forever.

Performance posture
-------------------
* Shared httpx.AsyncClient via `_http.get_client()` — keep-alive pool reused across calls.
* LRUCache keyed on `(hex, type_code, callsign, route_signature)`; max 256 entries (~500 KB).
* In-flight de-dup: if two concurrent requests want the same key, the second awaits the
  first's result rather than firing a parallel Ollama call.
"""
from __future__ import annotations

import asyncio
import hashlib
import logging
import re
from typing import Any, Optional

from . import settings as settings_store
from ._http import LRUCache, get_client


log = logging.getLogger("piscope.ollama")

_CACHE = LRUCache(max_size=256)
# Map of cache-key → asyncio.Future so concurrent callers share one upstream call.
_INFLIGHT: dict[str, asyncio.Future[Optional[str]]] = {}

# Strict whitelist for any string we let into the prompt. Anything outside this range
# (control characters, exotic unicode) gets stripped before assembly.
_PRINTABLE_RE = re.compile(r"[^\x20-\x7E]")
_HEX_RE = re.compile(r"^[0-9A-F]{6}$")    # case-normalised to uppercase by _sanitize
_CALLSIGN_RE = re.compile(r"^[A-Z0-9]{2,10}$")
_TYPE_RE = re.compile(r"^[A-Z0-9]{2,6}$")
_REG_RE = re.compile(r"^[A-Z0-9-]{3,10}$")

# Response cap — Gemma at our prompt sizes routinely produces 100–500 tokens. 4000 chars
# is enough headroom for ~700 tokens of English while bounding worst-case memory.
_MAX_RESPONSE_CHARS = 4000


def _sanitize(value: Any, pattern: re.Pattern) -> str:
    """Return value as a printable ASCII string if it matches the whitelist, else empty.
    Defence in depth — even if upstream enrichment serves something weird, we don't paste
    raw bytes into the prompt."""
    if not isinstance(value, str):
        return ""
    s = _PRINTABLE_RE.sub("", value).strip()
    if not s:
        return ""
    # Most callers pass uppercase identifiers (callsign/type/reg/hex). Apply pattern to
    # the upper-cased form so input is case-tolerant on the way in.
    candidate = s.upper() if pattern is not _PRINTABLE_RE else s
    return candidate if pattern.fullmatch(candidate) else ""


def _safe_text(value: Any, max_len: int = 80) -> str:
    """Looser sanitiser for names/cities — strip control chars, length-cap, no regex match."""
    if not isinstance(value, str):
        return ""
    return _PRINTABLE_RE.sub("", value).strip()[:max_len]


def _route_signature(payload: dict[str, Any]) -> str:
    """A stable hash of just the route-defining fields, so cache hits don't depend on
    fields that change tick-by-tick (lat/lon/altitude/heading)."""
    parts = [
        _safe_text(payload.get("origin_icao")),
        _safe_text(payload.get("destination_icao")),
        _safe_text(payload.get("airline_name")),
    ]
    h = hashlib.sha1("|".join(parts).encode("utf-8"), usedforsecurity=False)
    return h.hexdigest()[:12]


# Bump whenever the prompt template changes meaningfully — old cached responses for the
# same aircraft are otherwise served forever even though they were generated from a
# different prompt. v2 = iteration 6 (two paragraphs + live state + notable fact).
_PROMPT_VERSION = "v2"


def _cache_key(payload: dict[str, Any]) -> str:
    hex_id = _sanitize(payload.get("hex"), _HEX_RE)
    callsign = _sanitize(payload.get("callsign"), _CALLSIGN_RE)
    type_code = _sanitize(payload.get("type_code"), _TYPE_RE)
    return f"{_PROMPT_VERSION}|{hex_id}|{callsign}|{type_code}|{_route_signature(payload)}"


def _bounded_int(value: Any, lo: int, hi: int) -> Optional[int]:
    """Clamp + reject NaN/strings/extremes. Used for numerical fields so the prompt can't
    contain "altitude: 999999999" or other malformed inputs."""
    try:
        n = int(float(value))
    except (TypeError, ValueError):
        return None
    if not (lo <= n <= hi):
        return None
    return n


def _bounded_float(value: Any, lo: float, hi: float) -> Optional[float]:
    try:
        n = float(value)
    except (TypeError, ValueError):
        return None
    if not (lo <= n <= hi):
        return None
    return n


def _build_prompt(payload: dict[str, Any]) -> str:
    """Assemble a short, fact-only prompt. We deliberately do NOT mix user-provided strings
    in unstructured ways — every field is labelled and validated."""
    hex_id = _sanitize(payload.get("hex"), _HEX_RE) or "(unknown)"
    callsign = _sanitize(payload.get("callsign"), _CALLSIGN_RE) or "(unknown)"
    type_code = _sanitize(payload.get("type_code"), _TYPE_RE) or "(unknown)"
    reg = _sanitize(payload.get("registration"), _REG_RE) or "(unknown)"
    airline = _safe_text(payload.get("airline_name"), 60)
    origin = _safe_text(payload.get("origin_name"), 60)
    origin_mun = _safe_text(payload.get("origin_municipality"), 60)
    origin_iata = _safe_text(payload.get("origin_iata"), 8)
    origin_country = _safe_text(payload.get("origin_country_iso"), 4)
    dest = _safe_text(payload.get("destination_name"), 60)
    dest_mun = _safe_text(payload.get("destination_municipality"), 60)
    dest_iata = _safe_text(payload.get("destination_iata"), 8)
    dest_country = _safe_text(payload.get("destination_country_iso"), 4)
    operator = _safe_text(payload.get("operator"), 60)
    military = bool(payload.get("military"))
    on_ground = bool(payload.get("on_ground"))
    watchlist = bool(payload.get("watchlist_match"))
    squawk = _safe_text(payload.get("squawk"), 4)
    altitude = _bounded_int(payload.get("altitude_baro"), -2000, 65000)
    speed = _bounded_int(payload.get("ground_speed"), 0, 1500)
    vert_rate = _bounded_int(payload.get("vertical_rate"), -8000, 8000)
    distance_nm = _bounded_float(payload.get("distance_nm"), 0.0, 500.0)
    heading = _bounded_int(payload.get("heading"), 0, 360)
    is_emergency = bool(payload.get("is_emergency_squawk"))

    lines = [
        # Stronger prompt: two short paragraphs, the second with a 'notable fact' framing.
        # `factual` framing keeps the model from over-generating (gemma4 8B is prone to it).
        "You are a friendly aviation enthusiast writing for someone with a real-time ADS-B radar.",
        "Write TWO short paragraphs about the aircraft below, totalling under 110 words.",
        "Paragraph 1: state what the aircraft is, who operates it, and what it's doing right now (use ONLY the live fields provided — altitude/heading/speed/origin/destination). Don't invent.",
        "Paragraph 2: a single interesting historical or technical fact about this aircraft TYPE that the reader is likely not to know — first flight year, engine variant, design quirk, role. ONE sentence.",
        "Plain prose, no bullets, no markdown, no headers. If a field is missing, skip it silently.",
        "",
        "── Identifiers ──",
        f"ICAO24 hex: {hex_id}",
        f"Callsign: {callsign}",
        f"Type code: {type_code}",
        f"Registration: {reg}",
    ]
    if airline or operator:
        lines.append(f"Operator: {airline or operator}")
    if origin or origin_iata:
        bits = []
        if origin_iata: bits.append(origin_iata)
        if origin: bits.append(origin)
        if origin_mun: bits.append(origin_mun)
        if origin_country: bits.append(origin_country)
        lines.append(f"Origin: {', '.join(bits)}")
    if dest or dest_iata:
        bits = []
        if dest_iata: bits.append(dest_iata)
        if dest: bits.append(dest)
        if dest_mun: bits.append(dest_mun)
        if dest_country: bits.append(dest_country)
        lines.append(f"Destination: {', '.join(bits)}")
    lines.append("")
    lines.append("── Live state ──")
    if on_ground:
        lines.append("On ground: yes")
    if altitude is not None:
        lines.append(f"Altitude: {altitude} ft")
    if speed is not None:
        lines.append(f"Ground speed: {speed} knots")
    if heading is not None:
        lines.append(f"Heading: {heading}°")
    if vert_rate is not None:
        lines.append(f"Vertical rate: {vert_rate} ft/min ({'climbing' if vert_rate > 64 else 'descending' if vert_rate < -64 else 'level'})")
    if distance_nm is not None:
        lines.append(f"Distance from receiver: {distance_nm:.0f} nm")
    if squawk:
        lines.append(f"Transponder squawk: {squawk}")
    if military:
        lines.append("Military aircraft: yes")
    if is_emergency:
        lines.append("Emergency squawk active: yes (7500/7600/7700)")
    if watchlist:
        lines.append("This aircraft is on the user's watchlist.")
    lines.append("")
    lines.append("Brief:")
    return "\n".join(lines)


def is_configured() -> bool:
    """Cheap predicate the API layer uses to decide whether to expose the explain endpoint."""
    url = (settings_store.get("ollama_url") or "").strip()
    enabled = bool(settings_store.get("ollama_enabled"))
    return enabled and url.startswith(("http://", "https://"))


async def ping() -> dict[str, Any]:
    """Test the configured Ollama server. Used by Settings → AI "Test connection" button."""
    url = (settings_store.get("ollama_url") or "").strip()
    model = (settings_store.get("ollama_model") or "").strip()
    if not url.startswith(("http://", "https://")):
        return {"ok": False, "error": "URL must start with http:// or https://"}
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


async def explain(payload: dict[str, Any]) -> dict[str, Any]:
    """Produce a one-paragraph natural-language brief about an aircraft.

    Returns one of:
        {"text": "...", "source": "ai"}        — fresh AI response
        {"text": "...", "source": "cache"}     — served from local cache
        {"source": "unavailable", "error": "..."} — Ollama is offline / disabled
    """
    if not is_configured():
        return {"source": "unavailable", "error": "Ollama not configured"}

    key = _cache_key(payload)
    cached = _CACHE.get(key)
    if cached is not None:
        return {"text": cached, "source": "cache"}

    # In-flight de-dup. The second-arriving caller waits for the first's result instead
    # of firing a duplicate request — important if the user double-clicks the button.
    existing = _INFLIGHT.get(key)
    if existing is not None:
        try:
            text = await existing
            if text:
                return {"text": text, "source": "cache"}
        except Exception:
            pass  # fall through and try ourselves
        return {"source": "unavailable", "error": "concurrent call failed"}

    loop = asyncio.get_running_loop()
    fut: asyncio.Future[Optional[str]] = loop.create_future()
    _INFLIGHT[key] = fut
    try:
        result = await _generate(payload)
        if result:
            _CACHE.set(key, result)
            fut.set_result(result)
            return {"text": result, "source": "ai"}
        fut.set_result(None)
        return {"source": "unavailable", "error": "no response from Ollama"}
    except Exception as exc:
        if not fut.done():
            fut.set_exception(exc)
        log.info("ollama explain failed: %s", exc)
        return {"source": "unavailable", "error": f"{type(exc).__name__}: {exc}"}
    finally:
        _INFLIGHT.pop(key, None)


async def _generate(payload: dict[str, Any]) -> Optional[str]:
    """Single-shot Ollama call. Uses /api/chat so the model's own chat template is applied
    correctly — required for Gemma 4 family, harmless for older models. We disable the
    `think` channel so reasoning models don't burn the entire token budget thinking and
    return an empty content field."""
    url = (settings_store.get("ollama_url") or "").strip().rstrip("/")
    model = (settings_store.get("ollama_model") or "gemma4:latest").strip()
    prompt = _build_prompt(payload)
    body = {
        "model": model,
        "messages": [{"role": "user", "content": prompt}],
        "stream": False,
        # `think: false` keeps reasoning models from burning the predict budget on
        # internal monologue. Older models that don't understand this key ignore it.
        "think": False,
        # Match the spin-up/spin-down pattern most CLI users expect. Default Ollama keeps
        # the model resident for 5 min after the last call, which on a shared Mac/NAS hogs
        # 5+ GB of RAM unnecessarily. `0` (or "0s") forces an unload as soon as the response
        # is delivered. The setting is overridable for users who'd rather pay the steady
        # RAM cost in exchange for ~1-second cold-load latency on subsequent calls.
        "keep_alive": settings_store.get("ollama_keep_alive") or 0,
        "options": {
            "temperature": 0.5,   # slightly looser — the "notable fact" sentence is helped by it
            "top_p": 0.9,
            "num_predict": 360,   # ~270 words upper bound, matches 110-word target with headroom
            "stop": ["\n\n\n"],
        },
    }
    client = await get_client()
    # 25s total — generous because cold-loaded 8B model on first call can take a while.
    r = await client.post(f"{url}/api/chat", json=body, timeout=25.0)
    if r.status_code != 200:
        log.info("ollama returned %d: %s", r.status_code, r.text[:200])
        return None
    data = r.json()
    message = data.get("message") or {}
    text = (message.get("content") or "").strip()
    if not text:
        # Some models drop content into "thinking" if think:false isn't honoured — fall back.
        thinking = (message.get("thinking") or "").strip()
        if thinking:
            text = thinking
    if not text:
        return None
    if len(text) > _MAX_RESPONSE_CHARS:
        text = text[:_MAX_RESPONSE_CHARS].rsplit(" ", 1)[0] + "…"
    return text


def clear_cache() -> None:
    """Used when the model/URL settings change — old cached explanations may not match
    the new model's style, and the user just paid for the change."""
    _CACHE.clear()
