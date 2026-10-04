"""Track H: /runs endpoints against a real Redis (TestClient, unique namespace)."""

from __future__ import annotations

import json

import pytest

from ledger_core import ledger, run_config
from ledger_core.config import RunConfig
from ledger_core.events import read_events
from ledger_core.protocol import EventType, StepStatus

from api_helpers import api_client

PARSE_STEPS = [{
    "ref": "parse", "kind": "file.parse", "title": "Parse event_attendees.csv",
    "inputs": {"file": "event_attendees.csv"},
    "postcondition": {"check": "file.parsed_rows", "args": {"file": "event_attendees.csv"}},
}]


@pytest.fixture
def client(ns, r):  # r cleans the namespace afterwards
    with api_client(ns) as c:
        yield c


async def test_create_run_for_orchestrator(client, r, keys):
    res = client.post("/runs", json={"goal": "Process yesterday's leads", "input_file": "event_attendees.csv"})
    assert res.status_code == 201, res.text
    run = res.json()
    assert run["goal"] == "Process yesterday's leads"
    assert run["status"] == "created" and run["steps_total"] == 0
    assert len(run["input_sha256"]) == 64
    assert run["config_hash"] == RunConfig.model_validate(
        (await run_config.get_record(r, keys, run["id"]))["config"]).config_hash()
    types = [e.type for e in await read_events(r, keys, run_id=run["id"])]
    # config is stored before run.created (Track M): an orchestrator that picks the run
    # up at run.created must find the submitted config, not store playbook defaults
    assert types[:2] == [EventType.RUN_CONFIG_UPDATED, EventType.RUN_CREATED]

    listed = client.get("/runs").json()
    assert [x["id"] for x in listed] == [run["id"]]
    got = client.get(f"/runs/{run['id']}").json()
    assert got["id"] == run["id"] and got["current_config_hash"] == run["config_hash"]
    assert client.get("/runs/run_missing").status_code == 404


def test_create_run_validates_input(client):
    assert client.post("/runs", json={"goal": "x", "input_file": "nope.csv"}).status_code == 422
    assert client.post("/runs", json={"goal": "x", "input_file": "../../etc/passwd"}).status_code == 422
    assert client.post("/runs", json={"goal": ""}).status_code == 422
    assert client.post("/runs", json={"goal": "x", "config": {"bogus": 1}}).status_code == 422
    assert client.post("/runs", json={"goal": "x", "config": {"determinism": 7}}).status_code == 422


async def test_hand_written_plan_steps_and_step_detail(client, r, keys):
    run = client.post("/runs", json={"goal": "parse", "input_file": "event_attendees.csv",
                                     "config": {"max_attempts": 2}, "steps": PARSE_STEPS}).json()
    assert run["status"] == "running" and run["steps_total"] == 1
    steps = client.get(f"/runs/{run['id']}/steps").json()
    assert len(steps) == 1 and steps[0]["status"] == "ready" and steps[0]["max_attempts"] == 2
    detail = client.get(f"/runs/{run['id']}/steps/{steps[0]['id']}").json()
    assert [e["type"] for e in detail["events"]] == ["step.ready"]
    assert detail["lease_ttl_ms"] is None and detail["history"] == []
    assert client.get(f"/runs/{run['id']}/steps/stp_nope").status_code == 404
    # the step was enqueued for the parser skill
    assert await r.xlen(keys.queue("file.parse")) == 1


async def test_events_pagination_and_type_filter(client, r, keys):
    run = client.post("/runs", json={"goal": "p", "steps": PARSE_STEPS}).json()
    rid = run["id"]
    all_events = client.get(f"/runs/{rid}/events").json()
    types = [e["type"] for e in all_events["events"]]
    assert types[:2] == ["run.config_updated", "run.created"] and "plan.created" in types and "step.ready" in types
    assert all_events["next"] is None

    page1 = client.get(f"/runs/{rid}/events", params={"limit": 2}).json()
    assert len(page1["events"]) == 2 and page1["next"] == page1["events"][-1]["id"]
    page2 = client.get(f"/runs/{rid}/events", params={"limit": 100, "after": page1["next"]}).json()
    assert [e["id"] for e in page1["events"] + page2["events"]] == [e["id"] for e in all_events["events"]]

    only = client.get(f"/runs/{rid}/events", params={"type": "step.ready"}).json()["events"]
    assert {e["type"] for e in only} == {"step.ready"}
    # another run's events never leak in
    other = client.post("/runs", json={"goal": "other"}).json()
    assert all(e["run_id"] == rid for e in client.get(f"/runs/{rid}/events").json()["events"])
    assert other["id"] != rid


async def test_facts(client, r, keys):
    run = client.post("/runs", json={"goal": "f"}).json()
    await ledger.commit_fact(r, keys, run["id"], "lead:1", {"name": "Priya Raman"}, source_step="stp_x",
                             actor="verifier")
    facts = client.get(f"/runs/{run['id']}/facts").json()
    assert facts["lead:1"]["value"] == {"name": "Priya Raman"} and facts["lead:1"]["source_step"] == "stp_x"


async def test_run_config_get_put_reset(client, r, keys):
    rid = client.post("/runs", json={"goal": "c"}).json()["id"]
    got = client.get(f"/runs/{rid}/config").json()
    assert got["config"]["review_auto_threshold"] == 0.8 and got["version"] == 1
    res = client.put(f"/runs/{rid}/config", json={"review_auto_threshold": 0.7, "dry_run": True})
    assert res.status_code == 200, res.text
    body = res.json()
    assert body["config"]["review_auto_threshold"] == 0.7 and body["config"]["dry_run"] is True
    assert body["config_hash"] != got["config_hash"] and body["version"] == 2
    # GUI shape {config: {...}} is accepted too
    assert client.put(f"/runs/{rid}/config", json={"config": {"max_attempts": 4}}).json()["config"]["max_attempts"] == 4
    assert client.put(f"/runs/{rid}/config", json={"nope": 1}).status_code == 422
    assert client.put(f"/runs/{rid}/config", json={"lease_ttl_s": 1}).status_code == 422
    evs = [e for e in await read_events(r, keys, run_id=rid) if e.type == EventType.RUN_CONFIG_UPDATED]
    assert evs[-1].payload["diff"] == {"max_attempts": [3, 4]}
    # workers/verifier read the same config through ledger.get_run_config
    assert (await ledger.get_run_config(r, keys, rid)).max_attempts == 4
    reset = client.post(f"/runs/{rid}/config/reset").json()
    assert reset["config"]["max_attempts"] == 3 and reset["config"]["dry_run"] is False


async def test_determinism(client, r, keys):
    rid = client.post("/runs", json={"goal": "d"}).json()["id"]
    res = client.post(f"/runs/{rid}/determinism", json={"level": 1.0, "seed": 7})
    assert res.status_code == 200, res.text
    body = res.json()
    assert body["config"]["determinism"] == 1.0 and body["config"]["seed"] == 7
    assert body["temperatures"] == {"orchestrator": 0.0, "worker": 0.0, "meta_reviewer": 0.0, "verifier": 0.0}
    assert await r.get(keys.run_determinism(rid)) == "1.0"
    half = client.post(f"/runs/{rid}/determinism", json={"level": 0.5}).json()
    assert half["temperatures"]["worker"] == 0.5 and half["config"]["seed"] == 7
    assert client.post(f"/runs/{rid}/determinism", json={"level": 2}).status_code == 422


async def test_config_written_by_cli_hash_store(client, r, keys):
    """ledger.set_run_config (CLI) stores a Hash; the API reads it and migrates on write."""
    run = await ledger.create_run(r, keys, goal="cli run", actor="cli")
    await ledger.set_run_config(r, keys, run.id, RunConfig(max_attempts=5), actor="cli")
    assert client.get(f"/runs/{run.id}/config").json()["config"]["max_attempts"] == 5
    body = client.put(f"/runs/{run.id}/config", json={"dry_run": True}).json()
    assert body["config"]["max_attempts"] == 5 and body["config"]["dry_run"] is True
    assert await r.type(keys.run_config(run.id)) == "string"
    assert (await ledger.get_run_config(r, keys, run.id)).dry_run is True


async def test_replay(client, r, keys):
    src = client.post("/runs", json={"goal": "replay me", "input_file": "event_attendees.csv",
                                     "config": {"determinism": 1.0}}).json()
    res = client.post(f"/runs/{src['id']}/replay")
    assert res.status_code == 201, res.text
    rep = res.json()
    assert rep["replay_of"] == src["id"] and rep["id"] != src["id"]
    assert rep["goal"] == src["goal"] and rep["input_sha256"] == src["input_sha256"]
    assert rep["config_hash"] == src["config_hash"]
    created = [e for e in await read_events(r, keys, run_id=rep["id"]) if e.type == EventType.RUN_CREATED]
    assert created[0].payload["replay_of"] == src["id"]
    seeded = client.post(f"/runs/{src['id']}/replay", json={"seed": 99}).json()
    assert client.get(f"/runs/{seeded['id']}/config").json()["config"]["seed"] == 99
    assert client.post("/runs/run_nope/replay").status_code == 404


async def test_run_summary_counts(client, r, keys):
    run = client.post("/runs", json={"goal": "p", "steps": PARSE_STEPS}).json()
    step = client.get(f"/runs/{run['id']}/steps").json()[0]
    await ledger.transition(r, keys, step["id"], StepStatus.DEAD, actor="t", actor_role="orchestrator")
    got = client.get(f"/runs/{run['id']}").json()
    assert got["step_counts"] == {"dead": 1} and got["steps_committed"] == 0
    assert json.loads(json.dumps(got))  # JSON-serialisable
