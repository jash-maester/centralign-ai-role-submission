"""llm_budget.Budget: OpenRouter's GET /key first, a persistent per-day file
counter otherwise (respx-mocked; never the real API)."""

from __future__ import annotations

import json

import httpx
import pytest

from ledger_core.llm_budget import Budget, BudgetRefused, LocalCounter, utc_day
from ledger_core.settings import Settings

respx = pytest.importorskip("respx")

BASE = "https://openrouter.test/api/v1"
KEY = "sk-test-not-real"


def info(remaining: int, credit: float | None = 5.0) -> dict:
    return {"data": {"label": "sk-or-v1-abc...xyz", "limit_remaining": credit, "limit": 5,
                     "free_model_daily_requests": {"used": 50 - remaining, "limit": 50, "remaining": remaining}}}


def budget(tmp_path, *, key: str = KEY, **kw) -> Budget:
    s = Settings(openrouter_api_key=key, openrouter_base_url=BASE, llm_daily_request_budget=4, llm_budget_reserve=2)
    return Budget(s, cache_dir=str(tmp_path), **kw)


def test_local_counter_is_a_file_per_utc_day(tmp_path):
    c = LocalCounter(tmp_path)
    assert c.read("2026-10-03") == 0
    assert c.incr("2026-10-03") == 1 and c.incr("2026-10-03", 2) == 3
    assert c.read("2026-10-04") == 0  # a new day starts at 0
    assert json.loads((tmp_path / "budget" / "2026-10-03.json").read_text())["used"] == 3
    assert LocalCounter(tmp_path).read("2026-10-03") == 3  # another process sees it


async def test_free_model_reserve_and_paid_credit(tmp_path):
    with respx.mock(base_url=BASE) as mock:
        route = mock.get("/key").respond(json=info(3, credit=0.0))
        b = budget(tmp_path)
        snap = await b.check("vendor/model:free")  # 3 left > reserve 2
        assert snap["source"] == "openrouter" and snap["usable"] == 1
        assert route.calls[0].request.headers["authorization"] == f"Bearer {KEY}"
        b.record()
        with pytest.raises(BudgetRefused) as exc:
            await b.check("vendor/model:free")  # 3 - 1 made since the fetch = 2 = the reserve
        assert exc.value.snapshot["remaining"] == 2
        with pytest.raises(BudgetRefused, match="credit"):
            await b.check("vendor/paid-model")  # paid models spend credit: limit_remaining 0
        assert route.call_count == 1
        assert KEY not in json.dumps(await b.snapshot()) and "sk-or-v1" not in json.dumps(await b.snapshot())


async def test_refetch_after_ttl(tmp_path):
    now = [0.0]
    with respx.mock(base_url=BASE) as mock:
        route = mock.get("/key").mock(side_effect=[httpx.Response(200, json=info(30)),
                                                   httpx.Response(200, json=info(20))])
        b = budget(tmp_path, ttl_s=60, clock=lambda: now[0])
        assert (await b.snapshot())["remaining"] == 30
        now[0] = 59
        assert (await b.snapshot())["remaining"] == 30 and route.call_count == 1
        now[0] = 61
        assert (await b.snapshot())["remaining"] == 20 and route.call_count == 2


async def test_no_key_or_failed_check_uses_the_local_counter(tmp_path):
    b = budget(tmp_path, key="")
    snap = await b.snapshot()
    assert snap["source"] == "local" and snap["openrouter"] is None and snap["limit"] == 4
    for _ in range(4):
        b.record()
    with pytest.raises(BudgetRefused, match="local counter"):
        await b.check("vendor/model:free")
    assert b.counter.read(utc_day()) == 4
    with respx.mock(base_url=BASE) as mock:
        mock.get("/key").respond(503)
        failed = budget(tmp_path)
        snap = await failed.snapshot()
    assert snap["source"] == "local" and snap["used"] == 4 and snap["openrouter"] == {"ok": False, "status": 503}
