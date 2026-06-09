"""FlightAware budget accounting (iteration 13): the reserve-then-refund gate
must charge for billed calls (incl. 404), refund auth/network failures, and
never spend once the monthly cap is reached unless explicitly overridden."""
from __future__ import annotations

import asyncio
import sys
import pathlib

import httpx
import pytest

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))


class _Resp:
    def __init__(self, status_code, payload=None):
        self.status_code = status_code
        self._payload = payload or {}
    def json(self):
        return self._payload
    def raise_for_status(self):
        if self.status_code >= 400:
            raise httpx.HTTPStatusError("err", request=None, response=None)


class _Client:
    def __init__(self, resp=None, exc=None):
        self._resp, self._exc = resp, exc
    async def get(self, *a, **k):
        if self._exc:
            raise self._exc
        return self._resp


def _setup(monkeypatch, temp_db, client_obj):
    from app.services import flightaware, settings as s
    s.set_many({"fa_api_key": "KEY", "fa_monthly_limit_cents": 20})
    flightaware._CACHE.clear()
    async def _gc():
        return client_obj
    monkeypatch.setattr(flightaware, "get_client", _gc)
    return flightaware, s


def test_successful_lookup_bills_once(temp_db, monkeypatch):
    fa, s = _setup(monkeypatch, temp_db,
                   _Client(_Resp(200, {"flights": [{"ident": "BAW1", "operator": "BAW"}]})))
    res = asyncio.run(fa.lookup("BAW1"))
    assert res["flight"]["operator"] == "BAW"
    assert s.fa_budget_status()["spent_cents"] == 5


def test_404_is_billed(temp_db, monkeypatch):
    fa, s = _setup(monkeypatch, temp_db, _Client(_Resp(404)))
    res = asyncio.run(fa.lookup("BAW2"))
    assert res["flight"] is None
    assert s.fa_budget_status()["spent_cents"] == 5   # AeroAPI charges for 404s


def test_network_error_refunded(temp_db, monkeypatch):
    fa, s = _setup(monkeypatch, temp_db, _Client(exc=httpx.ConnectError("boom")))
    res = asyncio.run(fa.lookup("BAW3"))
    assert "error" in res
    assert s.fa_budget_status()["spent_cents"] == 0   # reservation refunded


def test_auth_failure_refunded(temp_db, monkeypatch):
    fa, s = _setup(monkeypatch, temp_db, _Client(_Resp(401)))
    asyncio.run(fa.lookup("BAW4"))
    assert s.fa_budget_status()["spent_cents"] == 0


def test_over_budget_blocks_without_override(temp_db, monkeypatch):
    fa, s = _setup(monkeypatch, temp_db, _Client(_Resp(200, {"flights": []})))
    s.fa_record_call(20)   # at the 20¢ cap
    res = asyncio.run(fa.lookup("BAW5"))
    assert res.get("blocked") == "over_budget"
    assert s.fa_budget_status()["spent_cents"] == 20   # no further spend
    # Override spends.
    res = asyncio.run(fa.lookup("BAW6", allow_over_budget=True))
    assert s.fa_budget_status()["spent_cents"] == 25
