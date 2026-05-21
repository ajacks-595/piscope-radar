"""Provider-agnostic AI helpers.

Holds everything that should NOT be re-implemented per provider: the prompt
builder for `/api/explain`, input sanitization, the LRU response cache, and
the in-flight de-duplication map. Providers consume `build_aircraft_prompt`
+ `aircraft_cache_key`; the public `explain` orchestrator in `ai/__init__.py`
handles caching and dedup uniformly.

Security posture
----------------
* Only structured, whitelisted enrichment fields touch any prompt.
* `_sanitize` rejects anything outside printable ASCII + a per-field regex.
* `_safe_text` strips control chars and length-caps free-form strings (names,
  cities).
* `_bounded_int` / `_bounded_float` clamp numerics so a malformed feed value
  can never insert "altitude: 999999999" into the prompt.
"""
from __future__ import annotations

import asyncio
import hashlib
import re
from typing import Any, Optional


# Whitelist for any string we let into a prompt.
_PRINTABLE_RE = re.compile(r"[^\x20-\x7E]")
_HEX_RE = re.compile(r"^[0-9A-F]{6}$")
_CALLSIGN_RE = re.compile(r"^[A-Z0-9]{2,10}$")
_TYPE_RE = re.compile(r"^[A-Z0-9]{2,6}$")
_REG_RE = re.compile(r"^[A-Z0-9-]{3,10}$")

# Hard ceiling on any provider response we'll cache or return — guards against
# a misbehaving model eating Pi RAM.
MAX_RESPONSE_CHARS = 4000


def _sanitize(value: Any, pattern: re.Pattern) -> str:
    if not isinstance(value, str):
        return ""
    s = _PRINTABLE_RE.sub("", value).strip()
    if not s:
        return ""
    candidate = s.upper() if pattern is not _PRINTABLE_RE else s
    return candidate if pattern.fullmatch(candidate) else ""


def _safe_text(value: Any, max_len: int = 80) -> str:
    if not isinstance(value, str):
        return ""
    return _PRINTABLE_RE.sub("", value).strip()[:max_len]


def _bounded_int(value: Any, lo: int, hi: int) -> Optional[int]:
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


def _route_signature(payload: dict[str, Any]) -> str:
    parts = [
        _safe_text(payload.get("origin_icao")),
        _safe_text(payload.get("destination_icao")),
        _safe_text(payload.get("airline_name")),
    ]
    h = hashlib.sha1("|".join(parts).encode("utf-8"), usedforsecurity=False)
    return h.hexdigest()[:12]


# Bump whenever the prompt template changes meaningfully — old cached responses
# for the same aircraft are otherwise served forever despite being generated
# from a different prompt. v2 = iteration 6 (two paragraphs + live state +
# notable fact). v3 = iteration 7 (multi-provider — provider name now in key).
_PROMPT_VERSION = "v3"


def aircraft_cache_key(payload: dict[str, Any], provider: str) -> str:
    """Cache key for /api/explain responses.

    Includes the provider name so that switching providers (e.g. Ollama →
    Claude CLI) doesn't serve a stale Ollama response for the new provider's
    request. Different models produce different prose for the same facts.
    """
    hex_id = _sanitize(payload.get("hex"), _HEX_RE)
    callsign = _sanitize(payload.get("callsign"), _CALLSIGN_RE)
    type_code = _sanitize(payload.get("type_code"), _TYPE_RE)
    return f"{_PROMPT_VERSION}|{provider}|{hex_id}|{callsign}|{type_code}|{_route_signature(payload)}"


def build_aircraft_prompt(payload: dict[str, Any]) -> str:
    """Assemble the /api/explain prompt. Every field is labelled and validated;
    we deliberately do NOT mix user-provided strings in unstructured ways."""
    lines: list[str] = [
        "You are a friendly aviation enthusiast writing for someone with a real-time ADS-B radar.",
        "Write TWO short paragraphs about the aircraft below, totalling under 110 words.",
        "Paragraph 1: state what the aircraft is, who operates it, and what it's doing right now (use ONLY the live fields provided — altitude/heading/speed/origin/destination). Don't invent.",
        "Paragraph 2: a single interesting historical or technical fact about this aircraft TYPE that the reader is likely not to know — first flight year, engine variant, design quirk, role. ONE sentence.",
        "Plain prose, no bullets, no markdown, no headers. If a field is missing, skip it silently.",
        "",
    ]
    lines.extend(_aircraft_context_block(payload))
    lines.append("")
    lines.append("Brief:")
    return "\n".join(lines)


# Hard cap on a single conversation turn we'll stitch into a follow-up prompt.
# Long assistant turns are normally already capped by `cap_response` on the way
# back from /api/explain; this is a defence-in-depth ceiling for anything the
# frontend ever sends us.
MAX_TURN_CHARS = 2000
MAX_QUESTION_CHARS = 500


def sanitize_turn_text(value: Any) -> str:
    """Loose sanitiser for chat content — strip control chars, length-cap, no
    regex match. Used for both prior turns and the new question. Doesn't try
    to filter the content beyond making it safe to embed in a prompt."""
    if not isinstance(value, str):
        return ""
    return _PRINTABLE_RE.sub("", value).strip()[:MAX_TURN_CHARS]


def _aircraft_context_block(payload: dict[str, Any]) -> list[str]:
    """Return the labelled fact block used by both build_aircraft_prompt and
    build_followup_prompt. Identifiers + live state. Empty/invalid fields are
    silently omitted; never inserts raw user-supplied strings."""
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

    lines: list[str] = [
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
    return lines


def build_followup_prompt(
    payload: dict[str, Any],
    history: list[dict[str, str]],
    question: str,
    max_turns: int = 5,
) -> str:
    """Assemble a stitched prompt for /api/explain/followup.

    `history` is a list of {"role": "user"|"assistant", "content": "..."}
    in chronological order — typically the initial brief is the first
    "assistant" entry, then alternating user/assistant pairs. We keep only
    the last `max_turns * 2` entries (one turn = user + assistant pair) so
    the prompt size stays bounded even on long chats.
    """
    # Truncate to most-recent `max_turns` exchanges (user+assistant = 2 entries).
    bounded = history[-max(1, max_turns) * 2:] if history else []

    lines: list[str] = [
        "You are a friendly aviation enthusiast continuing a conversation about an aircraft visible on a real-time ADS-B radar. The user has already read your initial brief; they are now asking follow-up questions.",
        "Style: 1-3 sentences per answer. Plain prose, no markdown, no bullets, no headers. Conversational and specific. Stick to facts about the aircraft type, operator, route, or general aviation knowledge.",
        "Don't restate facts the user already has from the brief. Don't refuse to discuss the aircraft — its identifiers and current state are public ADS-B data. If you genuinely don't know, say so in one sentence.",
        "",
    ]
    lines.extend(_aircraft_context_block(payload))
    lines.append("")
    lines.append("── Conversation so far ──")
    for entry in bounded:
        role = (entry.get("role") or "").strip().lower()
        content = sanitize_turn_text(entry.get("content"))
        if not content:
            continue
        if role == "assistant":
            lines.append(f"Assistant: {content}")
        elif role == "user":
            lines.append(f"User: {content}")
    lines.append("")
    lines.append(f"User: {sanitize_turn_text(question)[:MAX_QUESTION_CHARS]}")
    lines.append("Assistant:")
    return "\n".join(lines)


def cap_response(text: str) -> str:
    """Trim model output to MAX_RESPONSE_CHARS at a word boundary."""
    if len(text) <= MAX_RESPONSE_CHARS:
        return text
    return text[:MAX_RESPONSE_CHARS].rsplit(" ", 1)[0] + "…"


# --- LRU + in-flight dedup --------------------------------------------------
#
# Kept here rather than in _http.py because these are AI-specific lifecycles
# (cleared on settings change, etc.) and shouldn't share the HTTP-tile cache.


class _LRU:
    """Tiny LRU on top of dict — preserves insertion order, evicts oldest."""
    def __init__(self, max_size: int = 256) -> None:
        self._d: dict[str, str] = {}
        self._max = max_size

    def get(self, key: str) -> Optional[str]:
        v = self._d.get(key)
        if v is None:
            return None
        # Refresh recency by reinserting.
        self._d.pop(key, None)
        self._d[key] = v
        return v

    def set(self, key: str, value: str) -> None:
        if key in self._d:
            self._d.pop(key, None)
        self._d[key] = value
        while len(self._d) > self._max:
            self._d.pop(next(iter(self._d)))

    def clear(self) -> None:
        self._d.clear()


_CACHE = _LRU(max_size=256)
_INFLIGHT: dict[str, asyncio.Future[Optional[str]]] = {}


def cache_get(key: str) -> Optional[str]:
    return _CACHE.get(key)


def cache_set(key: str, value: str) -> None:
    _CACHE.set(key, value)


def cache_clear() -> None:
    _CACHE.clear()


def inflight_get(key: str) -> Optional[asyncio.Future[Optional[str]]]:
    return _INFLIGHT.get(key)


def inflight_set(key: str, fut: asyncio.Future[Optional[str]]) -> None:
    _INFLIGHT[key] = fut


def inflight_pop(key: str) -> None:
    _INFLIGHT.pop(key, None)
