"""e2e: determinism and replay (plans/02 F6, F9; plans/05 pre-submission checklist).

- At determinism 1.0, two runs from clean produce identical plans: the
  plan.created payloads (ids stripped), the understanding, and the whole step
  graph the orchestrator built (kind, lane, skill, title, dependencies, inputs,
  check).
- Replay (`make replay RUN=` = POST /runs/{id}/replay) of a finished run makes
  zero live LLM calls: every llm.call of the replay is answered offline
  (scripted fixture or the response cache), the daily OpenRouter budget counter
  does not move, and the replay reproduces the source plan.
"""

from __future__ import annotations

import datetime as dt

import pytest
from e2e_helpers import EXPECTED, Api, Crm, Mailpit, log, normalized_plan, outcomes, reset_world

from ledger_core.keys import Keys
from ledger_core.redis_conn import connect
from ledger_core.settings import get_settings

pytestmark = pytest.mark.e2e


def _run_from_clean(api: Api, crm: Crm, mail: Mailpit, config: dict) -> str:
    reset_world(api, crm, mail)
    run_id = api.submit(config)
    run = api.wait_settled(run_id)
    assert run["status"] == "completed_pending_input", run
    return run_id


def test_determinism_two_runs_from_clean_identical_plans(api: Api, crm: Crm, mail: Mailpit):
    a = _run_from_clean(api, crm, mail, {"determinism": 1.0})
    b = _run_from_clean(api, crm, mail, {"determinism": 1.0})
    for run_id in (a, b):
        cfg = api.get(f"/runs/{run_id}/config")
        assert cfg["config"]["determinism"] == 1.0 and cfg["config"]["seed_pinned"]
    pa, pb = normalized_plan(api, a), normalized_plan(api, b)
    assert pa["plan_created"] == pb["plan_created"]
    assert pa["understood"] == pb["understood"]
    assert pa["graph"] == pb["graph"]
    assert len(pa["graph"]) > 30
    assert outcomes(api.report(a)) == outcomes(api.report(b)) == {n: v[0] for n, v in EXPECTED.items()}
    log(f"identical plans: {a} == {b} ({len(pa['graph'])} steps)")


async def _budget_used() -> int:
    r = connect()
    try:
        day = dt.datetime.now(dt.UTC).strftime("%Y-%m-%d")
        return int(await r.get(Keys(get_settings().ledger_ns).llm_budget(day)) or 0)
    finally:
        await r.aclose()


async def test_replay_makes_zero_live_llm_calls(api: Api, crm: Crm, mail: Mailpit, shared):
    src = shared.get("finished_run")
    if src is None:  # run alone: make a source run first
        src = _run_from_clean(api, crm, mail, {})
    budget_before = await _budget_used()
    reset_world(api, crm, mail)
    replay = api.post(f"/runs/{src}/replay")
    assert replay["replay_of"] == src and not replay.get("warning"), replay
    run = api.wait_settled(replay["id"])
    assert run["status"] == "completed_pending_input", run

    calls = api.events(replay["id"], "llm.call", "llm.cache_hit")
    assert calls, "the replay made no LLM requests at all"
    live = [e for e in calls if e["type"] == "llm.call" and not e["payload"].get("scripted")
            and not e["payload"].get("cached")]
    assert live == [], [(e["actor"], e["payload"].get("model")) for e in live]
    assert await _budget_used() == budget_before
    assert api.get("/llm/budget", run_id=replay["id"], live=False) is not None
    log(f"replay {replay['id']} of {src}: {len(calls)} LLM calls, 0 live")

    ps, pr = normalized_plan(api, src), normalized_plan(api, replay["id"])
    assert pr["plan_created"] == ps["plan_created"] and pr["understood"] == ps["understood"]
    assert outcomes(api.report(replay["id"])) == {n: v[0] for n, v in EXPECTED.items()}
