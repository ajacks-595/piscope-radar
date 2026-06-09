"""Settings store: round-trip, whitelist, secret redaction, migration, FA budget."""
from __future__ import annotations

import sys
import pathlib

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))


def test_set_get_roundtrip(temp_db):
    from app.services import settings as s
    s.set_many({"theme": "nord", "poll_interval": 3})
    assert s.get("theme") == "nord"
    assert s.get("poll_interval") == 3


def test_unknown_keys_dropped(temp_db):
    from app.services import settings as s
    s.set_many({"theme": "radar", "totally_made_up_key": 42})
    assert s.get("theme") == "radar"
    assert "totally_made_up_key" not in s.get_all(redact=False)


def test_secret_redaction_and_set_flag(temp_db):
    from app.services import settings as s
    s.set_one("smtp_pass", "hunter2")
    # raw read keeps the value (the server needs it)
    assert s.get("smtp_pass") == "hunter2"
    # redacted read hides it + exposes a _set flag
    red = s.get_all(redact=True)
    assert red["smtp_pass"] == "***"
    # redaction placeholder is never written back over the real value
    s.set_many({"smtp_pass": "***"})
    assert s.get("smtp_pass") == "hunter2"


def test_cache_version_bumps_on_write(temp_db):
    from app.services import settings as s
    v0 = s.cache_version()
    s.set_one("theme", "terminal")
    assert s.cache_version() > v0


def test_migrate_ai_provider_pins_ollama(temp_db):
    from app.services import settings as s
    # Simulate a pre-iter-7 deployment: ollama enabled, no ai_provider chosen.
    s.set_one("ollama_enabled", True)
    s.set_one("ai_provider", "")
    s._CACHE = None
    s.init_db()   # runs _migrate_ai_provider
    assert s.get("ai_provider") == "ollama"


def test_migrate_ai_provider_leaves_fresh_install_blank(temp_db):
    from app.services import settings as s
    # Fresh temp_db: ollama never enabled → ai_provider stays empty.
    assert s.get("ai_provider") == ""


def test_fa_budget_tracking(temp_db):
    from app.services import settings as s
    status0 = s.fa_budget_status()
    assert status0["spent_cents"] == 0
    s.fa_record_call(5)
    s.fa_record_call(5)
    assert s.fa_budget_status()["spent_cents"] == 10


# --- value validation (iteration 13) ------------------------------------------


def test_invalid_numeric_value_rejected(temp_db):
    from app.services import settings as s
    # A non-numeric poll_interval used to be stored verbatim and later killed
    # the feed poll task when int() raised outside its try/except.
    s.set_many({"poll_interval": "abc"})
    assert s.get("poll_interval") == 2          # default survives
    s.set_many({"fa_monthly_limit_cents": {"weird": True}})
    assert s.get("fa_monthly_limit_cents") == 500


def test_numeric_values_clamped_not_dropped(temp_db):
    from app.services import settings as s
    s.set_many({"poll_interval": 0, "trail_length": 99999, "smtp_port": "8025"})
    assert s.get("poll_interval") == 1
    assert s.get("trail_length") == 500
    assert s.get("smtp_port") == 8025


def test_bool_and_enum_coercion(temp_db):
    from app.services import settings as s
    s.set_many({"digest_enabled": "false", "notify_military": 1,
                "ai_provider": "OLLAMA", "feed_mode": "bogus"})
    assert s.get("digest_enabled") is False
    assert s.get("notify_military") is True
    assert s.get("ai_provider") == "ollama"     # case-normalised
    assert s.get("feed_mode") == "global"       # invalid enum rejected → default


def test_hhmm_validation(temp_db):
    from app.services import settings as s
    s.set_many({"digest_local_time": "25:99"})
    assert s.get("digest_local_time") == "07:30"
    s.set_many({"digest_local_time": "8:05"})
    assert s.get("digest_local_time") == "08:05"   # canonicalised


def test_frame_ancestors_header_safe(temp_db):
    from app.services import settings as s
    # Newlines / control chars must never reach the CSP response header — a
    # stored CRLF would 500 every page load at the header-encoding layer.
    s.set_many({"frame_ancestors": "'self'\r\nSet-Cookie: x=y"})
    assert s.get("frame_ancestors") == "'self'"     # rejected → default
    s.set_many({"frame_ancestors": "'self', http://10.0.0.188:8090"})
    assert s.get("frame_ancestors") == "'self', http://10.0.0.188:8090"


def test_oversized_value_truncated_by_validator(temp_db):
    from app.services import settings as s
    # watchlist's _v_str(8192) validator caps length, so a huge value is truncated
    # to the cap rather than stored whole (the _MAX_VALUE_BYTES backstop in set_many
    # only fires for the rare validator-less key, e.g. the internal digest blob).
    s.set_many({"watchlist": "A" * (300 * 1024)})
    assert len(s.get("watchlist")) == 8192


def test_control_chars_stripped_from_strings(temp_db):
    from app.services import settings as s
    s.set_many({"ollama_model": "gem\x00ma4:\x1blatest"})
    assert s.get("ollama_model") == "gemma4:latest"


def test_stored_garbage_cleaned_on_cache_load(temp_db):
    import json
    import sqlite3
    from app.services import settings as s
    # Simulate a pre-validator DB: write garbage straight into the table.
    with sqlite3.connect(temp_db) as conn:
        conn.execute(
            "INSERT INTO settings(key, value) VALUES('poll_interval', ?) "
            "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
            (json.dumps("not-a-number"),),
        )
        conn.commit()
    s.reload_cache()
    assert s.get("poll_interval") == 2          # cleaned to default on load
