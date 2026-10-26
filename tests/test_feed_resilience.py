"""Feed-loop resilience (iteration 13): a transient all-feeds-failure must not
blank the live view or re-arm the alert de-dup, but a sustained outage must."""
from __future__ import annotations

import asyncio
import sys
import pathlib
import time

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))


def _make_feed(monkeypatch, temp_db):
    from app.services.feed import FeedService
    feed = FeedService()
    # Every feed fetch fails this cycle: empty rows + an ok:False status entry,
    # exactly like a real upstream timeout.
    async def _fail(name, url, *, kind):
        feed.feed_status[name] = {"kind": kind, "url": url, "ok": False,
                                  "error": "TimeoutException"}
        return []
    monkeypatch.setattr(feed, "_fetch_one", _fail)
    return feed


def test_transient_failure_keeps_store_and_dedup(temp_db, monkeypatch):
    feed = _make_feed(monkeypatch, temp_db)
    from app.models.aircraft import Aircraft
    feed.aircraft = {"abc123": Aircraft(hex="abc123", lat=51.0, lon=-0.1)}
    feed.trails["abc123"] = __import__("collections").deque([(51.0, -0.1, time.time())])
    feed._notified_military.add("abc123")
    feed._last_any_feed_ok_at = time.time()   # feed was healthy a moment ago

    asyncio.run(feed._poll_once())

    # Within the 60 s grace window the previous store + de-dup survive, so the
    # map doesn't blank and a recovered feed two seconds later won't re-alert.
    assert "abc123" in feed.aircraft
    assert "abc123" in feed._notified_military
    assert feed.connection_state == "error"


def test_sustained_outage_clears_store(temp_db, monkeypatch):
    feed = _make_feed(monkeypatch, temp_db)
    from app.models.aircraft import Aircraft
    feed.aircraft = {"abc123": Aircraft(hex="abc123", lat=51.0, lon=-0.1)}
    feed._notified_military.add("abc123")
    feed._notified_emergency.add("abc123")
    feed._emergency_started_at["abc123"] = time.time() - 30
    # Pretend the last good poll was over a minute ago → sustained outage.
    feed._last_any_feed_ok_at = time.time() - 120

    asyncio.run(feed._poll_once())

    assert feed.aircraft == {}
    assert not feed._notified_military
    assert not feed._notified_emergency
    assert feed.connection_state == "error"


def test_stale_feed_status_pruned(temp_db, monkeypatch):
    feed = _make_feed(monkeypatch, temp_db)
    # A removed extra feed left a stale ok:True row that would otherwise mask the
    # outage of the feeds that are actually configured now.
    feed.feed_status["old_extra"] = {"kind": "tar1090", "ok": True}
    feed._last_any_feed_ok_at = time.time()

    asyncio.run(feed._poll_once())

    assert "old_extra" not in feed.feed_status
    assert "primary" in feed.feed_status   # the configured global feed


def _flicker_feed(monkeypatch):
    """Feed whose (healthy) upstream returns whatever `rows` currently holds."""
    from app.services.feed import FeedService
    from app.services import events_bus, webhooks
    feed = FeedService()
    rows = []

    async def _ok(name, url, *, kind):
        feed.feed_status[name] = {"kind": kind, "url": url, "ok": True, "rows": len(rows)}
        return [dict(r) for r in rows]
    monkeypatch.setattr(feed, "_fetch_one", _ok)
    hooks, bus = [], []
    monkeypatch.setattr(webhooks, "fan_out", lambda kind, ac: hooks.append(kind))
    real_publish = events_bus.publish
    monkeypatch.setattr(events_bus, "publish",
                        lambda kind, **kw: (bus.append(kind), real_publish(kind, **kw))[1])
    return feed, rows, hooks, bus


MIL = {"hex": "ae07db", "flight": "RCH688", "lat": 51.1, "lon": -1.0, "alt_baro": 30000}
EMG = {"hex": "400aaa", "flight": "BAW9", "lat": 51.2, "lon": -1.1, "alt_baro": 8000, "squawk": "7700"}


def test_single_missed_poll_does_not_refire_alerts(temp_db, monkeypatch):
    # A fringe contact dropping out for one poll used to re-arm the de-dup, so it
    # re-alerted (webhook + SSE + event row) the moment it came back.
    from app.services import events as events_store
    feed, rows, hooks, bus = _flicker_feed(monkeypatch)
    rows[:] = [MIL, EMG]
    asyncio.run(feed._poll_once())
    rows[:] = []                       # both flicker out for one poll
    asyncio.run(feed._poll_once())
    rows[:] = [MIL, EMG]               # ...and come back
    asyncio.run(feed._poll_once())

    assert hooks.count("military") == 1 and hooks.count("emergency") == 1
    assert len(events_store.recent_events(kind="military")) == 1
    assert len(events_store.recent_events(kind="emergency")) == 1
    assert "emergency_resolved" not in bus    # a blip isn't "coverage lost"


def test_rearms_after_grace_period(temp_db, monkeypatch):
    from app.services.feed import REARM_GRACE_S
    feed, rows, hooks, bus = _flicker_feed(monkeypatch)
    rows[:] = [MIL, EMG]
    asyncio.run(feed._poll_once())
    rows[:] = []
    # Pretend both were last seen longer ago than the grace window.
    for h in ("ae07db", "400aaa"):
        feed._notified_last_seen[h] = time.time() - REARM_GRACE_S - 5
    asyncio.run(feed._poll_once())
    assert not feed._notified_military and not feed._notified_emergency
    assert bus.count("emergency_resolved") == 1
    assert not feed._notified_last_seen      # bookkeeping pruned with the re-arm
    rows[:] = [MIL, EMG]
    asyncio.run(feed._poll_once())
    assert hooks.count("military") == 2 and hooks.count("emergency") == 2
