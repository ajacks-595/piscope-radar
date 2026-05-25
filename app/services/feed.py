from __future__ import annotations

import asyncio
import json
import logging
import time
from collections import deque
from datetime import datetime, timezone
from typing import Any, Optional

import httpx

from ..models.aircraft import Aircraft, aircraft_from_wire, haversine_nm
from . import backups as backups_store
from . import events as events_store
from . import events_bus
from . import hexdb as hexdb_service
from . import insights as insights_store
from . import records as records_store
from . import settings as settings_store
from . import webhooks as webhooks_service
from ._http import validate_external_url, _BLOCKED_METADATA_HOSTS  # SSRF guard (shared)  # noqa: F401


log = logging.getLogger("piscope.feed")

ADSB_LOL_URL = "https://api.adsb.lol/v2/lat/{lat}/lon/{lon}/dist/{nm}"
# NB: validate_external_url is imported from _http above (shared with webhooks.py
# to avoid an import cycle). feed's callers use the default resolve=False — the
# feed URL is admin-set and polled every cycle, so no per-poll DNS lookup.


class FeedService:
    """Polls a tar1090 (or adsb.lol) JSON feed and keeps an in-memory aircraft store.

    Subscribers (e.g. the WebSocket endpoint) receive a snapshot after each successful poll
    via async queues registered through `subscribe()`.
    """

    def __init__(self) -> None:
        self.aircraft: dict[str, Aircraft] = {}
        self.trails: dict[str, deque[tuple[float, float, float]]] = {}  # hex → deque of (lat, lon, ts)
        self.receiver: Optional[dict[str, float]] = None
        self.connection_state: str = "idle"            # idle | polling | error
        self.last_error: Optional[str] = None
        self.total_messages: int = 0
        self.last_poll_at: float = 0.0
        self.feed_now: float = 0.0
        # Receiver health counters surfaced through /api/health and the topbar.
        self.start_time: float = time.time()
        self.poll_count: int = 0
        self.error_count: int = 0
        self.last_poll_duration_ms: float = 0.0
        # Watchdog: track the most recent successful-feed timestamp so we can detect an outage
        # and fire a `feed_down` webhook once (then re-arm + fire `feed_recovered` when polls succeed).
        # `_watchdog_armed=True` means "ready to fire feed_down on the next sustained outage".
        self._last_any_feed_ok_at: float = time.time()
        self._watchdog_armed: bool = True
        self._outage_notified: bool = False
        # Component-level perf so we can tell HTTP-fetch wait from SQL/CPU work.
        self.last_fetch_ms: float = 0.0
        self.last_sql_ms: float = 0.0
        self.last_process_ms: float = 0.0
        # Per-feed status (multi-receiver support). Keyed by feed-source name.
        self.feed_status: dict[str, dict[str, Any]] = {}

        # Daily aggregates. Reset when the date rolls over.
        self._daily_date: str = datetime.now(timezone.utc).strftime("%Y-%m-%d")
        self._daily_unique: set[str] = set()
        self._daily_max_range: float = 0.0
        self._daily_emergencies: set[str] = set()
        self._daily_military: set[str] = set()

        # De-dup for event emission. Re-armed when an aircraft leaves coverage.
        self._notified_military: set[str] = set()
        self._notified_emergency: set[str] = set()
        self._notified_watchlist: set[str] = set()
        # When an emergency squawk first appeared. Used to emit `emergency_resolved`
        # bus events with the squawk's lifetime when the aircraft drops the
        # emergency code (or leaves coverage).
        self._emergency_started_at: dict[str, float] = {}

        # Snapshot persistence cadence — every Nth poll write to feed_snapshots. Read from
        # settings on each poll so it can be retuned without a restart.
        self._snapshot_counter = 0
        self._next_prune_at = 0.0
        # Trail deltas: per-poll map of hex → newest trail point (only set if the position
        # changed this poll). Frontends append these to their local trail history; the
        # full history is sent only on initial WS connect (see `snapshot()`).
        self._last_trail_appends: dict[str, tuple] = {}
        # Cache of decoded settings + the version that produced it; lets the poll loop
        # skip the per-cycle JSON merge when nothing changed.
        self._settings_cached: Optional[dict[str, Any]] = None
        self._settings_cache_version: int = -1

        self._subscribers: list[asyncio.Queue] = []
        self._task: Optional[asyncio.Task] = None
        self._enrich_task: Optional[asyncio.Task] = None
        self._stop = asyncio.Event()
        self._client: Optional[httpx.AsyncClient] = None

        # Hex IDs queued for hexdb type-enrichment. Some tar1090 deployments omit `t` for many
        # aircraft, so we backfill the ICAO type code from hexdb in the background.
        self._enrich_pending: set[str] = set()
        self._enriched_hexes: set[str] = set()
        # Map hex → ICAO type code we've discovered via hexdb. Lets subsequent polls reuse it.
        self._known_types: dict[str, str] = {}
        # Types discovered by the enrichment loop but not yet written to seen_types. Drained
        # into the poll batch so we don't open competing write transactions on SQLite.
        self._pending_type_records: list[str] = []

    # --- lifecycle -----------------------------------------------------------

    async def start(self) -> None:
        if self._task is not None:
            return
        self._client = httpx.AsyncClient(timeout=8.0)
        self._stop.clear()
        self._task = asyncio.create_task(self._run(), name="feed-poll-loop")
        self._enrich_task = asyncio.create_task(self._enrichment_loop(), name="feed-type-enrich")
        log.info("FeedService started")

    async def stop(self) -> None:
        self._stop.set()
        for t in (self._task, self._enrich_task):
            if t:
                t.cancel()
                try:
                    await t
                except (asyncio.CancelledError, Exception):
                    pass
        if self._client:
            await self._client.aclose()
        self._task = None
        self._enrich_task = None
        self._client = None

    async def _enrichment_loop(self) -> None:
        """Drain `_enrich_pending` slowly so we don't hammer hexdb. ~one lookup per 0.2 s."""
        while not self._stop.is_set():
            try:
                if self._enrich_pending:
                    hex_id = self._enrich_pending.pop()
                    self._enriched_hexes.add(hex_id)
                    try:
                        result = await hexdb_service.lookup(hex_id)
                    except Exception:
                        result = None
                    if result:
                        icao_type = (result.get("icao_type_code") or "").strip().upper()
                        if icao_type:
                            self._known_types[hex_id] = icao_type
                            # Defer the write — the next poll's batch will flush it. This avoids
                            # opening a competing SQLite write transaction every 0.2 s, which
                            # was causing the poll loop to block for the busy_timeout (5 s).
                            self._pending_type_records.append(icao_type)
                            # Backfill the live record so the next snapshot carries the type.
                            cur = self.aircraft.get(hex_id)
                            if cur and not cur.type_code:
                                cur.type_code = icao_type
                await asyncio.sleep(0.2)
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                log.warning("enrichment loop error: %s", exc)
                await asyncio.sleep(1.0)

    # --- pub/sub -------------------------------------------------------------

    def subscribe(self) -> asyncio.Queue:
        q: asyncio.Queue = asyncio.Queue(maxsize=4)
        self._subscribers.append(q)
        return q

    def unsubscribe(self, q: asyncio.Queue) -> None:
        if q in self._subscribers:
            self._subscribers.remove(q)

    def _broadcast(self, payload: dict[str, Any]) -> None:
        # Pre-serialise to JSON text once per broadcast instead of once per subscriber.
        # Before iter-10 the WS endpoint called `ws.send_json(payload)` which did its
        # own json.dumps for every connected client — `iterencode` showed up as ~6%
        # of active CPU in the profile. With one dumps here, each subscriber just
        # ships the same string via `ws.send_text`.
        try:
            text = json.dumps(payload, default=str, separators=(",", ":"))
        except Exception as exc:
            log.warning("broadcast serialise failed: %s", exc)
            return
        for q in list(self._subscribers):
            try:
                if q.full():
                    # Drop the oldest queued snapshot if a client is slow — we don't want unbounded growth.
                    try:
                        q.get_nowait()
                    except asyncio.QueueEmpty:
                        pass
                q.put_nowait(text)
            except Exception as exc:
                log.warning("broadcast to subscriber failed: %s", exc)

    # --- snapshot for new connections ---------------------------------------

    def snapshot(self) -> dict[str, Any]:
        """Full snapshot — sent on initial WS connect and from the REST fallback. The live
        broadcast (`broadcast_payload`) is much smaller because it only ships new trail points
        rather than the full trail history per aircraft."""
        return {
            "type": "aircraft_update",
            "now": self.feed_now,
            "polled_at": self.last_poll_at,
            "aircraft": [a.to_json() for a in self.aircraft.values()],
            "receiver": self.receiver,
            "connection_state": self.connection_state,
            "last_error": self.last_error,
            "total_messages": self.total_messages,
            "trails": {hex_id: list(pts) for hex_id, pts in self.trails.items()},
            "trails_full": True,    # tells the client to replace its trail state
            "feeds": self.feed_status,
        }

    def broadcast_payload(self) -> dict[str, Any]:
        """Slim payload for WS broadcasts. Sends just the most recently appended trail
        point per aircraft as `trail_appends` instead of the full 30-point history — the
        client merges these into its local trail state. Saves ~70% of WS bandwidth.
        """
        return {
            "type": "aircraft_update",
            "now": self.feed_now,
            "polled_at": self.last_poll_at,
            "aircraft": [a.to_json() for a in self.aircraft.values()],
            "receiver": self.receiver,
            "connection_state": self.connection_state,
            "last_error": self.last_error,
            "total_messages": self.total_messages,
            "trail_appends": self._last_trail_appends,
            "feeds": self.feed_status,
        }

    def health(self) -> dict[str, Any]:
        uptime = time.time() - self.start_time
        return {
            "uptime_seconds": round(uptime, 1),
            "polls": self.poll_count,
            "errors": self.error_count,
            "last_poll_duration_ms": round(self.last_poll_duration_ms, 1),
            "last_fetch_ms": round(self.last_fetch_ms, 1),
            "last_sql_ms": round(self.last_sql_ms, 1),
            "last_process_ms": round(self.last_process_ms, 1),
            "polls_per_min": round((self.poll_count / max(1, uptime)) * 60.0, 2),
            "connection_state": self.connection_state,
            "last_error": self.last_error,
            "receiver": self.receiver,
            "feeds": self.feed_status,
            "subscriber_count": len(self._subscribers),
            "aircraft_count": len(self.aircraft),
            "trails_count": len(self.trails),
            "daily": {
                "date": self._daily_date,
                "unique_aircraft": len(self._daily_unique),
                "max_range_nm": round(self._daily_max_range, 1),
                "emergencies": len(self._daily_emergencies),
                "military_seen": len(self._daily_military),
            },
        }

    # --- poll loop -----------------------------------------------------------

    async def _run(self) -> None:
        # Probe receiver position once at startup (best effort).
        await self._try_load_receiver()
        while not self._stop.is_set():
            try:
                await self._poll_once()
            except Exception as exc:
                log.exception("poll cycle failed: %s", exc)
                self.connection_state = "error"
                self.last_error = str(exc)
                self.error_count += 1
            interval = max(1, int(settings_store.get("poll_interval") or 2))
            try:
                await asyncio.wait_for(self._stop.wait(), timeout=interval)
            except asyncio.TimeoutError:
                pass

    async def _try_load_receiver(self) -> None:
        raw = settings_store.get("tar1090_base_url") or ""
        if not raw:
            # Use configured receiver lat/lon if present
            lat = settings_store.get("receiver_lat")
            lon = settings_store.get("receiver_lon")
            if lat and lon:
                self.receiver = {"lat": float(lat), "lon": float(lon)}
            return
        try:
            base = validate_external_url(raw).rstrip("/")
        except ValueError as exc:
            log.warning("Refusing to probe receiver.json: %s", exc)
            return
        try:
            r = await self._client.get(f"{base}/data/receiver.json")
            r.raise_for_status()
            data = r.json()
            lat = data.get("lat")
            lon = data.get("lon")
            if isinstance(lat, (int, float)) and isinstance(lon, (int, float)):
                self.receiver = {"lat": float(lat), "lon": float(lon)}
                settings_store.set_many({"receiver_lat": float(lat), "receiver_lon": float(lon)})
        except Exception as exc:
            log.info("receiver.json probe failed (will rely on configured location): %s", exc)

    async def _fetch_one(self, name: str, url: str, *, kind: str) -> list[dict[str, Any]]:
        """Fetch a single source. Tracks per-feed status."""
        started = time.time()
        try:
            r = await self._client.get(url, headers={"User-Agent": "PiScope-Radar/1.0"})
            r.raise_for_status()
            data = r.json()
            rows = (data.get("aircraft") if kind == "tar1090" else (data.get("ac") or data.get("aircraft"))) or []
            self.feed_status[name] = {
                "kind": kind, "url": url, "ok": True, "rows": len(rows),
                "duration_ms": round((time.time() - started) * 1000, 1), "last_ok_at": time.time(),
            }
            return rows
        except Exception as exc:
            self.feed_status[name] = {
                "kind": kind, "url": url, "ok": False, "error": type(exc).__name__,
                "duration_ms": round((time.time() - started) * 1000, 1),
                "last_error_at": time.time(),
            }
            log.warning("feed %s fetch failed: %s", name, exc)
            return []

    async def _poll_once(self) -> None:
        self.connection_state = "polling"
        poll_start = time.time()
        # Reuse the cached settings dict when nothing has been written since the last poll.
        v = settings_store.cache_version()
        if v != self._settings_cache_version or self._settings_cached is None:
            self._settings_cached = settings_store.get_all(redact=False)
            self._settings_cache_version = v
        s = self._settings_cached
        feed_mode = s.get("feed_mode") or "global"
        tar_base = (s.get("tar1090_base_url") or "").strip()

        # Build the list of feeds to poll this cycle. The primary depends on feed_mode; any
        # extra feeds from the settings are merged on top (latest poll wins per hex).
        feeds: list[tuple[str, str, str]] = []   # (name, url, kind)
        if feed_mode == "local" and tar_base:
            try:
                base = validate_external_url(tar_base).rstrip("/")
            except ValueError as exc:
                raise RuntimeError(f"Invalid tar1090 URL: {exc}") from exc
            feeds.append(("primary", f"{base}/data/aircraft.json", "tar1090"))
        else:
            # Prefer the user's receiver coords as the global feed centre — they nearly always
            # want adsb.lol traffic around their actual location, not some unrelated default.
            # The dedicated global_center_* fields are only used when receiver coords aren't set.
            lat = (s.get("global_center_lat") if s.get("global_center_lat") not in (None, "")
                   else s.get("receiver_lat") if s.get("receiver_lat") not in (None, "")
                   else 51.5)
            lon = (s.get("global_center_lon") if s.get("global_center_lon") not in (None, "")
                   else s.get("receiver_lon") if s.get("receiver_lon") not in (None, "")
                   else -0.1)
            lat, lon = float(lat), float(lon)
            nm = int(s.get("global_radius_nm") or 250)
            feeds.append(("primary", ADSB_LOL_URL.format(lat=lat, lon=lon, nm=nm), "adsblol"))
            if self.receiver is None:
                self.receiver = {"lat": lat, "lon": lon}

        try:
            extra = json.loads(s.get("extra_feeds_json") or "[]")
        except Exception:
            extra = []
        for feed in (extra if isinstance(extra, list) else []):
            try:
                name = str(feed.get("name") or "extra")
                raw = feed.get("url") or ""
                base = validate_external_url(raw).rstrip("/")
                feeds.append((name, f"{base}/data/aircraft.json", "tar1090"))
            except Exception as exc:
                log.info("ignoring invalid extra feed: %s", exc)

        # Poll all feeds in parallel.
        fetch_started = time.time()
        results = await asyncio.gather(
            *[self._fetch_one(name, url, kind=kind) for (name, url, kind) in feeds],
            return_exceptions=False,
        )
        self.last_fetch_ms = (time.time() - fetch_started) * 1000

        now_ts = time.time()
        self.feed_now = now_ts
        self.last_poll_at = now_ts
        # Aggregate row count across feeds for the topbar "total" indicator.
        all_rows: list[dict[str, Any]] = []
        for rows in results:
            all_rows.extend(rows)
        self.total_messages = len(all_rows)

        new_store: dict[str, Aircraft] = {}
        receiver_lat = self.receiver["lat"] if self.receiver else None
        receiver_lon = self.receiver["lon"] if self.receiver else None
        trail_len = int(s.get("trail_length") or 30)

        watchlist = events_store.parse_watchlist(s.get("watchlist") or "")

        # Open ONE SQLite connection for the whole poll so polar / heatmap / records / types
        # writes all share a single transaction. Without this we'd commit per aircraft
        # (potentially thousands of fsyncs per minute on SD storage).
        poll_aircraft: list[Aircraft] = []
        new_appends: dict[str, tuple] = {}
        # Coalesce per-bucket heatmap counts so we issue one UPSERT per distinct bucket
        # at the end of the loop, not one per aircraft.
        heatmap_counts: dict[tuple[int, int], int] = {}
        # Coalesce per-type sightings (iter 10.3). Pre-iter-10 each aircraft caused
        # a SELECT + UPDATE — ~200 SQL ops/poll at 50 contacts. Now it's one
        # executemany per poll per distinct type. The in-memory `known_types` set
        # tells us whether a type is new (for "rare" event firing) without a DB hit.
        type_counts_this_poll: dict[str, int] = {}
        rare_fired_this_poll: set[str] = set()
        known_types = insights_store.ensure_known_types()
        sql_started = time.time()
        with settings_store.batch() as poll_conn:
            for row in all_rows:
                ac = aircraft_from_wire(row, now_ts)
                if not ac:
                    continue
                poll_aircraft.append(ac)
                # If two feeds report the same hex, the later one wins. Preserve the position
                # iff the newer report omits lat/lon (a mode-S–only echo over an ADS-B fix).
                existing = new_store.get(ac.hex)
                if existing is not None and ac.lat is None:
                    ac.lat = existing.lat
                    ac.lon = existing.lon
                if ac.distance_nm is None and ac.lat is not None and ac.lon is not None \
                        and receiver_lat is not None and receiver_lon is not None:
                    ac.distance_nm = haversine_nm(receiver_lat, receiver_lon, ac.lat, ac.lon)
                new_store[ac.hex] = ac

                if ac.lat is not None and ac.lon is not None:
                    trail = self.trails.get(ac.hex)
                    if trail is None:
                        trail = deque(maxlen=trail_len)
                        self.trails[ac.hex] = trail
                    if not trail or trail[-1][0] != ac.lat or trail[-1][1] != ac.lon:
                        # 5-tuple: (lat, lon, ts, altitude_baro_or_None, ground_speed_or_None)
                        point = (ac.lat, ac.lon, now_ts, ac.altitude_baro, ac.ground_speed)
                        trail.append(point)
                        # Remember this point so the broadcast can ship it as a delta.
                        new_appends[ac.hex] = point
                    if trail.maxlen != trail_len:
                        self.trails[ac.hex] = deque(trail, maxlen=trail_len)

                # Daily aggregates
                self._daily_unique.add(ac.hex)
                if ac.distance_nm is not None and ac.distance_nm > self._daily_max_range:
                    self._daily_max_range = ac.distance_nm

                # Insights — polar coverage + heatmap + type ledger (rare alerts) + all-time records.
                # All four share the same SQLite connection so the per-aircraft work is one row
                # in the eventual single transaction (committed at the end of the poll).
                if ac.lat is not None and ac.lon is not None and receiver_lat is not None and receiver_lon is not None:
                    if ac.distance_nm is not None:
                        insights_store.update_polar(receiver_lat, receiver_lon,
                                                    hex_id=ac.hex, lat=ac.lat, lon=ac.lon,
                                                    distance_nm=ac.distance_nm,
                                                    conn=poll_conn)
                    bucket = insights_store.heatmap_bucket(ac.lat, ac.lon)
                    heatmap_counts[bucket] = heatmap_counts.get(bucket, 0) + 1
                # Records updated in one batch at the end of the loop (O(5) DB ops total).
                # If the wire data didn't carry a type code, but we previously enriched it via hexdb,
                # restore that value before the snapshot ships.
                if not ac.type_code and ac.hex in self._known_types:
                    ac.type_code = self._known_types[ac.hex]

                is_new_type = False
                if ac.type_code:
                    code = ac.type_code.upper().strip()
                    type_counts_this_poll[code] = type_counts_this_poll.get(code, 0) + 1
                    # New = type isn't in the persisted known set AND we haven't
                    # already fired "rare" for it earlier in this same poll.
                    if code not in known_types and code not in rare_fired_this_poll:
                        is_new_type = True
                        rare_fired_this_poll.add(code)
                elif ac.hex not in self._enriched_hexes:
                    # Queue this hex for background type enrichment via hexdb.
                    self._enrich_pending.add(ac.hex)

                ac_dict = ac.to_json()

                # Event emission (de-duped — re-armed when aircraft leaves coverage below).
                # Critical: pass `conn=poll_conn` so the event write happens inside our batch
                # transaction. Opening a fresh connection here would deadlock against our own
                # write lock until busy_timeout (5 s) fires per event.
                def _fire(kind: str, payload: dict[str, Any]) -> None:
                    events_store.record_event(
                        kind, hex=ac.hex, callsign=ac.callsign, registration=ac.registration,
                        distance_nm=ac.distance_nm, payload=payload, conn=poll_conn,
                    )
                    webhooks_service.fan_out(kind, ac_dict)
                    # Live SSE channel for dashboards (iter 9.3). publish() is
                    # synchronous + non-blocking; failures are swallowed inside
                    # so the poll loop never trips on bus issues.
                    try:
                        events_bus.publish(
                            kind, hex=ac.hex, lat=ac.lat, lon=ac.lon,
                            data={
                                "callsign": ac.callsign,
                                "registration": ac.registration,
                                "type_code": ac.type_code,
                                "altitude_ft": ac.altitude_baro,
                                "distance_nm": ac.distance_nm,
                                **payload,
                            },
                        )
                    except Exception as exc:
                        log.warning("events_bus.publish failed for %s: %s", kind, exc)

                if ac.military and ac.hex not in self._notified_military:
                    self._notified_military.add(ac.hex)
                    self._daily_military.add(ac.hex)
                    _fire("military", {"type_code": ac.type_code, "altitude": ac.altitude_baro})
                if ac.is_emergency_squawk and ac.hex not in self._notified_emergency:
                    self._notified_emergency.add(ac.hex)
                    self._daily_emergencies.add(ac.hex)
                    self._emergency_started_at[ac.hex] = now_ts
                    _fire("emergency", {"squawk": ac.squawk, "altitude": ac.altitude_baro,
                                         "lat": ac.lat, "lon": ac.lon, "type_code": ac.type_code})
                elif (not ac.is_emergency_squawk) and ac.hex in self._notified_emergency:
                    # Squawk has reverted to a normal code while still in coverage.
                    started = self._emergency_started_at.pop(ac.hex, None)
                    duration = (now_ts - started) if started else None
                    self._notified_emergency.discard(ac.hex)
                    try:
                        events_bus.publish(
                            "emergency_resolved", hex=ac.hex, lat=ac.lat, lon=ac.lon,
                            data={
                                "callsign": ac.callsign,
                                "registration": ac.registration,
                                "type_code": ac.type_code,
                                "duration_s": round(duration, 1) if duration is not None else None,
                                "reason": "squawk_normalised",
                            },
                        )
                    except Exception as exc:
                        log.warning("emergency_resolved publish failed: %s", exc)
                if watchlist and ac.hex not in self._notified_watchlist:
                    tokens = {t.upper() for t in [ac.callsign, ac.registration, ac.hex] if t}
                    if tokens & set(watchlist):
                        self._notified_watchlist.add(ac.hex)
                        _fire("watchlist", {"type_code": ac.type_code, "altitude": ac.altitude_baro})
                if is_new_type:
                    # "rare" only fires after the type has had at least a few seconds to settle —
                    # avoids alerting on the very first poll of every fresh install.
                    if time.time() - self.start_time > 30:
                        _fire("rare", {"type_code": ac.type_code, "altitude": ac.altitude_baro})

            # End of for-loop. Push the per-poll bests for records + heatmap in single batches.
            records_store.update_records_bulk(poll_aircraft, conn=poll_conn)
            insights_store.flush_heatmap_batch(heatmap_counts, conn=poll_conn)
            # Drain anything the enrichment loop queued while this poll was building.
            # Each enrichment-discovered type counts as one sighting; merge into the
            # per-poll counts so flush_type_sightings handles it in the same batch.
            pending = list(self._pending_type_records)
            self._pending_type_records.clear()
            for type_code in pending:
                code = (type_code or "").upper().strip()
                if code:
                    type_counts_this_poll[code] = type_counts_this_poll.get(code, 0) + 1
            # One executemany per distinct type instead of two SQL ops per aircraft.
            insights_store.flush_type_sightings(type_counts_this_poll, conn=poll_conn)
        self.last_sql_ms = (time.time() - sql_started) * 1000

        # Trail GC + re-arm event de-dup once aircraft drop out of coverage for ≥120 s.
        for hex_id in list(self.trails.keys()):
            if hex_id not in new_store:
                last = self.trails[hex_id][-1] if self.trails[hex_id] else None
                last_ts = last[2] if last else 0
                if now_ts - last_ts > 120:
                    del self.trails[hex_id]
        for s_set in (self._notified_military, self._notified_emergency, self._notified_watchlist):
            for h in list(s_set):
                if h not in new_store:
                    # Coverage-loss resolves a still-active emergency. Other
                    # event kinds (military, watchlist) just re-arm silently.
                    if s_set is self._notified_emergency:
                        started = self._emergency_started_at.pop(h, None)
                        duration = (now_ts - started) if started else None
                        try:
                            events_bus.publish(
                                "emergency_resolved", hex=h, lat=None, lon=None,
                                data={
                                    "duration_s": round(duration, 1) if duration is not None else None,
                                    "reason": "coverage_lost",
                                },
                            )
                        except Exception as exc:
                            log.warning("emergency_resolved publish failed: %s", exc)
                    s_set.discard(h)

        # Roll the daily counters when the date changes.
        today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
        if today != self._daily_date:
            self._daily_date = today
            self._daily_unique.clear()
            self._daily_max_range = 0.0
            self._daily_emergencies.clear()
            self._daily_military.clear()

        self.aircraft = new_store
        self.connection_state = "polling" if any(f.get("ok") for f in self.feed_status.values()) else "error"
        self.last_error = None if self.connection_state == "polling" else "all feeds failed"
        self.poll_count += 1
        self.last_poll_duration_ms = (time.time() - poll_start) * 1000
        self.last_process_ms = max(0.0, self.last_poll_duration_ms - self.last_fetch_ms - self.last_sql_ms)
        self._check_watchdog(now_ts)

        # Persist & update daily stats every poll (cheap), then broadcast.
        events_store.update_daily_stats(
            unique_hexes_today=len(self._daily_unique),
            max_range_nm_today=self._daily_max_range,
            emergencies_today=len(self._daily_emergencies),
            military_today=len(self._daily_military),
        )

        # Stash the per-poll trail deltas so `broadcast_payload()` can ship them.
        self._last_trail_appends = new_appends

        snap = self.snapshot()
        snap_every = max(1, int(s.get("snapshot_every_n_polls") or 5))
        self._snapshot_counter += 1
        if self._snapshot_counter >= snap_every:
            self._snapshot_counter = 0
            events_store.record_snapshot(now_ts, snap)
        # Prune old snapshots every 5 minutes to keep DB bounded.
        if now_ts > self._next_prune_at:
            self._next_prune_at = now_ts + 300
            retention_s = float(s.get("replay_retention_minutes") or 60) * 60
            events_store.prune_old_snapshots(retention_s)
            # Daily backup check — only writes if the date has rolled over and `daily_backup_dir` is set.
            backups_store.maybe_run_daily()

        # Broadcast the slim payload (trail deltas only) — full snapshots only ship on initial
        # WS connect via the WS endpoint, or via the REST `/api/aircraft` fallback.
        self._broadcast(self.broadcast_payload())

    # --- watchdog ------------------------------------------------------------

    def _check_watchdog(self, now_ts: float) -> None:
        """Fire `feed_down` once after the configured outage threshold; fire `feed_recovered`
        once when feeds come back. Re-arms automatically. Failure to emit a webhook never
        affects the feed loop."""
        threshold_min = settings_store.get("watchdog_outage_minutes") or 0
        try:
            threshold_sec = max(0, int(threshold_min)) * 60
        except (TypeError, ValueError):
            threshold_sec = 0
        if threshold_sec <= 0:
            return  # watchdog disabled
        any_ok = any(f.get("ok") for f in self.feed_status.values())
        if any_ok:
            self._last_any_feed_ok_at = now_ts
            if self._outage_notified:
                # Recovery — fire once, then re-arm for the next outage.
                self._outage_notified = False
                self._watchdog_armed = True
                webhooks_service.fan_out("feed_recovered", {
                    "message": f"PiScope Radar feeds recovered after outage.",
                    "now": now_ts,
                })
            return
        outage_sec = now_ts - self._last_any_feed_ok_at
        if outage_sec >= threshold_sec and self._watchdog_armed:
            # First time crossing the threshold this outage — alert once, then disarm so
            # we don't spam every poll for the whole duration.
            self._watchdog_armed = False
            self._outage_notified = True
            minutes = int(outage_sec // 60)
            webhooks_service.fan_out("feed_down", {
                "message": f"PiScope Radar feeds down for ~{minutes} min. "
                           f"Last successful poll: {int(outage_sec)}s ago.",
                "outage_sec": int(outage_sec),
                "last_error": self.last_error,
                "now": now_ts,
            })

    # --- helpers used by REST endpoints --------------------------------------

    async def test_connection(self, base_url: str) -> dict[str, Any]:
        # resolve=True: user-supplied URL hit on demand from the settings UI.
        base = validate_external_url(base_url, resolve=True).rstrip("/")
        url = f"{base}/data/aircraft.json"
        async with httpx.AsyncClient(timeout=6.0) as client:
            r = await client.get(url)
            r.raise_for_status()
            data = r.json()
            count = len(data.get("aircraft") or [])
            return {"ok": True, "count": count, "messages": data.get("messages")}


feed_service = FeedService()
