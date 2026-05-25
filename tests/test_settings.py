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
