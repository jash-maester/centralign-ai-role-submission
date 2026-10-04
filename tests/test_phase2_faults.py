"""Track F: ledger_core.faults is the single reader/writer of Keys.faults (F2-F5)."""

from __future__ import annotations

import sys
import types

import pytest

from ledger_core import faults, ledger
from ledger_core.events import read_events
from ledger_core.protocol import EventType, FaultName
from ledger_core.verifier import Verifier
from ledger_core.worker_base import Worker, WorkResult, take_fault_shot, worker_card

from ledger_helpers import S, new_step


async def _fault_events(r, keys):
    return [e for e in await read_events(r, keys) if e.type == EventType.FAULT_INJECTED]


async def test_set_emits_who_and_what(r, keys):
    f = await faults.set_fault(r, keys, FaultName.FALSE_CLAIM, shots=2, scope="api.espocrm", actor="api",
                               run_id="run_x")
    assert f == "false_claim:api.espocrm"
    assert await r.hget(keys.faults, f) == "2"
    [ev] = await _fault_events(r, keys)
    assert ev.payload["phase"] == "set" and ev.payload["by"] == "api" and ev.run_id == "run_x"
    assert ev.payload["value"] == "2" and "verifier must reject" in ev.payload["what"]
    await faults.set_fault(r, keys, FaultName.MODEL_OUTAGE, shots=None)
    assert await r.hget(keys.faults, "model_outage") == "on"
    with pytest.raises(ValueError):
        await faults.set_fault(r, keys, FaultName.UI_CHANGED, shots=0)


@pytest.mark.parametrize("fault", list(FaultName))
async def test_every_fault_can_be_set_consumed_cleared(r, keys, fault):
    await faults.set_fault(r, keys, fault, shots=1)
    assert await faults.is_armed(r, keys, fault)
    assert await faults.consume(r, keys, fault) == fault.value
    assert not await faults.is_armed(r, keys, fault)
    assert await faults.consume(r, keys, fault) is None
    await faults.set_fault(r, keys, fault, shots=None, scope="x")
    assert await faults.clear(r, keys, fault) == [f"{fault.value}:x"]
    phases = [e.payload["phase"] for e in await _fault_events(r, keys)]
    assert phases == ["set", "set", "cleared"]


async def test_scopes_most_specific_first_and_shots_count(r, keys):
    await faults.set_fault(r, keys, "false_claim", shots=1, scope="worker-api")
    await faults.set_fault(r, keys, "false_claim", shots=2, scope="api.espocrm")
    await faults.set_fault(r, keys, "false_claim", shots=1)
    order = [await faults.consume(r, keys, "false_claim", scope=["worker-api", "api.espocrm"]) for _ in range(5)]
    assert order == ["false_claim:worker-api", "false_claim:api.espocrm", "false_claim:api.espocrm",
                     "false_claim", None]
    # unrelated scope only sees the global field
    await faults.set_fault(r, keys, "false_claim", shots=1, scope="api.espocrm")
    assert await faults.consume(r, keys, "false_claim", scope="file.parse") is None
    # legacy helper delegates to the module
    assert await take_fault_shot(r, keys, "false_claim", skill="api.espocrm") == "false_claim:api.espocrm"


async def test_on_is_not_consumed_and_clear_all(r, keys):
    await faults.set_fault(r, keys, "model_outage", shots=None)
    await faults.set_fault(r, keys, "expire_session", shots=3, scope="worker-browser-1")
    for _ in range(3):
        assert await faults.consume(r, keys, "model_outage") == "model_outage"
    assert await faults.active(r, keys) == {"model_outage": "on", "expire_session:worker-browser-1": "3"}
    assert sorted(await faults.clear(r, keys)) == ["expire_session:worker-browser-1", "model_outage"]
    assert await faults.active(r, keys) == {}
    assert await faults.clear(r, keys) == []


async def test_worker_false_claim_goes_through_faults_module(r, keys):
    """F2 via the faults module: agent-scoped switch, consumed event on the step."""
    step = await new_step(r, keys)
    await ledger.transition(r, keys, step.id, S.READY, actor="o", actor_role="orchestrator")
    calls = []

    async def handler(step, ctx):
        calls.append(step.id)
        return WorkResult(summary="parsed", data={"rows": []})

    w = Worker(r, keys, worker_card("parser-x", "p", {"file.parse": ["file.parse"]}), handler, block_ms=150)
    await faults.set_fault(r, keys, "false_claim", shots=1, scope="parser-x", actor="test")
    await w.run_until_idle()
    s = await ledger.get_step(r, keys, step.id)
    assert s.status == S.CLAIMED_DONE and s.claim.acted is False and calls == []
    consumed = [e for e in await _fault_events(r, keys) if e.payload["phase"] == "consumed"]
    assert consumed[0].step_id == step.id and consumed[0].payload["switch"] == "false_claim:parser-x"
    # the verifier catches it (file.parsed_rows against the real CSV)
    await Verifier(r, keys, block_ms=150).run_until_idle()
    assert (await ledger.get_step(r, keys, step.id)).status == S.READY


async def test_model_outage_reads_through_faults(r, keys):
    from ledger_core.llm_openrouter import OpenRouterBackend

    be = OpenRouterBackend(r=r, keys=keys)
    assert await be._consume_outage() is False
    await faults.set_fault(r, keys, "model_outage", shots=1)
    assert await be._consume_outage() is True
    assert await be._consume_outage() is False


@pytest.fixture
def browser_hook(monkeypatch):
    """Import browser_worker.faults_hook without Playwright (not in the core image)."""
    pw = types.ModuleType("playwright")
    api = types.ModuleType("playwright.async_api")
    api.Locator = api.Page = object
    monkeypatch.setitem(sys.modules, "playwright", pw)
    monkeypatch.setitem(sys.modules, "playwright.async_api", api)
    monkeypatch.syspath_prepend("/repo/services/browser")
    from browser_worker import faults_hook, selectors

    yield faults_hook, selectors
    selectors.set_ui_changed(False)


async def test_browser_hook_expire_session_and_ui_changed(r, keys, browser_hook):
    faults_hook, selectors = browser_hook
    step = await new_step(r, keys)

    class Op:
        expired = 0

        async def expire_session(self):
            self.expired += 1

    op = Op()
    assert await faults_hook.before_step(op, r, keys, step, agent_id="worker-browser-1") == []
    await faults.set_fault(r, keys, "expire_session", shots=1, scope="worker-browser-1")
    await faults.set_fault(r, keys, "ui_changed", shots=1, scope="browser.espocrm")
    fired = await faults_hook.before_step(op, r, keys, step, agent_id="worker-browser-1")
    assert fired == ["expire_session", "ui_changed"] and op.expired == 1 and selectors.ui_changed()
    faults_hook.after_step(fired)
    assert not selectors.ui_changed()
    assert await faults_hook.before_step(op, r, keys, step, agent_id="worker-browser-2") == []
    consumed = [e for e in await _fault_events(r, keys) if e.payload["phase"] == "consumed"]
    assert {e.payload["fault"] for e in consumed} == {"expire_session", "ui_changed"}
    assert all(e.step_id == step.id for e in consumed)
