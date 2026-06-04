"""Tiny in-process rate limiter for the handful of unauthenticated endpoints
that cost real CPU or real money.

Why *global* (process-wide), not per-IP: PiScope sits behind lighttpd, which
proxies every LAN client from 127.0.0.1. A per-IP limiter would therefore see
one client and throttle everyone collectively (or have to trust a spoofable
X-Forwarded-For). A global cap is both honest and proxy-safe — it bounds total
work regardless of who's asking. LAN-only, no-auth posture means this is
abuse-mitigation, not a hard security boundary, so the defaults are generous:
normal interactive use (a human clicking "Explain", a dashboard polling every
5 s) never trips it; only a scripted flood does.

Sliding window via a per-bucket timestamp deque. No external deps, no threads —
asyncio serialises access, and the worst case (a stray call from a worker
thread) only ever miscounts, never corrupts.
"""
from __future__ import annotations

import time
from collections import deque
from typing import Deque, Dict, Tuple

# bucket name -> deque of recent allow() timestamps within the window.
_BUCKETS: Dict[str, Deque[float]] = {}


def allow(name: str, *, limit: int, window_s: float) -> bool:
    """Record an attempt against bucket ``name`` and report whether it's allowed.

    Returns True (and counts the hit) if fewer than ``limit`` hits have landed in
    the trailing ``window_s`` seconds; returns False without counting once the
    window is full, so a sustained flood stays capped at ``limit`` per window."""
    now = time.time()
    dq = _BUCKETS.get(name)
    if dq is None:
        dq = deque()
        _BUCKETS[name] = dq
    cutoff = now - window_s
    while dq and dq[0] < cutoff:
        dq.popleft()
    if len(dq) >= limit:
        return False
    dq.append(now)
    return True


def reset() -> None:
    """Clear all buckets. Used by the test fixtures for isolation."""
    _BUCKETS.clear()


def _bucket_size(name: str) -> int:
    """Current in-window count for a bucket (diagnostics / tests)."""
    return len(_BUCKETS.get(name, ()))
