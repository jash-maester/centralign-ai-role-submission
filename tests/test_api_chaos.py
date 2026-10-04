"""Track H: POST /chaos/{fault} sets the switches workers read, and records fault.injected."""

from __future__ import annotations

import pytest

from api_helpers import api_client
from ledger_core import agents, faults, ledger, report
from ledger_core.events import read_events
from ledger_core.protocol import AgentCard, EventType, FaultName, Run, RunStatus
from ledger_core.worker_base import take_fault_shot


@pytest.fixture
def client(ns, r):
    with api_client(ns) as c:
        yield c


async def test_every_switch_fault(client, r, keys):
    rid = client.post("/runs", json={"goal": "chaos"}).json()["id"]
    expected = {
        "false_claim": "false_claim:browser.espocrm",
        "expire_session": "expire_session",
        "model_outage": "model_outage",
        "ui_changed": "ui_changed",
    }
    for fault, field in expected.items():
        res = client.post(f"/chaos/{fault}")
        assert res.status_code == 200, res.text
        assert res.json()["switch"] == field and res.json()["run_id"] == rid
    armed = await r.hgetall(keys.faults)
    assert armed == {f: "1" for f in expected.values()}
    evs = [e for e in await read_events(r, keys) if e.type == EventType.FAULT_INJECTED]
    assert [e.payload["fault"] for e in evs] == list(expected)
    assert all(e.payload["phase"] == "injected" and e.run_id == rid for e in evs)
    # the worker side consumes the scoped shot exactly once
    assert await take_fault_shot(r, keys, FaultName.FALSE_CLAIM, skill="browser.espocrm") == "false_claim:browser.espocrm"
    assert await take_fault_shot(r, keys, FaultName.FALSE_CLAIM, skill="browser.espocrm") is None
    assert client.get("/chaos").json()["armed"] == {f: "1" for f in list(expected.values())[1:]}


async def test_shots_skill_and_clear(client, r, keys):
    assert client.post("/chaos/model_outage", json={"shots": "on"}).json()["shots"] == "on"
    assert client.post("/chaos/false_claim", json={"skill": "", "shots": 3}).json()["switch"] == "false_claim"
    assert await r.hget(keys.faults, "false_claim") == "3"
    assert client.post("/chaos/false_claim", json={"shots": 0}).status_code == 422
    assert client.post("/chaos/false_claim", json={"shots": "lots"}).status_code == 422
    assert client.post("/chaos/not_a_fault").status_code == 422
    assert client.delete("/chaos/false_claim").json()["cleared"] == ["false_claim"]
    assert await r.hgetall(keys.faults) == {"model_outage": "on"}


async def test_kill_worker_records_event(client, r, keys):
    assert client.post("/chaos/kill_worker").status_code == 422
    await agents.register_agent(r, keys, AgentCard(id="worker-browser-2", name="b2", role="worker",
                                                   container="worker-browser-2"))
    await agents.set_alive(r, keys, "worker-browser-2", current_step="stp_1", run_id="run_1", fence=3)
    res = client.post("/chaos/kill_worker", json={"agent_id": "worker-browser-2", "kill": False}).json()
    assert "kill" not in res
    ev = [e for e in await read_events(r, keys) if e.type == EventType.FAULT_INJECTED][-1]
    assert ev.payload["agent_id"] == "worker-browser-2" and ev.payload["container"] == "worker-browser-2"
    assert '"current_step": "stp_1"' in ev.payload["held"] and ev.payload["kill"] == "recorded only"
    # no docker socket in the test container: an honest error, still recorded
    res = client.post("/chaos/kill_worker", json={"agent_id": "worker-browser-2"}).json()
    assert "error" in res["kill"]


# ---- run attribution (Track N) ------------------------------------------------------------


async def _run(r, keys, status: RunStatus, created_at: int):
    run = await ledger.create_run(r, keys, Run(goal=f"chaos {status.value}", created_at=created_at))
    if status != RunStatus.CREATED:
        await ledger.set_run_status(r, keys, run.id, status, actor="test")
    return run


async def test_chaos_targets_the_running_run_not_one_waiting_on_a_human(client, r, keys):
    running = await _run(r, keys, RunStatus.RUNNING, 1_000)
    waiting = await _run(r, keys, RunStatus.COMPLETED_PENDING_INPUT, 2_000)  # newer, but only waits on a human
    res = client.post("/chaos/false_claim").json()
    assert res["run_id"] == running.id and res["pending"] is False
    assert client.post("/chaos/model_outage", params={"run_id": waiting.id}).json()["run_id"] == waiting.id
    assert client.post("/chaos/ui_changed", json={"run_id": waiting.id}).json()["run_id"] == waiting.id
    assert client.post("/chaos/ui_changed", params={"run_id": "run_nope"}).status_code == 404
    assert await r.hgetall(keys.faults_pending) == {}


async def test_chaos_without_a_running_run_is_pending_until_a_run_consumes_it(client, r, keys):
    old = await _run(r, keys, RunStatus.COMPLETED_PENDING_INPUT, 1_000)
    res = client.post("/chaos/false_claim").json()
    assert res["run_id"] is None and res["pending"] is True
    first = [e for e in await read_events(r, keys) if e.type == EventType.FAULT_INJECTED]
    assert len(first) == 1 and first[0].run_id is None and first[0].payload["pending"] is True
    assert "false_claim" in await r.hgetall(keys.faults_pending)

    nxt = await _run(r, keys, RunStatus.RUNNING, 3_000)
    shot = await faults.consume_and_record(r, keys, FaultName.FALSE_CLAIM, scope="browser.espocrm",
                                           actor="worker-browser-1", run_id=nxt.id, step_id="stp_x")
    assert shot == "false_claim:browser.espocrm"
    mine = [e for e in await ledger.run_events(r, keys, nxt.id) if e.type == EventType.FAULT_INJECTED]
    assert [e.payload["phase"] for e in mine] == ["injected", "consumed"]
    assert mine[0].payload["attached"] is True and mine[0].payload["pending_event"] == res["event_id"]
    assert not [e for e in await ledger.run_events(r, keys, old.id) if e.type == EventType.FAULT_INJECTED]
    assert await r.hgetall(keys.faults_pending) == {}
    rep = await report.build_report(r, keys, nxt.id)
    assert [f["fault"] for f in rep["faults"]] == ["false_claim"]
    # attached once only: a re-armed shot does not attach the old injection again
    await faults.set_fault(r, keys, FaultName.FALSE_CLAIM, scope="browser.espocrm", run_id=nxt.id)
    await faults.consume_and_record(r, keys, FaultName.FALSE_CLAIM, scope="browser.espocrm", actor="w",
                                    run_id=nxt.id, step_id="stp_y")
    phases = [e.payload["phase"] for e in await ledger.run_events(r, keys, nxt.id)
              if e.type == EventType.FAULT_INJECTED]
    assert phases == ["injected", "consumed", "set", "consumed"]


async def test_clearing_a_fault_drops_its_pending_injection(client, r, keys):
    assert client.post("/chaos/model_outage").json()["pending"] is True
    assert client.delete("/chaos/model_outage").json()["cleared"] == ["model_outage"]
    assert await r.hgetall(keys.faults_pending) == {}
