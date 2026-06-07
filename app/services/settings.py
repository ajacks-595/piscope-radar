from __future__ import annotations

import json
import sqlite3
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional

# Cap value sizes so a single setting can't blow up memory or the DB.
_MAX_VALUE_BYTES = 256 * 1024

# In-memory cache of the full settings dict (with defaults merged). Refreshed on every
# writer (`set_many`/`set_one`) so readers don't hit the DB on every poll cycle.
_CACHE: Optional[dict[str, Any]] = None
_CACHE_VERSION = 0   # bumped on every write so callers can invalidate any derived state


DB_PATH = Path(__file__).resolve().parent.parent.parent / "piscope.db"


DEFAULTS: dict[str, Any] = {
    "tar1090_base_url": "",                # blank → use adsb.lol global mode
    "poll_interval": 2,
    "trail_length": 30,
    "show_ground": True,
    "theme": "radar",                      # AppTheme rawValue from Swift app
    "map_style": "automatic",
    "range_rings_enabled": True,
    "range_rings_nm": "50,100,150,200",
    "antenna_range_nm": 200,
    "fa_api_key": "",
    "fa_monthly_limit_cents": 500,
    "watchlist": "",
    "notify_military": True,
    "notify_emergency": True,
    "notify_watchlist": True,
    "receiver_lat": None,
    "receiver_lon": None,
    "feed_mode": "global",                 # "local" or "global"
    "global_center_lat": 51.5,             # London — a busy default; user should override.
    "global_center_lon": -0.1,
    "global_radius_nm": 250,
    "always_show_labels": False,
    "openaip_api_key": "",
    "openaip_overlay_enabled": False,
    # Audio alerts
    "audio_alerts_enabled": False,
    "audio_directional": True,
    # Follow mode
    "follow_selected": False,
    # Replay
    "replay_retention_minutes": 60,
    # Multi-feed: JSON array of {name, url, type} (type = "tar1090" only for now)
    "extra_feeds_json": "[]",
    # Some upstream APIs (planespotters at least) now reject User-Agents that don't include a
    # contact URL or email. The default points at the project page; users should override it
    # via Settings → General once they have their own deployment so unknown traffic doesn't
    # get blamed on this repo.
    "contact_url": "https://github.com/ajacks-595/piscope-radar",
    # Overlay toggles
    "weather_overlay_enabled": False,
    "day_night_enabled": False,
    # Webhook endpoints: list of {kind: "discord"|"slack"|"ntfy"|"generic", url, types: ["emergency","military","watchlist"]}
    "webhooks_json": "[]",
    # Saved views (camera positions): list of {name, lat, lon, zoom}
    "saved_views_json": "[]",
    # Trail colouring: "single" (theme accent) | "altitude" (band-colour gradient) | "speed"
    "trail_colour_mode": "single",
    # Whether trails should fade towards the tail (CPU-cheap; opacity per segment)
    "trail_fade": True,
    # Daily DB snapshot — disk path; empty disables.
    "daily_backup_dir": "",
    # Map label rendering: "off" / "callsign" / "full" (tar1090-style block).
    "map_label_mode": "off",
    # In "full" mode, labels are hidden below this zoom to reduce clutter.
    "label_full_min_zoom": 8,
    # How often to persist a snapshot row to feed_snapshots. 1 = every poll. Higher values
    # reduce SQLite write pressure on slow storage (SD cards). Default 5 = every 10 s at
    # the default 2 s poll interval.
    "snapshot_every_n_polls": 5,
    # Receiver-health watchdog: if every feed has been failing continuously for this many
    # minutes, fire a `feed_down` webhook (and a `feed_recovered` webhook when it comes back).
    # 0 disables the watchdog entirely. Useful for "is my Pi still alive?" Discord alerts.
    "watchdog_outage_minutes": 5,
    # ---- AI explain (iteration 5) ----
    # Ollama (or any compatible) server URL. Empty disables AI explanations entirely; the
    # frontend's "Explain this aircraft" button hides itself when this is blank. The Pi must
    # be able to reach this URL — typical setup is `http://10.0.0.x:11434` on a LAN host
    # with Ollama bound to 0.0.0.0.
    "ollama_url": "",
    # Model name as known to Ollama, e.g. `gemma4:latest`, `llama3.1:8b`, `qwen2.5:7b`.
    "ollama_model": "gemma4:latest",
    # Master enable switch — separate from `ollama_url` so a user can pause AI calls without
    # losing their URL config.
    "ollama_enabled": False,
    # How long Ollama keeps the model resident after a request. Default `0` releases it
    # immediately (matches what most third-party tools do). Override to e.g. "5m" to keep
    # the model warm if you expect many requests in a short window — accepts Ollama's usual
    # duration string format ("30s", "5m", "1h") or a number of seconds.
    "ollama_keep_alive": 0,
    # ---- Multi-provider AI (iteration 7) ----
    # Active provider name. One of "ollama" | "cloud_api" | "claude_cli" | "" (none).
    # Empty falls back to the legacy `ollama_enabled` flag so existing deployments
    # keep working without an explicit migration step.
    "ai_provider": "",
    # Cloud LLM provider (Anthropic / OpenAI / Google) — bring-your-own API key.
    "cloud_api_enabled": False,
    "cloud_api_vendor": "anthropic",       # "anthropic" | "openai" | "google"
    "cloud_api_key": "",
    # Empty model name falls back to a sensible per-vendor default in ai/cloud_api.py.
    "cloud_api_model": "",
    # Claude CLI provider — POSTs to a small shim daemon that wraps `claude -p`.
    # The shim lives on a LAN host that has Claude Code installed and authenticated.
    # See tools/claude-shim/ in the repo for a turnkey daemon + systemd unit.
    "claude_cli_enabled": False,
    "claude_cli_url": "",
    # Optional bearer token sent as `Authorization: Bearer <token>` to the shim.
    # Recommended even on a LAN — defence in depth against a compromised device.
    "claude_cli_token": "",
    # ---- AI chat follow-ups (iteration 8) ----
    # Maximum prior exchanges retained in /api/explain/followup prompts.
    # Each "turn" is one user message + one assistant reply. Higher values
    # cost more tokens per follow-up (cloud_api + claude_cli only — Ollama
    # is free). 5 keeps the prompt small while still feeling chatty.
    "ai_chat_max_turns": 5,
    # ---- Embed mode security (iteration 9.4) ----
    # Allowed parent origins for iframe embedding. Comma-separated list of
    # CSP frame-ancestors tokens — special keyword `'self'` (with quotes) or
    # an origin like `http://10.0.0.188:8090` or `https://home.example.com`.
    # Default `'self'` blocks all cross-origin embedding. Required for any
    # external dashboard (jacknet-home / SOC-dashboard / etc.) to iframe
    # PiScope via the iter-9.1 embed mode.
    "frame_ancestors": "'self'",
    # ---- Daily digest (iteration 5) ----
    "digest_enabled": True,
    # 24h "HH:MM" string. Cron fires once per local day at this time. Default 07:30 lands
    # in time for morning coffee on most schedules.
    "digest_local_time": "07:30",
    # Where to deliver. In-app is the no-setup baseline (renders under Stats → Today).
    # Webhooks reuse the existing fan-out subscribed to type "digest".
    # Email requires the SMTP_* settings below.
    "digest_deliver_in_app": True,
    "digest_deliver_webhook": False,
    "digest_deliver_email": False,
    # SMTP — only used when digest_deliver_email is on. Password lives in SECRET_KEYS so
    # it's never returned to the wire after being set.
    "smtp_host": "",
    "smtp_port": 587,
    "smtp_user": "",
    "smtp_pass": "",
    "smtp_from": "",
    "smtp_to": "",
    "smtp_use_starttls": True,
    # ---- Airport overlay (iteration 5) ----
    # Bundled large+medium airports rendered as a Leaflet layer. Zoom-aware: large always,
    # medium at zoom ≥ 8, IATA labels at ≥ 9. Default off so we don't add visual noise unsolicited.
    "airport_overlay_enabled": False,
    # Internal storage slot for the most-recent digest JSON — written by the digest service
    # and read by /api/digest. Keyed in DEFAULTS so set_many's whitelist accepts it; the value
    # is intentionally public (same data exposed via /api/digest), so no SECRET_KEYS entry.
    "digest_latest_json": None,
    # ---- Analytics (analytics feature, phase 1) ----
    # How long to keep the per-(hex, day) sighting ledger and hourly traffic stats.
    # ~1,000 unique aircraft/day ≈ 50 MB/year of sightings, so a year keeps the DB
    # comfortably bounded on SD storage while still allowing year-over-year views.
    # 0 = keep forever. Pruned alongside the snapshot prune in the feed loop.
    "analytics_retention_days": 365,
}

# Settings the user should never read back over the wire.
SECRET_KEYS = {"fa_api_key", "openaip_api_key", "smtp_pass", "cloud_api_key", "claude_cli_token"}


def _connect() -> sqlite3.Connection:
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(DB_PATH, isolation_level="DEFERRED", check_same_thread=True)
    conn.row_factory = sqlite3.Row
    # PRAGMA tuning for SD-card-friendly write throughput. These are safe defaults for a
    # local Pi: WAL gives parallel reads + faster writes; NORMAL trades a tiny crash window
    # (last few seconds of unflushed events) for ~5-10× fewer fsyncs.
    conn.execute("PRAGMA journal_mode = WAL")
    conn.execute("PRAGMA synchronous = NORMAL")
    conn.execute("PRAGMA temp_store = MEMORY")
    conn.execute("PRAGMA mmap_size = 67108864")           # 64 MB memory-mapped pages
    conn.execute("PRAGMA cache_size = -8192")             # 8 MB page cache
    conn.execute("PRAGMA busy_timeout = 5000")            # wait 5 s before raising on lock contention
    return conn


SCHEMA_VERSION = 3  # bump and add an `if v < N: …` block below whenever the schema changes


def _migrate(conn: sqlite3.Connection) -> None:
    cur = conn.execute("PRAGMA user_version").fetchone()
    v = cur[0] if cur else 0
    # v0 → v1: initial tables created below in CREATE TABLE IF NOT EXISTS statements.
    # v1 → v2: add bookmarks + records + close-pass log
    if v < 2:
        conn.execute(
            "CREATE TABLE IF NOT EXISTS bookmarks ("
            "hex TEXT PRIMARY KEY, "
            "label TEXT, "
            "callsign TEXT, registration TEXT, type_code TEXT, "
            "added_at REAL NOT NULL)"
        )
        # All-time bests for records panel — one row per category, replaced when beaten.
        conn.execute(
            "CREATE TABLE IF NOT EXISTS records ("
            "category TEXT PRIMARY KEY, "
            "hex TEXT, callsign TEXT, registration TEXT, type_code TEXT, "
            "value REAL, lat REAL, lon REAL, altitude INTEGER, "
            "recorded_at REAL NOT NULL)"
        )
        conn.execute("PRAGMA user_version = 2")
    # v2 → v3: analytics feature — per-(hex, UTC-day) sighting ledger + per-UTC-hour
    # traffic stats. Both are DERIVED tables fed incrementally by the feed loop
    # (services/analytics.py); raw ingestion and all pre-existing tables are untouched.
    if v < 3:
        conn.execute(
            "CREATE TABLE IF NOT EXISTS aircraft_sightings ("
            "hex TEXT NOT NULL, "
            "date TEXT NOT NULL, "                 # UTC YYYY-MM-DD
            "first_ts REAL NOT NULL, "
            "last_ts REAL NOT NULL, "
            "polls INTEGER NOT NULL DEFAULT 0, "   # poll cycles the hex appeared in (≈ dwell)
            "callsign TEXT, registration TEXT, type_code TEXT, "
            "military INTEGER NOT NULL DEFAULT 0, "
            "max_alt INTEGER, min_alt INTEGER, max_gs REAL, "
            "max_range_nm REAL, min_range_nm REAL, "
            "PRIMARY KEY(hex, date))"
        )
        # Window queries scan by date; hex lookups use the PK prefix.
        conn.execute("CREATE INDEX IF NOT EXISTS idx_sightings_date ON aircraft_sightings(date)")
        conn.execute(
            "CREATE TABLE IF NOT EXISTS hourly_stats ("
            "hour TEXT PRIMARY KEY, "              # UTC YYYY-MM-DDTHH
            "unique_aircraft INTEGER NOT NULL DEFAULT 0, "
            "obs INTEGER NOT NULL DEFAULT 0, "     # sum of per-poll contact counts
            "max_range_nm REAL NOT NULL DEFAULT 0, "
            "military INTEGER NOT NULL DEFAULT 0, "
            # Observation-weighted altitude-band counters (one increment per
            # aircraft per poll) — powers the altitude-distribution chart.
            "alt_ground INTEGER NOT NULL DEFAULT 0, "
            "alt_low INTEGER NOT NULL DEFAULT 0, "
            "alt_mid INTEGER NOT NULL DEFAULT 0, "
            "alt_high INTEGER NOT NULL DEFAULT 0, "
            "alt_very_high INTEGER NOT NULL DEFAULT 0)"
        )
        conn.execute("PRAGMA user_version = 3")
    conn.commit()


def _migrate_ai_provider() -> None:
    """One-time migration for iteration 7's multi-provider AI:
    if a deployment had `ollama_enabled=True` but no explicit `ai_provider`
    set, pin `ai_provider='ollama'` so the new UI dropdown reflects the
    current behaviour. Default-state installs (Ollama never enabled) keep
    `ai_provider=''` and stay in the no-AI state."""
    if (get("ai_provider") or "").strip():
        return
    if bool(get("ollama_enabled")):
        set_one("ai_provider", "ollama")


def init_db() -> None:
    with _connect() as conn:
        # Enable incremental auto-vacuum so the DB file shrinks after we prune old snapshots.
        # `auto_vacuum=INCREMENTAL` only takes effect on a fresh DB; we VACUUM here as a no-op
        # on already-existing DBs, but new installs will reclaim space automatically.
        try:
            current = conn.execute("PRAGMA auto_vacuum").fetchone()[0]
            if current == 0:
                conn.execute("PRAGMA auto_vacuum = INCREMENTAL")
                # The new auto_vacuum mode only sticks after a VACUUM. Run it once per startup if needed.
                conn.commit()
                conn.execute("VACUUM")
        except sqlite3.Error:
            pass
        conn.execute("CREATE TABLE IF NOT EXISTS settings (key TEXT PRIMARY KEY, value TEXT)")
        conn.execute(
            "CREATE TABLE IF NOT EXISTS fa_budget ("
            "month TEXT PRIMARY KEY, spent_cents INTEGER DEFAULT 0)"
        )
        # Event log: watchlist matches, military entries, emergency squawks. Searchable by time.
        conn.execute(
            "CREATE TABLE IF NOT EXISTS events ("
            "id INTEGER PRIMARY KEY AUTOINCREMENT, "
            "ts REAL NOT NULL, "
            "kind TEXT NOT NULL, "
            "hex TEXT, callsign TEXT, registration TEXT, "
            "distance_nm REAL, payload TEXT)"
        )
        conn.execute("CREATE INDEX IF NOT EXISTS idx_events_ts ON events(ts DESC)")
        conn.execute("CREATE INDEX IF NOT EXISTS idx_events_kind ON events(kind, ts DESC)")
        # Per-day aggregates. Updated incrementally by the feed loop.
        conn.execute(
            "CREATE TABLE IF NOT EXISTS daily_stats ("
            "date TEXT PRIMARY KEY, "
            "total_polls INTEGER DEFAULT 0, "
            "unique_aircraft INTEGER DEFAULT 0, "
            "max_range_nm REAL DEFAULT 0, "
            "emergencies INTEGER DEFAULT 0, "
            "military_seen INTEGER DEFAULT 0)"
        )
        # Snapshot ring buffer used by replay. The payload is a JSON dump of the WS snapshot.
        conn.execute(
            "CREATE TABLE IF NOT EXISTS feed_snapshots ("
            "ts REAL PRIMARY KEY, payload TEXT NOT NULL)"
        )
        conn.execute("CREATE INDEX IF NOT EXISTS idx_snapshots_ts ON feed_snapshots(ts DESC)")
        # Per-aircraft personal notes — small free-form text the user can attach to a hex.
        conn.execute(
            "CREATE TABLE IF NOT EXISTS aircraft_notes ("
            "hex TEXT PRIMARY KEY, note TEXT NOT NULL, updated_at REAL NOT NULL)"
        )
        # Type ledger — every unique ICAO type_code we've seen, plus first/last and count.
        # Powers the "rare type" alert and the type leaderboard.
        conn.execute(
            "CREATE TABLE IF NOT EXISTS seen_types ("
            "type_code TEXT PRIMARY KEY, "
            "first_seen REAL, last_seen REAL, sightings INTEGER DEFAULT 0)"
        )
        # 360-bin polar coverage diagram: best (max) range observed per integer-degree bearing.
        conn.execute(
            "CREATE TABLE IF NOT EXISTS polar_coverage ("
            "bearing INTEGER PRIMARY KEY, max_nm REAL NOT NULL DEFAULT 0, "
            "last_hex TEXT, last_seen REAL)"
        )
        # Coarse position heatmap for the activity layer. Lat/lon are bucketed to 0.05° (~3 nm)
        # and we just keep a hit counter — perfect for Leaflet.heat.
        conn.execute(
            "CREATE TABLE IF NOT EXISTS position_heatmap ("
            "lat_bucket INTEGER NOT NULL, lon_bucket INTEGER NOT NULL, "
            "hits INTEGER NOT NULL DEFAULT 0, "
            "PRIMARY KEY(lat_bucket, lon_bucket))"
        )
        # Run migrations after the base CREATE TABLEs so we don't fight CREATE TABLE IF NOT EXISTS.
        _migrate(conn)
        conn.commit()
    # Settings-level migrations live outside the open connection because they go through
    # `set_one` (which manages its own connection + cache invalidation).
    _migrate_ai_provider()


def _coerce(key: str, raw: Optional[str]) -> Any:
    if raw is None:
        return DEFAULTS.get(key)
    try:
        return json.loads(raw)
    except (json.JSONDecodeError, TypeError):
        return raw


from contextlib import contextmanager


@contextmanager
def batch():
    """Open a single SQLite connection for the duration of a poll cycle. Helpers given this
    connection write without their own commit; the context manager commits once on exit,
    turning ~thousands of fsyncs/min into ~tens/min on a Pi."""
    conn = _connect()
    try:
        yield conn
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def _populate_cache() -> dict[str, Any]:
    """Read every settings row from disk and rebuild the in-memory cache. Holds raw values
    (no redaction) so writers can compare deltas without hitting disk again."""
    global _CACHE
    with _connect() as conn:
        rows = conn.execute("SELECT key, value FROM settings").fetchall()
    stored = {r["key"]: _coerce(r["key"], r["value"]) for r in rows}
    _CACHE = {**DEFAULTS, **stored}
    return _CACHE


def cache_version() -> int:
    """Monotonic counter — callers (e.g. the feed loop) can stash this and re-read settings
    only when it changes. Saves a hash map copy per poll."""
    return _CACHE_VERSION


def reload_cache() -> None:
    """Discard and rebuild the in-memory settings cache from disk, bumping the version.
    Needed after the DB file is swapped underneath us (import/restore): otherwise readers
    keep serving the pre-restore values and the feed loop never notices the new settings."""
    global _CACHE, _CACHE_VERSION
    _CACHE = None
    _CACHE_VERSION += 1
    _populate_cache()


def get(key: str) -> Any:
    if _CACHE is None:
        _populate_cache()
    return _CACHE.get(key, DEFAULTS.get(key))


def get_all(redact: bool = True) -> dict[str, Any]:
    if _CACHE is None:
        _populate_cache()
    merged = dict(_CACHE)
    if redact:
        for k in SECRET_KEYS:
            merged[k] = "***" if merged.get(k) else ""
        # Derived flags the UI uses to know whether secret keys are stored without revealing them.
        merged["fa_api_key_set"] = bool(_CACHE.get("fa_api_key"))
        merged["openaip_api_key_set"] = bool(_CACHE.get("openaip_api_key"))
        merged["cloud_api_key_set"] = bool(_CACHE.get("cloud_api_key"))
        merged["claude_cli_token_set"] = bool(_CACHE.get("claude_cli_token"))
        # The digest blob is large (~10 KB) and the frontend pulls it from /api/digest
        # when it actually needs it — strip it from the generic settings response to keep
        # the payload lean.
        merged.pop("digest_latest_json", None)
    return merged


def set_many(values: dict[str, Any]) -> None:
    """Persist a batch of settings updates. Only keys present in DEFAULTS are accepted —
    this whitelists what callers can write so a hostile (or buggy) client cannot pollute the
    settings table with arbitrary keys or shadow internal config."""
    global _CACHE_VERSION
    with _connect() as conn:
        for k, v in values.items():
            if k not in DEFAULTS:
                continue  # whitelist: silently ignore unknown keys
            if k in SECRET_KEYS and (v == "***" or v is None):
                # don't overwrite a stored secret with the redaction placeholder
                continue
            conn.execute(
                "INSERT INTO settings(key, value) VALUES(?, ?) "
                "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
                (k, json.dumps(v)),
            )
        conn.commit()
    # Rebuild the in-memory cache so subsequent reads see the new values without hitting disk.
    _populate_cache()
    _CACHE_VERSION += 1


def set_one(key: str, value: Any) -> None:
    set_many({key: value})


def redacted_sql_dump(db_path: Path) -> str:
    """Return a full SQL dump of the DB with every SECRET_KEYS value blanked.

    Shared by the manual /api/export AND the automated daily backup so neither writes
    plaintext API keys / SMTP password / provider tokens into a zip that may be emailed
    or cloud-stored — and so the two paths can never drift. Runs against a throwaway
    in-memory copy via SQLite's online backup, so the live DB is never modified."""
    with sqlite3.connect(db_path) as src, sqlite3.connect(":memory:") as mem:
        src.backup(mem)
        secret_keys = sorted(SECRET_KEYS)
        if secret_keys:
            # Values are JSON-encoded in the settings table; '""' is an empty JSON string.
            placeholders = ",".join("?" * len(secret_keys))
            mem.execute(
                f"UPDATE settings SET value = '\"\"' WHERE key IN ({placeholders})",
                tuple(secret_keys),
            )
            mem.commit()
        return "\n".join(mem.iterdump())


# --- FlightAware monthly budget bucket ---------------------------------------


def _current_month() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m")


def fa_budget_status() -> dict[str, Any]:
    month = _current_month()
    with _connect() as conn:
        row = conn.execute("SELECT spent_cents FROM fa_budget WHERE month = ?", (month,)).fetchone()
    spent = row["spent_cents"] if row else 0
    limit = int(get("fa_monthly_limit_cents") or 0)
    return {
        "month": month,
        "spent_cents": spent,
        "limit_cents": limit,
        "remaining_cents": max(0, limit - spent) if limit > 0 else None,
        "over_budget": limit > 0 and spent >= limit,
    }


def fa_record_call(cost_cents: int = 5) -> dict[str, Any]:
    month = _current_month()
    with _connect() as conn:
        conn.execute(
            "INSERT INTO fa_budget(month, spent_cents) VALUES(?, ?) "
            "ON CONFLICT(month) DO UPDATE SET spent_cents = spent_cents + excluded.spent_cents",
            (month, cost_cents),
        )
        conn.commit()
    return fa_budget_status()
