"""Regression tests for the 2026-11-08 repo review (.repo-review/2026-11-08T121013Z).

P1-1  non-finite settings values / crafted restores wedging the settings layer
P1-2  SSRF guard bypass via IPv4-mapped (and other IPv4-embedding) IPv6 literals
P2-1  cached AI provider reachability on /api/explain/status
P2-2  /api/import zip-bomb + restore hardening (authorizer whitelist, canonical schema)
P2-3  TTL cache / single-flight / miss rate-limit on the expensive read endpoints
P2-4  WebSocket + SSE subscriber caps
P3    snapshot sampling, enrichment cache policy, loader shape guards, misc

Stubs sit at I/O boundaries (httpx client, smtplib, the DB file) — never on the
function under test (see CLAUDE.md "Don't stub the function under test").
"""
from __future__ import annotations

import asyncio
import io
import json
import math
import sqlite3
import time
import zipfile
import zlib

import pytest


def _zip(sql: str, extra: dict[str, str] | None = None, name: str = "piscope.sql") -> bytes:
    bio = io.BytesIO()
    with zipfile.ZipFile(bio, "w", zipfile.ZIP_DEFLATED) as zf:
        zf.writestr(name, sql)
        for k, v in (extra or {}).items():
            zf.writestr(k, v)
    return bio.getvalue()


def _import(client, payload: bytes):
    return client.post("/piscope/api/import",
                       files={"file": ("backup.zip", payload, "application/zip")},
                       headers={"X-PiScope-Import": "1"})


@pytest.fixture()
def quiet_feed(monkeypatch):
    """/api/import stops + restarts the feed around the DB swap; keep the real poll
    loop from starting during tests."""
    from app.services import feed as feed_mod

    async def _noop():
        return None
    monkeypatch.setattr(feed_mod.feed_service, "stop", _noop)
    monkeypatch.setattr(feed_mod.feed_service, "start", _noop)


def _post_json_raw(client, url: str, raw: str):
    return client.post(url, content=raw, headers={"content-type": "application/json"})


# --- P1-1: non-finite settings ------------------------------------------------------


@pytest.mark.parametrize("raw", ['{"poll_interval": Infinity}', '{"trail_length": 1e400}',
                                 '{"poll_interval": NaN}', '{"poll_interval": -Infinity}'])
def test_settings_non_finite_json_is_400_and_unchanged(client, raw):
    before = client.get("/piscope/api/settings").json()
    r = _post_json_raw(client, "/piscope/api/settings", raw)
    assert r.status_code == 400, r.text
    after = client.get("/piscope/api/settings").json()
    assert after["poll_interval"] == before["poll_interval"]
    assert after["trail_length"] == before["trail_length"]


def test_settings_non_finite_string_and_huge_int_never_500(client):
    # "1e999" is a string (silently rejected, per the per-key contract); a 400-digit
    # JSON int is a real Python int and simply clamps.
    raw = '{"poll_interval": "1e999", "trail_length": 1' + "0" * 400 + "}"
    r = _post_json_raw(client, "/piscope/api/settings", raw)
    assert r.status_code == 200, r.text
    assert r.json()["poll_interval"] == 2          # unchanged default
    assert r.json()["trail_length"] == 500         # clamped to the validator's max


def test_v_int_and_keep_alive_reject_non_finite():
    from app.services import settings as s
    v = s._VALIDATORS["poll_interval"]
    for bad in (float("inf"), float("-inf"), float("nan"), "1e999", "Infinity"):
        with pytest.raises(ValueError):
            s._validated("poll_interval", bad)
    assert v(10 ** 400) == 120 and v(-(10 ** 400)) == 1
    with pytest.raises(ValueError):
        s._validated("ollama_keep_alive", float("inf"))
    assert s._validated("ollama_keep_alive", 10 ** 400) == 86400


def test_populate_cache_survives_poisoned_rows(temp_db):
    from app.services import settings as s
    with sqlite3.connect(s.DB_PATH) as conn:
        conn.executemany("INSERT OR REPLACE INTO settings(key, value) VALUES(?, ?)", [
            ("poll_interval", "Infinity"),
            ("trail_length", "1e400"),
            ("antenna_range_nm", "1" + "0" * 400),
            # Deep enough to raise RecursionError on every interpreter (3.11 already
            # fails at ~5k levels; 3.14 copes with 50k).
            ("webhooks_json", json.dumps("[" * 200_000 + "]" * 200_000)),
            ("digest_latest_json", '{"x": NaN}'),
        ])
    s.reload_cache()
    assert s.get("poll_interval") == s.DEFAULTS["poll_interval"]
    assert s.get("trail_length") == s.DEFAULTS["trail_length"]
    assert s.get("antenna_range_nm") == 2000        # huge int clamps rather than failing
    assert s.get("webhooks_json") == s.DEFAULTS["webhooks_json"]
    assert s.get("digest_latest_json") is None
    json.dumps(s.get_all(redact=True), allow_nan=False)   # strict-JSON serialisable


def test_import_with_infinity_setting_resets_it_and_service_stays_up(client, quiet_feed):
    from app.services import settings as s
    sql = ("CREATE TABLE settings (key TEXT PRIMARY KEY, value TEXT);\n"
           "INSERT INTO settings(key,value) VALUES('poll_interval','Infinity');\n"
           "INSERT INTO settings(key,value) VALUES('theme','\"nord\"');\n")
    r = _import(client, _zip(sql))
    assert r.status_code == 200, r.text
    assert r.json()["settings_reset"] == ["poll_interval"]
    got = client.get("/piscope/api/settings")
    assert got.status_code == 200 and got.json()["theme"] == "nord"
    assert got.json()["poll_interval"] == s.DEFAULTS["poll_interval"]
    assert client.get("/piscope/api/health").status_code == 200


@pytest.mark.parametrize("body", ['{"hex":"abc123","altitude_baro":Infinity}',
                                  '{"hex":"abc123","heading":NaN}',
                                  '{"hex":"abc123","distance_nm":1e400}'])
def test_explain_non_finite_body_is_422_not_500(client, body):
    # FastAPI's default 422 echoed the offending input; strict JSON rendering of an
    # `inf` input then raised → 500. Both AI endpoints now answer a clean 422.
    r = _post_json_raw(client, "/piscope/api/explain", body)
    assert r.status_code == 422, r.text
    r = _post_json_raw(client, "/piscope/api/explain/followup",
                       '{"aircraft": %s, "question": "hi"}' % body)
    assert r.status_code == 422, r.text


@pytest.mark.parametrize("param", ["min_alt", "max_alt", "min_speed", "radius_km", "overhead_threshold_s"])
@pytest.mark.parametrize("value", ["inf", "-inf", "nan", "1e309"])
def test_dashboard_summary_non_finite_params_are_400(client, param, value):
    r = client.get(f"/piscope/api/dashboard/summary?lat=51&lon=0&{param}={value}")
    assert r.status_code == 400, r.text
    assert r.json()["success"] is False


def test_bounded_int_handles_overflow():
    from app.services.ai import _common
    assert _common._bounded_int(float("inf"), 0, 10) is None
    assert _common._bounded_float(10 ** 400, 0, 10) is None


# --- P1-2: SSRF -----------------------------------------------------------------------

BLOCKED_URLS = [
    "http://169.254.169.254/", "http://2852039166/", "http://0xA9FEA9FE/",
    "http://0251.0376.0251.0376/", "http://169.254.43518/", "http://100.100.100.200/",
    "http://[::ffff:169.254.169.254]/", "http://[::ffff:a9fe:a9fe]/",
    "http://[::ffff:100.100.100.200]/", "http://[64:ff9b::a9fe:a9fe]/",
    "http://[2002:a9fe:a9fe::]/", "http://[::a9fe:a9fe]/", "http://[fe80::1%25eth0]/",
    "http://[fd00:ec2::254]/", "http://metadata.google.internal/",
    "http://metadata.google.internal./", "http://169.254.169.254./",
    "http://0.0.0.0/", "http://[::]/", "http://224.0.0.1/", "http://[ff02::1]/",
]
ALLOWED_URLS = [
    "http://localhost/tar1090", "http://127.0.0.1:8080/", "http://[::1]/",
    "http://10.0.0.212:11434", "http://192.168.1.5/", "http://[::ffff:10.0.0.5]/",
]


@pytest.mark.parametrize("url", BLOCKED_URLS)
def test_ssrf_guard_blocks_encoded_metadata_forms(url):
    from app.services._http import validate_external_url
    with pytest.raises(ValueError):
        validate_external_url(url)


@pytest.mark.parametrize("url", ALLOWED_URLS)
def test_ssrf_guard_still_allows_loopback_and_lan(url):
    from app.services._http import validate_external_url
    assert validate_external_url(url) == url


@pytest.mark.parametrize("url", ["http://[::ffff:169.254.169.254]/", "http://[::ffff:100.100.100.200]/"])
def test_webhook_test_endpoint_refuses_mapped_metadata(client, monkeypatch, url):
    from app.services import webhooks
    sent = []

    async def _post_one(*a, **k):
        sent.append(a)
    monkeypatch.setattr(webhooks, "_post_one", _post_one)
    r = client.post("/piscope/api/webhooks/test", json={"kind": "generic", "url": url})
    assert r.status_code == 400, r.text
    assert not sent, "a mapped metadata address reached the HTTP layer"


def test_ssrf_resolved_hostname_to_mapped_metadata_is_blocked(monkeypatch):
    from app.services import _http

    def fake_getaddrinfo(host, *a, **k):
        return [(10, 1, 6, "", ("::ffff:169.254.169.254", 0, 0, 0))]
    monkeypatch.setattr(_http.socket, "getaddrinfo", fake_getaddrinfo)
    with pytest.raises(ValueError):
        _http.validate_external_url("http://innocent.example/", resolve=True)


def test_shared_http_client_does_not_follow_redirects():
    # A redirect to a metadata address would bypass the pre-request check entirely.
    from app.services import _http
    client = asyncio.run(_http.get_client())
    try:
        assert client.follow_redirects is False
    finally:
        asyncio.run(_http.close_client())


# --- P2-2: import limits + restore hardening ----------------------------------------


def test_import_rejects_declared_oversize_entry(client, monkeypatch):
    from app.services import restore
    monkeypatch.setattr(restore, "MAX_SQL_BYTES", 1000)
    r = _import(client, _zip("-- " + "x" * 5000 + "\n"))
    assert r.status_code == 400 and "uncompressed" in r.json()["detail"]


def test_capped_reader_enforces_real_size_even_if_header_lies():
    # The central directory's file_size is attacker-controlled; the byte counter is not.
    from app.services import restore
    reader = io.BufferedReader(restore._CappedReader(io.BytesIO(b"a" * 5000), 1000))
    with pytest.raises(restore.RestoreError):
        reader.read()


def test_import_rejects_too_many_entries(client):
    r = _import(client, _zip("SELECT 1;\n", {f"junk{i}.txt": "x" for i in range(20)}))
    assert r.status_code == 400 and "entries" in r.json()["detail"]


def test_import_rejects_oversize_upload(client):
    r = client.post("/piscope/api/import",
                    files={"file": ("backup.zip", b"\0" * (64 * 1024 * 1024 + 1), "application/zip")},
                    headers={"X-PiScope-Import": "1"})
    assert r.status_code == 413


@pytest.mark.parametrize("sql", [
    "VACUUM INTO '/tmp/piscope-pwned.db';",
    "CREATE TRIGGER t AFTER INSERT ON settings BEGIN DELETE FROM settings; END;",
    "CREATE VIEW v AS SELECT 1;",
    "CREATE TEMP TABLE t(x);",
    "CREATE TABLE t(x);\nWITH RECURSIVE c(x) AS (SELECT 1 UNION ALL SELECT x+1 FROM c) INSERT INTO t SELECT x FROM c;",
    "CREATE TABLE t(x);\nINSERT INTO t VALUES(randomblob(100000000));",
    "CREATE TABLE t(x);\nINSERT INTO t VALUES(zeroblob(100000000));",
    "CREATE TABLE t(x);\nINSERT INTO t VALUES('a');\nUPDATE t SET x = x || x;",
    "PRAGMA writable_schema = ON;",
    "CREATE TABLE settings(key TEXT PRIMARY KEY, value TEXT);\nDELETE FROM settings;",
    "ATTACH DATABASE ':memory:' AS evil;",
    # Expression defaults/indexes/checks: DEFAULT bypasses the FUNCTION hook entirely.
    "CREATE TABLE t(x DEFAULT (zeroblob(100000000)));\nINSERT INTO t DEFAULT VALUES;",
    "CREATE TABLE t(x);\nINSERT INTO t VALUES(1);\nCREATE INDEX i ON t(zeroblob(100000000));",
    "CREATE TABLE t(x CHECK (length(printf('%.*c', 100000000, 'x')) > 0));\nINSERT INTO t VALUES(1);",
])
def test_restore_authorizer_denies_non_dump_sql(client, sql):
    r = _import(client, _zip(sql + "\n"))
    assert r.status_code == 400, (sql, r.text)


def test_import_refuses_generated_columns(client):
    sql = ("CREATE TABLE settings(key TEXT PRIMARY KEY, raw TEXT, "
           "value TEXT GENERATED ALWAYS AS (raw) VIRTUAL);\n"
           "INSERT INTO settings(key, raw) VALUES('theme', '\"nord\"');\n")
    r = _import(client, _zip(sql))
    assert r.status_code == 400 and "generated" in r.json()["detail"]


def test_import_normalises_a_redefined_schema(client, quiet_feed):
    # A dump that drops settings' PRIMARY KEY used to break every upsert afterwards;
    # rows are now copied into the canonical schema, unknown tables/keys are dropped.
    sql = ("CREATE TABLE settings(key, value);\n"
           "INSERT INTO settings VALUES('theme', '\"nord\"');\n"
           "INSERT INTO settings VALUES('not_a_setting', '1');\n"
           "CREATE TABLE junk(x);\nINSERT INTO junk VALUES(1);\n")
    assert _import(client, _zip(sql)).status_code == 200
    assert client.post("/piscope/api/settings", json={"theme": "dracula"}).json()["theme"] == "dracula"
    from app.services import settings as s
    with sqlite3.connect(s.DB_PATH) as conn:
        tables = {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        keys = {r[0] for r in conn.execute("SELECT key FROM settings")}
    assert "junk" not in tables and "not_a_setting" not in keys


def test_import_cleans_webhooks_and_views_like_their_endpoints(client, quiet_feed):
    hooks = json.dumps([{"kind": "discord", "url": "javascript:alert(1)"},
                        {"kind": "<b>", "url": "https://ok.example/h", "types": ["military", {"x": 1}],
                         "label": 12345}])
    views = json.dumps([{"name": "home", "lat": 51.5, "lon": 0, "zoom": 8}, {"name": "bad", "lat": "x"}])
    sql = ("CREATE TABLE settings(key TEXT PRIMARY KEY, value TEXT);\n"
           f"INSERT INTO settings VALUES('webhooks_json', {_sql_str(json.dumps(hooks))});\n"
           f"INSERT INTO settings VALUES('saved_views_json', {_sql_str(json.dumps(views))});\n")
    assert _import(client, _zip(sql)).status_code == 200
    assert client.get("/piscope/api/webhooks").json()["webhooks"] == [
        {"kind": "generic", "url": "https://ok.example/h", "types": ["military"], "label": ""}]
    assert [v["name"] for v in client.get("/piscope/api/views").json()["views"]] == ["home"]


def _sql_str(s: str) -> str:
    return "'" + s.replace("'", "''") + "'"


def test_import_rejects_truncated_statement(client):
    r = _import(client, _zip("CREATE TABLE settings(key TEXT PRIMARY KEY, value TEXT);\nINSERT INTO settings VALUES('a',\n"))
    assert r.status_code == 400 and "middle of a statement" in r.json()["detail"]


def test_import_rejects_corrupt_deflate_stream(client):
    good = bytearray(_zip("CREATE TABLE settings(key TEXT PRIMARY KEY, value TEXT);\n" * 200))
    # Flip bytes inside the compressed data (after the 30-byte local header + name).
    for i in range(60, 90):
        good[i] ^= 0xFF
    r = _import(client, bytes(good))
    assert r.status_code == 400, r.text


def test_export_import_round_trip_preserves_every_table(client, quiet_feed):
    """A real export of a DB with a row in every table — including a multi-line note
    with quotes and semicolons (the statement splitter's hard case), a snapshot blob,
    and an AUTOINCREMENT table — restores exactly."""
    from app.services import events, settings as s
    client.post("/piscope/api/settings", json={"theme": "nord", "fa_api_key": "SECRET"})
    events.record_snapshot(time.time(), {"aircraft": [{"hex": "abc123", "lat": 51.0}]})
    note = "line1;\nit's line2;\n'';\n"
    with sqlite3.connect(s.DB_PATH) as conn:
        conn.execute("INSERT INTO events(ts,kind,hex,payload) VALUES(1,'military','abc123','{}')")
        conn.execute("INSERT INTO aircraft_notes VALUES('abc123', ?, 1)", (note,))
        conn.execute("INSERT INTO position_heatmap VALUES(1030, -2, 7)")
        conn.execute("INSERT INTO aircraft_sightings(hex,date,first_ts,last_ts,polls) VALUES('abc123','2026-11-08',1,2,3)")
        conn.execute("INSERT INTO hourly_stats(hour,unique_aircraft,obs) VALUES('2026-11-08T10',5,50)")
        before = {t: conn.execute(f'SELECT COUNT(*) FROM "{t}"').fetchone()[0] for t, in
                  conn.execute("SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%'")}
        keys_before = {k for k, in conn.execute("SELECT key FROM settings")}
    exp = client.get("/piscope/api/export")
    assert exp.status_code == 200 and exp.headers["content-type"] == "application/zip"
    with zipfile.ZipFile(io.BytesIO(exp.content)) as zf:
        assert set(zf.namelist()) == {"piscope.sql", "exported_at.txt", "README-RESTORE.txt"}
        assert b"SECRET" not in zf.read("piscope.sql")
    client.post("/piscope/api/settings", json={"theme": "radar"})
    r = _import(client, exp.content)
    assert r.status_code == 200, r.text
    with sqlite3.connect(s.DB_PATH) as conn:
        after = {t: conn.execute(f'SELECT COUNT(*) FROM "{t}"').fetchone()[0] for t in before}
        keys_after = {k for k, in conn.execute("SELECT key FROM settings")}
        assert conn.execute("SELECT note FROM aircraft_notes").fetchone()[0] == note
        assert conn.execute("PRAGMA user_version").fetchone()[0] == s.SCHEMA_VERSION
    # A restore re-runs the migrations from v0 (as the raw restore always did), and the
    # v4 one deliberately pins host_guard_enabled for "existing" installs — so that
    # one settings row may appear; nothing else may change.
    assert keys_after - keys_before <= {"host_guard_enabled"} and keys_before <= keys_after
    after["settings"] = before["settings"]
    assert after == before
    assert client.get("/piscope/api/settings").json()["theme"] == "nord"
    assert events.snapshot_timeline()   # snapshot blob survived and decodes


def test_export_refuses_cross_site_and_runs_one_at_a_time(client):
    assert client.get("/piscope/api/export", headers={"Sec-Fetch-Site": "cross-site"}).status_code == 403
    assert client.get("/piscope/api/export", headers={"Sec-Fetch-Site": "same-origin"}).status_code == 200


def test_daily_backup_streams_a_valid_zip(temp_db, tmp_path):
    from app.services import backups
    out = backups._write_backup(tmp_path)
    assert out is not None and out.exists()
    assert not list(tmp_path.glob("*.partial"))
    with zipfile.ZipFile(out) as zf:
        assert "CREATE TABLE" in zf.read("piscope.sql").decode()


def test_snapshot_decode_refuses_zlib_bomb(temp_db, monkeypatch):
    from app.services import events
    monkeypatch.setattr(events, "_MAX_SNAPSHOT_JSON_BYTES", 1024)
    bomb = events._SNAPSHOT_MAGIC + zlib.compress(b'{"aircraft": ["' + b"a" * 10_000_000 + b'"]}', 9)
    assert len(bomb) < 50_000
    assert events._decode_snapshot(bomb) is None
    ok = events._SNAPSHOT_MAGIC + zlib.compress(b'{"aircraft": []}')
    assert events._decode_snapshot(ok) == {"aircraft": []}


# --- P2-3: expensive reads ------------------------------------------------------------


def test_analytics_is_cached_within_ttl(client, monkeypatch):
    from app.routers import api
    from app.services import analytics
    api._EXPENSIVE_READS.clear()
    calls = []
    real = analytics.overview
    monkeypatch.setattr(analytics, "overview", lambda rk: calls.append(rk) or real(rk))
    for _ in range(3):
        assert client.get("/piscope/api/analytics?range=7d").status_code == 200
    assert calls == ["7d"]
    assert client.get("/piscope/api/analytics?range=bogus").status_code == 400


def test_expensive_read_misses_are_rate_limited(client):
    from app.routers import api
    api._EXPENSIVE_READS.clear()
    codes = [client.get(f"/piscope/api/analytics/notable?range=7d&limit={i}").status_code
             for i in range(1, api._EXPENSIVE_MISSES_PER_MIN + 3)]
    assert codes[:api._EXPENSIVE_MISSES_PER_MIN] == [200] * api._EXPENSIVE_MISSES_PER_MIN
    assert codes[-1] == 429
    # A cached key is still served while the miss bucket is exhausted.
    assert client.get("/piscope/api/analytics/notable?range=7d&limit=1").status_code == 200


def test_expensive_read_cache_is_single_flight():
    from app.routers import api
    cache = api._ExpensiveReadCache(ttl_s=60)
    calls = []

    def slow(x):
        calls.append(x)
        time.sleep(0.2)
        return x * 2

    async def main():
        return await asyncio.gather(*(cache.get(("k",), slow, 21) for _ in range(8)))
    from app.services import ratelimit
    ratelimit.reset()
    assert asyncio.run(main()) == [42] * 8
    assert calls == [21]


def test_returning_query_matches_reference(temp_db):
    """The rewritten (COUNT(*) + active-hex prefilter) returning query must equal the
    original COUNT(DISTINCT date) formulation."""
    from app.services import notable, settings as s
    now = time.time()
    day = 86400
    rows = []
    for h in range(40):
        hexid = f"a{h:05x}"
        for d in range(h % 7 + 1):
            ts = now - (h * 0.37 + d) * day
            rows.append((hexid, time.strftime("%Y-%m-%d", time.gmtime(ts)), ts - 60, ts, 5, h % 3 == 0))
    with sqlite3.connect(s.DB_PATH) as conn:
        conn.executemany("INSERT OR IGNORE INTO aircraft_sightings(hex,date,first_ts,last_ts,polls,military) "
                         "VALUES(?,?,?,?,?,?)", rows)
        conn.commit()   # returning_in_window reads through its own connection
        for since in (now - day, now - 7 * day, None):
            ref = conn.execute(
                "SELECT hex, COUNT(DISTINCT date) AS d, MAX(last_ts) AS l FROM aircraft_sightings "
                "GROUP BY hex HAVING d >= 2 AND l >= ? ORDER BY d DESC, l DESC LIMIT 100",
                (since or 0,)).fetchall()
            got = notable.returning_in_window(since, 2, 100)
            assert [(r["hex"], r["days_seen"]) for r in got] == [(h, d) for h, d, _ in ref], since


# --- P2-4: subscriber caps ------------------------------------------------------------


def test_ws_subscriber_cap_closes_with_1013(client, monkeypatch):
    from starlette.websockets import WebSocketDisconnect
    from app.services import feed
    monkeypatch.setattr(feed, "MAX_WS_SUBSCRIBERS", 1)
    with client.websocket_connect("/piscope/ws") as first:
        first.receive_json()   # initial snapshot
        with client.websocket_connect("/piscope/ws") as second:
            with pytest.raises(WebSocketDisconnect) as exc:
                second.receive_json()
            assert exc.value.code == 1013
    assert feed.feed_service.health()["subscriber_count"] == 0


def test_sse_subscriber_cap(client, monkeypatch):
    from app.services import events_bus
    events_bus._subscribers.clear()
    monkeypatch.setattr(events_bus, "MAX_SUBSCRIBERS", 0)
    r = client.get("/piscope/api/dashboard/events")
    assert r.status_code == 503 and r.headers.get("retry-after") == "30"
    with pytest.raises(events_bus.SubscriberLimitError):
        events_bus.subscribe()
    stats = client.get("/piscope/api/dashboard/events/stats").json()
    assert stats["subscriber_limit"] == 0 and stats["subscribers"] == 0


# --- P2-1: reachability ---------------------------------------------------------------


def test_explain_status_reports_cached_reachability(temp_db, monkeypatch):
    from app import main  # noqa: F401  (router wiring)
    from app.routers import api
    from app.services import ai, settings as s
    s.set_many({"ai_provider": "claude_cli", "claude_cli_enabled": True,
                "claude_cli_url": "http://10.0.0.155:8090"})
    pings = []

    async def fake_ping():
        pings.append(1)
        return {"ok": False, "error": "ConnectError: All connection attempts failed"}
    monkeypatch.setattr(ai, "ping", fake_ping)
    ai._reach.update(key=None, checked_at=None, ok=None, error=None)

    async def flow():
        first = await api.explain_status()
        assert first["configured"] is True and first["provider"]["reachable"] is None
        await asyncio.sleep(0)            # let the background probe run
        await asyncio.sleep(0)
        second = await api.explain_status()
        third = await api.explain_status()
        return second, third
    second, third = asyncio.run(flow())
    assert second["provider"]["reachable"] is False
    assert "ConnectError" in second["provider"]["error"]
    assert third["provider"]["reachable"] is False and len(pings) == 1   # cached within TTL


# --- P3 -----------------------------------------------------------------------------


def test_snapshot_timeline_includes_newest_and_oldest(temp_db):
    from app.services import events, settings as s
    now = time.time()
    with sqlite3.connect(s.DB_PATH) as conn:
        conn.executemany("INSERT INTO feed_snapshots(ts, payload) VALUES(?, ?)",
                         [(now - 1000 + i, b"x") for i in range(1000)])
    pts = events.snapshot_timeline(max_points=100)
    assert len(pts) == 100
    assert pts[0] == now - 1000 and pts[-1] == now - 1
    assert pts == sorted(pts)
    assert len(events.snapshot_timeline(max_points=5000)) == 1000


def test_digest_peak_sees_the_newest_snapshots(temp_db):
    from app.services import digest, events, settings as s
    now = time.time()

    def blob(n):
        return events._SNAPSHOT_MAGIC + zlib.compress(json.dumps({"aircraft": [{}] * n}).encode())
    rows = [(now - 3500 + i, blob(1)) for i in range(3400)]
    rows.append((now - 1, blob(77)))           # the busiest moment is the most recent one
    with sqlite3.connect(s.DB_PATH) as conn:
        conn.executemany("INSERT INTO feed_snapshots(ts, payload) VALUES(?, ?)", rows)
    d = digest.build_digest(window_hours=24)
    assert d["totals"]["peak_concurrent_aircraft"] == 77


class _FakeResp:
    def __init__(self, status, body=None):
        self.status_code = status
        self._body = body

    def json(self):
        return self._body


class _FakeClient:
    def __init__(self, responses):
        self.responses = list(responses)
        self.calls = 0

    async def get(self, *a, **k):
        self.calls += 1
        return self.responses.pop(0)


@pytest.mark.parametrize("modname,arg", [("hexdb", "abc123"), ("adsbdb", "BAW123"), ("planespotters", "abc123")])
def test_enrichment_caches_404_but_backs_off_transient(monkeypatch, modname, arg):
    import importlib
    mod = importlib.import_module(f"app.services.{modname}")
    mod._CACHE.clear()
    mod._BACKOFF.clear()
    fake = _FakeClient([_FakeResp(503), _FakeResp(404)])

    async def get_client():
        return fake
    monkeypatch.setattr(mod, "get_client", get_client)
    assert asyncio.run(mod.lookup(arg)) is None          # 503 → transient, not cached
    assert asyncio.run(mod.lookup(arg)) is None          # within backoff: no upstream call
    assert fake.calls == 1
    mod._BACKOFF.clear()                                  # backoff lapses
    assert asyncio.run(mod.lookup(arg)) == {}            # 404 → genuine miss, cached
    assert asyncio.run(mod.lookup(arg)) == {} and fake.calls == 2


def test_hexdb_non_string_fields_do_not_500(monkeypatch):
    from app.services import hexdb
    hexdb._CACHE.clear()
    hexdb._BACKOFF.clear()
    fake = _FakeClient([_FakeResp(200, {"Registration": 12345, "Manufacturer": {"x": 1},
                                        "Type": "A320 ", "ICAOTypeCode": None})])

    async def get_client():
        return fake
    monkeypatch.setattr(hexdb, "get_client", get_client)
    out = asyncio.run(hexdb.lookup("abc123"))
    assert out["registration"] is None and out["manufacturer"] is None and out["type"] == "A320"


@pytest.mark.parametrize("modname,fn", [("notable", "_load_rules"), ("categorize", "_load_table"),
                                        ("airlines", "_load_table")])
@pytest.mark.parametrize("bad", ["[1, 2]", '"text"', "{not json", '{"categories": [1], "airlines": [1], '
                                 '"military_hex_ranges": {"a": 1}, "military_callsign_prefixes": [1]}'])
def test_bundled_json_loaders_survive_bad_shapes(tmp_path, monkeypatch, modname, fn, bad):
    import importlib
    mod = importlib.import_module(f"app.services.{modname}")
    p = tmp_path / "data.json"
    p.write_text(bad)
    monkeypatch.setattr(mod, "_DATA_PATH", p)
    getattr(mod, fn)()   # must not raise


def test_dashboard_contact_tags_are_unique():
    from app.services import dashboard
    out = dashboard._contact_compact({"hex": "ae1234", "military": True}, 1.0, "military")
    assert out["tags"].count("military") == 1


def test_every_secret_has_a_set_flag(temp_db):
    from app.services import settings as s
    s.set_many({"smtp_pass": "hunter2"})
    allv = s.get_all(redact=True)
    for k in s.SECRET_KEYS:
        assert f"{k}_set" in allv
    assert allv["smtp_pass_set"] is True and allv["smtp_pass"] == "***"


def test_smtp_ssl_uses_implicit_tls(temp_db, monkeypatch):
    from app.services import digest, settings as s
    calls = []

    class _Fake:
        def __init__(self, *a, **k):
            calls.append(type(self).__name__)

        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

        def ehlo(self):
            pass

        def starttls(self, **k):
            calls.append("starttls")

        def login(self, *a):
            pass

        def send_message(self, m):
            calls.append("sent")

    class SMTP_SSL(_Fake):
        pass

    class SMTP(_Fake):
        pass
    monkeypatch.setattr(digest.smtplib, "SMTP_SSL", SMTP_SSL)
    monkeypatch.setattr(digest.smtplib, "SMTP", SMTP)
    s.set_many({"smtp_host": "mail.example", "smtp_port": 465, "smtp_from": "a@x", "smtp_to": "b@x",
                "smtp_use_ssl": True, "smtp_use_starttls": True})
    assert digest._send_email("hi") == (True, None)
    assert calls == ["SMTP_SSL", "sent"]     # no STARTTLS inside an already-TLS session


def test_webhook_save_tolerates_unhashable_types(client):
    r = client.post("/piscope/api/webhooks", json={"webhooks": [
        {"url": "https://h.example/x", "types": [{"a": 1}, "military"], "label": 7}]})
    assert r.status_code == 200
    assert r.json()["webhooks"][0]["types"] == ["military"]


def test_fresh_db_lands_on_schema_version(temp_db):
    from app.services import settings as s
    with sqlite3.connect(s.DB_PATH) as conn:
        assert conn.execute("PRAGMA user_version").fetchone()[0] == s.SCHEMA_VERSION


def test_validation_error_handler_never_emits_non_finite_json(client):
    r = _post_json_raw(client, "/piscope/api/webhooks/test", '{"url": Infinity}')
    assert r.status_code == 422
    json.loads(r.text)     # strict JSON
    assert not any(isinstance(v, float) and not math.isfinite(v)
                   for v in json.loads(r.text, parse_constant=lambda c: float(c)).values()
                   if isinstance(v, float))
