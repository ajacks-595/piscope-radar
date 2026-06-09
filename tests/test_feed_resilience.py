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
