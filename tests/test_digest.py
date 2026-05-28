"""Daily-digest aggregation. Focus: correctly decoding the compressed snapshot ring
buffer (the previous bug returned 0 peak-concurrent for all modern rows)."""
from __future__ import annotations

import sys
import pathlib

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))


def test_build_digest_reads_peak_from_compressed_snapshots(temp_db):
    # Regression for B5: events.record_snapshot stores snapshots as `Z1` + zlib bytes.
    # The digest must decode them to compute peak_concurrent_aircraft. Previously digest
    # had its own str-only decoder that returned None for the bytes payload, so the peak
    # was always 0. Write via the real compressed writer and assert the count survives.
    import time
    from app.services import events as events_store
    from app.services import digest as digest_svc

    now = time.time()
    events_store.record_snapshot(now - 10, {
        "type": "aircraft_update",
        "aircraft": [{"hex": f"a{i}"} for i in range(5)],
        "trails": {"a0": [[1, 2, 3]]},   # dropped by record_snapshot; must not affect count
    })
    d = digest_svc.build_digest(now_ts=now)
    assert d["totals"]["peak_concurrent_aircraft"] == 5


def test_build_digest_empty_db_is_safe(temp_db):
    # No snapshots / events / stats → zeros, never a crash.
    from app.services import digest as digest_svc
    d = digest_svc.build_digest()
    assert d["totals"]["peak_concurrent_aircraft"] == 0
    assert d["totals"]["events"] == 0


def test_build_digest_uses_utc_date_not_local(temp_db, monkeypatch):
    # Regression for B6: build_digest must key the daily_stats lookup by UTC date — the
    # same key the feed loop / events.update_daily_stats write with. Simulate a host whose
    # local date is a full day behind UTC; the digest must still find the UTC-keyed row.
    import datetime as real_dt
    from app.services import events as events_store
    from app.services import digest as digest_svc

    # Written under the REAL UTC date (events isn't patched).
    events_store.update_daily_stats(unique_hexes_today=42, max_range_nm_today=99.0,
                                     emergencies_today=0, military_today=0)

    class _FakeDateTime:
        @staticmethod
        def now(tz=None):
            base = real_dt.datetime.now(real_dt.timezone.utc)
            # Local (tz=None) pretends to be a day behind — the bug's failure mode.
            return (base - real_dt.timedelta(days=1)) if tz is None else base.astimezone(tz)

    monkeypatch.setattr(digest_svc, "datetime", _FakeDateTime)
    d = digest_svc.build_digest()
    # Pre-fix used local date → looked up the wrong (empty) day → 0. With the UTC fix → 42.
    assert d["totals"]["unique_aircraft_today"] == 42
    assert d["totals"]["max_range_nm_today"] == 99.0
