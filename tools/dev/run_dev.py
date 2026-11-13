#!/usr/bin/env python3
"""Run PiScope locally against the synthetic feed — for UI work and E2E runs off the Pi.

    python tools/dev/mock_tar1090.py &          # synthetic traffic on :8771
    python tools/dev/run_dev.py                 # PiScope on http://127.0.0.1:8765/piscope
    PISCOPE_URL=http://127.0.0.1:8765/piscope node tests/e2e/run.js

What it does differently from a real install:

- uses a throwaway SQLite database (``PISCOPE_DEV_DB``, default: a file in the system
  temp dir) — never the repo's or the Pi's ``piscope.db``;
- points the feed at the mock tar1090 (``PISCOPE_DEV_FEED``, default
  ``http://127.0.0.1:8771``) with the receiver at its placeholder London position;
- binds 127.0.0.1 only.

The backend's enrichment lookups (hexdb / adsbdb / planespotters) still go out to the
internet when an aircraft is selected. To keep a run fully offline, start this with the
proxy variables pointed at a dead port — the lookups then fail fast and the UI shows its
fallbacks:

    HTTP_PROXY=http://127.0.0.1:9 HTTPS_PROXY=http://127.0.0.1:9 NO_PROXY=127.0.0.1,localhost \\
        python tools/dev/run_dev.py

http://localhost is a secure context, so (unlike the plain-HTTP Pi) the service worker is
active here — which makes this the place to test PWA / cache behaviour.
"""
from __future__ import annotations

import os
import pathlib
import sys
import tempfile

REPO = pathlib.Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO))

from app.services import settings as settings_store  # noqa: E402

settings_store.DB_PATH = pathlib.Path(
    os.environ.get("PISCOPE_DEV_DB") or pathlib.Path(tempfile.gettempdir()) / "piscope-dev.db")
settings_store.init_db()
settings_store.set_many({
    "feed_mode": "local",
    "tar1090_base_url": os.environ.get("PISCOPE_DEV_FEED", "http://127.0.0.1:8771"),
    "receiver_lat": 51.50,
    "receiver_lon": -0.10,
    "global_center_lat": 51.50,
    "global_center_lon": -0.10,
    "contact_url": "https://example.invalid/dev",
    "watchlist": "BAW117",
    "host_guard_enabled": False,
})

import uvicorn  # noqa: E402

from app.main import app  # noqa: E402

if __name__ == "__main__":
    port = int(sys.argv[1]) if len(sys.argv) > 1 else 8765
    print(f"PiScope dev: http://127.0.0.1:{port}/piscope  (db: {settings_store.DB_PATH})", flush=True)
    uvicorn.run(app, host="127.0.0.1", port=port, log_level="warning")
