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
from typing import Any, Optional


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


class Subscription:
    """A live subscription to the bus. Plain object, NOT an async generator —
    that distinction is load-bearing (iteration 13). The SSE consumer races
    each `next()` against a 25 s heartbeat with `asyncio.wait_for`, which
    cancels the pending awaitable on timeout. When `subscribe()` was an async
    generator, that cancellation propagated into the generator's suspended
    `await q.get()`, ran its `finally`, and CLOSED the generator — so on a
    quiet feed (the normal case, since aircraft events are sparse) the stream
    tore itself down on the very first 25 s heartbeat and the client
    reconnected every 25 s, re-replaying the whole ring each time. Cancelling
    a bare `queue.get()` future leaves the subscription untouched, so the
    heartbeat now does what it's supposed to: hold the connection open.

    Replay-then-live ordering matches the old generator: the queue is attached
    BEFORE the ring is snapshotted, so an event published in the attach gap
    lands on the queue; ring events already delivered are de-duped by id.
    Always call `close()` (the SSE handler does so in a `finally`)."""

    __slots__ = ("_q", "_replay", "_replayed_through", "_closed")

    def __init__(self, start_after_id: int = 0) -> None:
        self._q: asyncio.Queue[BusEvent] = asyncio.Queue(maxsize=128)
        _subscribers.append(self._q)
        self._replay = [ev for ev in list(_ring) if ev.id > start_after_id]
        self._replayed_through = max(
            [start_after_id] + [ev.id for ev in self._replay])
        self._closed = False

    async def next(self) -> BusEvent:
        """Next event — replayed ring events first, then live ones. Awaits the
        queue when nothing is buffered; raises CancelledError if the awaiting
        task is cancelled (the subscription survives — call next() again)."""
        if self._replay:
            return self._replay.pop(0)
        while True:
            ev = await self._q.get()
            if ev.id > self._replayed_through:
                return ev   # else already delivered during ring replay; skip

    def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        try:
            _subscribers.remove(self._q)
        except ValueError:
            pass


def subscribe(start_after_id: int = 0) -> Subscription:
    """Attach a live subscription replaying everything after `start_after_id`."""
    return Subscription(start_after_id)


def subscriber_count() -> int:
    return len(_subscribers)


def ring_size() -> int:
    return len(_ring)


def latest_event_id() -> int:
    return _next_id - 1
