from __future__ import annotations

import asyncio
import logging
from urllib.parse import urlparse

from fastapi import APIRouter, WebSocket, WebSocketDisconnect, status

from ..services.feed import feed_service


log = logging.getLogger("piscope.ws")

router = APIRouter()


def _origin_is_same_host(ws: WebSocket) -> bool:
    """Reject cross-origin WS connections to stop random websites streaming the user's feed.

    We compare the request's Origin header host against the Host header. If Origin is absent
    (some non-browser clients), we allow the connection — Origin is only sent by browsers, and
    the threat model is hostile browser tabs."""
    origin = ws.headers.get("origin")
    if not origin:
        return True
    try:
        origin_host = urlparse(origin).netloc.lower()
    except Exception:
        return False
    host = (ws.headers.get("host") or "").lower()
    return origin_host == host


@router.websocket("/ws")
async def aircraft_ws(ws: WebSocket) -> None:
    if not _origin_is_same_host(ws):
        log.warning("Rejected cross-origin WS connect from %s", ws.headers.get("origin"))
        await ws.close(code=status.WS_1008_POLICY_VIOLATION)
        return
    await ws.accept()
    queue = feed_service.subscribe()
    try:
        # Send the latest snapshot immediately so the UI doesn't have to wait
        # for the next poll cycle.
        await ws.send_json(feed_service.snapshot())
        while True:
            try:
                payload = await asyncio.wait_for(queue.get(), timeout=30.0)
            except asyncio.TimeoutError:
                # Send a heartbeat so reverse proxies don't kill the connection.
                await ws.send_json({"type": "ping"})
                continue
            await ws.send_json(payload)
    except WebSocketDisconnect:
        pass
    except Exception as exc:
        log.warning("websocket loop ended: %s", exc)
    finally:
        feed_service.unsubscribe(queue)
        try:
            await ws.close()
        except Exception:
            pass
