"""AI façade: provider dispatch + fallback, cache-key provider scoping, cloud vendor logic.

No network — only the offline predicates (is_configured / active_provider_name /
cache-key construction) are exercised.
"""
from __future__ import annotations

import sys
import pathlib

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))


def test_active_provider_fallback_to_ollama(temp_db):
    from app.services import ai
    from app.services import settings as s
    # No ai_provider set, but ollama_enabled → back-compat fallback.
    s.set_one("ollama_enabled", True)
    assert ai.active_provider_name() == "ollama"


def test_active_provider_explicit(temp_db):
    from app.services import ai
    from app.services import settings as s
    s.set_one("ai_provider", "cloud_api")
    assert ai.active_provider_name() == "cloud_api"


def test_active_provider_none(temp_db):
    from app.services import ai
    # Fresh DB: nothing configured.
    assert ai.active_provider_name() == "none"
    assert ai.is_configured() is False


def test_is_configured_dispatch(temp_db):
    from app.services import ai
    from app.services import settings as s
    s.set_many({"ai_provider": "claude_cli", "claude_cli_enabled": True,
                "claude_cli_url": "http://10.0.0.155:8090"})
    assert ai.is_configured() is True


def test_cache_key_scoped_by_provider():
    from app.services.ai import _common
    payload = {"hex": "abc123", "type_code": "B789", "callsign": "BAW1"}
    k_ollama = _common.aircraft_cache_key(payload, "ollama")
    k_cloud = _common.aircraft_cache_key(payload, "cloud_api")
    assert k_ollama != k_cloud           # switching provider can't serve a stale brief
    assert "ollama" in k_ollama


def test_cap_response_trims_at_word_boundary():
    from app.services.ai import _common
    long = "word " * 2000
    out = _common.cap_response(long)
    assert len(out) <= _common.MAX_RESPONSE_CHARS + 1
    assert out.endswith("…")


def test_cloud_api_vendor_defaults(temp_db):
    from app.services.ai import cloud_api
    from app.services import settings as s
    assert set(cloud_api.DEFAULT_MODELS) == {"anthropic", "openai", "google", "openai_compatible"}
    # Default vendor when unset / invalid.
    assert cloud_api._vendor() == "anthropic"
    s.set_one("cloud_api_vendor", "google")
    assert cloud_api._vendor() == "google"
    # is_configured needs both enabled + key.
    assert cloud_api.is_configured() is False
    s.set_many({"cloud_api_enabled": True, "cloud_api_key": "sk-test"})
    assert cloud_api.is_configured() is True


def test_cloud_api_openai_compatible_config_and_ssrf(temp_db):
    # The openai_compatible vendor needs a valid, non-blocked base URL — but no API key
    # (open LAN servers often need none). The base URL is the one user-redirectable host,
    # so it goes through the same SSRF guard as the ollama/shim URLs.
    from app.services.ai import cloud_api
    from app.services import settings as s

    s.set_many({"cloud_api_enabled": True, "cloud_api_vendor": "openai_compatible"})
    assert cloud_api._vendor() == "openai_compatible"
    # No base URL yet → not configured, even with the master switch on.
    assert cloud_api.is_configured() is False
    assert cloud_api._base_url() is None

    # link-local / cloud-metadata base is refused (dotted and numeric-encoded forms).
    s.set_one("cloud_api_base_url", "http://169.254.169.254/v1")
    assert cloud_api._base_url() is None and cloud_api.is_configured() is False
    s.set_one("cloud_api_base_url", "http://2852039166/v1")   # 169.254.169.254 as one decimal
    assert cloud_api._base_url() is None and cloud_api.is_configured() is False

    # non-http(s) scheme refused.
    s.set_one("cloud_api_base_url", "file:///etc/passwd")
    assert cloud_api._base_url() is None

    # A LAN endpoint is allowed (LiteLLM/vLLM on the LAN); trailing slash is trimmed,
    # and no API key is required for this vendor.
    s.set_one("cloud_api_base_url", "http://10.0.0.5:4000/v1/")
    assert cloud_api._base_url() == "http://10.0.0.5:4000/v1"
    assert cloud_api.is_configured() is True

    # No universal default model for this vendor — it echoes whatever the user set.
    assert cloud_api._model() == ""
    s.set_one("cloud_api_model", "gpt-oss:120b")
    assert cloud_api._model() == "gpt-oss:120b"


def test_ai_provider_urls_reject_link_local_keep_lan(temp_db):
    # S4: user-set provider URLs go through the SSRF guard — link-local/metadata blocked,
    # LAN and loopback still allowed (Ollama/shim commonly run on a LAN box or same host).
    from app.services import settings as s
    from app.services.ai import ollama, claude_cli

    s.set_many({"ollama_enabled": True, "ollama_url": "http://169.254.169.254:11434"})
    assert ollama.is_configured() is False
    s.set_one("ollama_url", "http://10.0.0.5:11434")
    assert ollama.is_configured() is True

    s.set_many({"claude_cli_enabled": True, "claude_cli_url": "http://169.254.169.254:8090"})
    assert claude_cli.is_configured() is False
    s.set_one("claude_cli_url", "http://127.0.0.1:8090")   # loopback intentionally allowed
    assert claude_cli.is_configured() is True


def test_ollama_ping_reports_blocked_url(temp_db):
    import asyncio
    from app.services import settings as s
    from app.services.ai import ollama
    s.set_many({"ollama_enabled": True, "ollama_url": "http://169.254.169.254"})
    res = asyncio.run(ollama.ping())   # returns before any network call
    assert res["ok"] is False and "blocked" in res["error"].lower()


def test_explain_cache_roundtrip():
    from app.services.ai import _common
    _common.cache_clear()
    assert _common.cache_get("k1") is None
    _common.cache_set("k1", "a brief")
    assert _common.cache_get("k1") == "a brief"
    _common.cache_clear()
    assert _common.cache_get("k1") is None
