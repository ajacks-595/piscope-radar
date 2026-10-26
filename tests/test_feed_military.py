"""Live military classification: the feed must flag military aircraft by the same
hex-range / callsign rules the notable panel uses, not only by the readsb dbFlags
bit (which a receiver without an aircraft database never sets)."""
from __future__ import annotations

import asyncio
import sys
import pathlib

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))


ROWS = [
    # US DoD hex allocation, no dbFlags (the production local-tar1090 case).
    {"hex": "ae07db", "flight": "RCH688  ", "lat": 51.1, "lon": -1.0, "alt_baro": 30000, "t": "C17"},
    # Civil hex, military callsign prefix only.
    {"hex": "400abc", "flight": "RRR4567", "lat": 51.2, "lon": -1.1, "alt_baro": 20000},
    # Explicit dbFlags military bit.
    {"hex": "3f0001", "flight": "GAF123", "lat": 51.3, "lon": -1.2, "alt_baro": 15000, "dbFlags": 1},
    # Plain airliner.
    {"hex": "400def", "flight": "BAW123", "lat": 51.4, "lon": -1.3, "alt_baro": 35000, "t": "A320"},
]


def _feed_with_rows(monkeypatch, rows):
    from app.services.feed import FeedService
    from app.services import webhooks
    feed = FeedService()

    async def _ok(name, url, *, kind):
        feed.feed_status[name] = {"kind": kind, "url": url, "ok": True, "rows": len(rows)}
        return [dict(r) for r in rows]
    monkeypatch.setattr(feed, "_fetch_one", _ok)
    fired = []
    monkeypatch.setattr(webhooks, "fan_out", lambda kind, ac: fired.append((kind, ac.get("hex"))))
    return feed, fired


def test_rule_based_military_fires_live_events(temp_db, monkeypatch):
    from app.services import events as events_store
    feed, fired = _feed_with_rows(monkeypatch, ROWS)
    asyncio.run(feed._poll_once())

    mil = {ac.hex: ac for ac in feed.aircraft.values() if ac.military}
    assert set(mil) == {"ae07db", "400abc", "3f0001"}
    assert mil["ae07db"].military_reason == "United States (DoD allocation)"
    assert "Reach" not in mil["ae07db"].military_reason        # hex range wins
    assert mil["400abc"].military_reason == "Royal Air Force (Ascot)"
    assert mil["3f0001"].military_reason == "Military (aircraft DB flag)"
    assert not feed.aircraft["400def"].military

    logged = {e["hex"] for e in events_store.recent_events(kind="military")}
    assert logged == {"ae07db", "400abc", "3f0001"}
    assert {h for k, h in fired if k == "military"} == {"ae07db", "400abc", "3f0001"}
    # Reason rides on the wire payload for the MIL badge tooltip.
    assert feed.aircraft["400abc"].to_json()["military_reason"] == "Royal Air Force (Ascot)"


def test_ledger_keeps_raw_dbflag(temp_db, monkeypatch):
    # The analytics ledger stores only the dbFlags bit so the notable panel's
    # "aircraft DB flag" reason stays truthful; rule matches are re-derived there.
    import sqlite3
    from app.services import settings as settings_store
    feed, _ = _feed_with_rows(monkeypatch, ROWS)
    asyncio.run(feed._poll_once())
    with settings_store.batch() as conn:
        feed._analytics.flush(conn, force=True)
    db = sqlite3.connect(temp_db)
    flags = dict(db.execute("SELECT hex, military FROM aircraft_sightings").fetchall())
    assert flags == {"ae07db": 0, "400abc": 0, "3f0001": 1, "400def": 0}
