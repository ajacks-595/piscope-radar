"""In-process pub-sub event bus: publish, ring buffer, Last-Event-ID replay."""
from __future__ import annotations

import asyncio
import sys
import pathlib

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))


def _reset_bus():
    from app.services import events_bus
    events_bus._ring.clear()
    events_bus._subscribers.clear()
    events_bus._next_id = 1
    return events_bus


def test_publish_increments_id_and_ring():
    bus = _reset_bus()
    e1 = bus.publish("emergency", hex="a1", lat=1.0, lon=2.0, data={"squawk": "7700"})
    e2 = bus.publish("military", hex="a2")
    assert e1.id == 1 and e2.id == 2
    assert bus.latest_event_id() == 2
    assert bus.ring_size() == 2
    assert e1.kind == "emergency" and e1.data["squawk"] == "7700"


def test_subscribe_replays_after_last_event_id():
    bus = _reset_bus()
    e1 = bus.publish("emergency", hex="a1")
    e2 = bus.publish("military", hex="a2")
    bus.publish("watchlist", hex="a3")

    async def drain(after_id):
        out = []
        sub = bus.subscribe(start_after_id=after_id)
        try:
            for _ in range(bus.latest_event_id() - after_id):
                out.append(await sub.next())
        finally:
            sub.close()
        return out

    # Reconnect with Last-Event-ID = e1.id → should replay e2 + e3 only.
    got = asyncio.run(drain(e1.id))
    assert [e.id for e in got] == [e2.id, e2.id + 1]


def test_subscriber_count_tracks_lifecycle():
    bus = _reset_bus()

    async def run():
        assert bus.subscriber_count() == 0
        sub = bus.subscribe(start_after_id=0)
        count_while_subscribed = bus.subscriber_count()
        sub.close()
        return count_while_subscribed

    assert asyncio.run(run()) == 1
    assert bus.subscriber_count() == 0


def test_cancelling_next_does_not_tear_down_subscription():
    """Regression (iter 13): the SSE consumer races sub.next() against a 25 s
    heartbeat with wait_for, which CANCELS the pending next() on timeout. That
    must NOT close the subscription — otherwise a quiet feed (the normal case)
    tore the stream down on every heartbeat. Here: cancel a pending next(), then
    publish and confirm the SAME subscription still delivers the event."""
    bus = _reset_bus()

    async def run():
        sub = bus.subscribe(start_after_id=0)
        # No events yet → next() blocks. Race it against a short timeout and let
        # the timeout win, exactly as the heartbeat does.
        try:
            await asyncio.wait_for(sub.next(), timeout=0.05)
        except asyncio.TimeoutError:
            pass
        assert bus.subscriber_count() == 1, "subscription was torn down by the cancel"
        # The subscription is still live: a freshly published event arrives.
        bus.publish("emergency", hex="zz", data={"squawk": "7700"})
        ev = await asyncio.wait_for(sub.next(), timeout=1.0)
        sub.close()
        return ev

    ev = asyncio.run(run())
    assert ev.hex == "zz" and ev.data["squawk"] == "7700"
    assert bus.subscriber_count() == 0
