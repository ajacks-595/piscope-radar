"""In-process pub-sub broker for significant aircraft events (iter 9.3).

`feed.py` publishes events here whenever it detects an emergency squawk,
military contact, watchlist hit, or rare-type sighting. The
`/api/dashboard/events` SSE endpoint subscribes per-client, optionally
filters by observer location + radius, and streams matching events to the
HTTP response.

State is in-process and ephemeral — events are lost on restart. A bounded
ring buffer keeps the last N events so SSE clients reconnecting with a
`Last-Event-ID` header can replay anything they missed during a brief
disconnect. Anything older than the ring buffer is just gone; clients
should treat reconnect-without-replay as "resync via /api/dashboard/summary".
"""
from __future__ import annotations

import asyncio
import logging
import time
from collections import deque
from dataclasses import dataclass, field
from typing import Any, AsyncIterator, Optional


log = logging.getLogger("piscope.events_bus")


# Ring buffer size. 500 events @ a few KB each = ~1 MB worst case, plenty
# of headroom for a ~30 s reconnect window even during a busy event spike.
_RING_SIZE = 500


@dataclass
class BusEvent:
    id: int
    ts: float
    kind: str          # 'emergency' | 'emergency_resolved' | 'military' | 'watchlist' | 'rare'
    hex: str
    lat: Optional[float]
    lon: Optional[float]
    data: dict[str, Any] = field(default_factory=dict)


_next_id = 1
_ring: deque[BusEvent] = deque(maxlen=_RING_SIZE)
_subscribers: list[asyncio.Queue[BusEvent]] = []


def publish(kind: str, *, hex: str, lat: Optional[float] = None,
            lon: Optional[float] = None, data: Optional[dict[str, Any]] = None) -> BusEvent:
    """Append an event to the ring buffer and notify every live subscriber.
    Called from `feed.py`'s event-detection paths. Backpressure: if a
    subscriber's queue is full, the event is *dropped for that subscriber*
    rather than blocking the poll loop — better to skip than stall the feed."""
    global _next_id
    ev = BusEvent(
        id=_next_id, ts=time.time(), kind=kind, hex=hex, lat=lat, lon=lon,
        data=data or {},
    )
    _next_id += 1
    _ring.append(ev)
    for q in list(_subscribers):
        try:
            q.put_nowait(ev)
        except asyncio.QueueFull:
            log.warning("bus subscriber queue full — dropping %s event for one client", kind)
    return ev


async def subscribe(start_after_id: int = 0) -> AsyncIterator[BusEvent]:
    """Async iterator: yield missed events from the ring buffer first (per
    Last-Event-ID semantics), then yield each live event as it arrives. Cleans
    the subscriber list up on consumer-side cancellation.

    Ordering is deliberate: we attach the live queue BEFORE snapshotting the
    ring. If we drained the ring first (as this used to), an event published in
    the gap between the drain and the attach would land in neither and be lost.
    Attaching first means every post-attach event reaches the queue; we then
    replay the ring and de-dup by id so an event that lands in both the ring
    snapshot and the queue is yielded exactly once."""
    q: asyncio.Queue[BusEvent] = asyncio.Queue(maxsize=128)
    _subscribers.append(q)
    try:
        # Replay the ring (everything newer than the client's Last-Event-ID),
        # tracking the highest id we replay so the same event arriving on the
        # queue can be dropped as a duplicate.
        replayed_through = start_after_id
        for ev in list(_ring):
            if ev.id > start_after_id:
                replayed_through = max(replayed_through, ev.id)
                yield ev
        while True:
            ev = await q.get()
            if ev.id <= replayed_through:
                continue  # already delivered during ring replay
            yield ev
    finally:
        try:
            _subscribers.remove(q)
        except ValueError:
            pass


def subscriber_count() -> int:
    return len(_subscribers)


def ring_size() -> int:
    return len(_ring)


def latest_event_id() -> int:
    return _next_id - 1
