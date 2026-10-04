"""Track H: POST /chaos/{fault} sets the switches workers read, and records fault.injected."""

from __future__ import annotations

import pytest

from api_helpers import api_client
from ledger_core import agents
from ledger_core.events import read_events
from ledger_core.protocol import AgentCard, EventType, FaultName
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
