"""Shared pytest fixtures.

Runs against a throwaway temp SQLite DB so tests never touch the live
/opt/piscope/piscope.db. The TestClient is created WITHOUT the context-manager
form on purpose — that skips the app lifespan, so the feed poll loop and digest
scheduler don't start (no network calls to adsb.lol / hexdb during tests).
"""
from __future__ import annotations

import os
import pathlib
import sys
import tempfile

import pytest

# Make the `app` package importable regardless of pytest's path mode.
sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))


@pytest.fixture()
def temp_db(monkeypatch):
    """Point the settings store at a fresh temp DB, initialise the schema, and
    reset the in-memory caches for isolation."""
    fd, path = tempfile.mkstemp(suffix=".db")
    os.close(fd)
    from app.services import settings as settings_store

    monkeypatch.setattr(settings_store, "DB_PATH", pathlib.Path(path))
    settings_store._CACHE = None
    settings_store._CACHE_VERSION = 0
    settings_store.init_db()
    # Reset insights' in-process known-type ledger so rare-type state doesn't leak between tests.
    from app.services import insights as insights_store
    insights_store._KNOWN_TYPES = None
    yield path
    try:
        os.unlink(path)
    except OSError:
        pass


@pytest.fixture()
def client(temp_db):
    from fastapi.testclient import TestClient
    from app.main import app
    # No `with` → lifespan (feed poll loop + digest scheduler) does not start.
    return TestClient(app)
