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
        agen = bus.subscribe(start_after_id=after_id)
        try:
            async for ev in agen:
                out.append(ev)
                if len(out) >= (bus.latest_event_id() - after_id):
                    break
        finally:
            await agen.aclose()
        return out

    # Reconnect with Last-Event-ID = e1.id → should replay e2 + e3 only.
    got = asyncio.run(drain(e1.id))
    assert [e.id for e in got] == [e2.id, e2.id + 1]


def test_subscriber_count_tracks_lifecycle():
    bus = _reset_bus()

    async def run():
        assert bus.subscriber_count() == 0
        agen = bus.subscribe(start_after_id=0)
        # Drive past the (empty) replay so the queue subscriber attaches.
        task = asyncio.ensure_future(agen.__anext__())
        await asyncio.sleep(0.05)
        count_while_subscribed = bus.subscriber_count()
        task.cancel()
        try:
            await task
        except (asyncio.CancelledError, StopAsyncIteration):
            pass
        await agen.aclose()
        return count_while_subscribed

    assert asyncio.run(run()) == 1
    assert bus.subscriber_count() == 0
