"""Track H: /agents, /agents/{id}/config, restart and the exec WebSocket (no docker socket here)."""

from __future__ import annotations

import pytest

from ledger_core import agents, ledger
from ledger_core.events import read_events
from ledger_core.protocol import AgentCard, AgentSkill, EventType, Skill, StepKind, ToolSpec

from api_helpers import api_client
from ledger_helpers import a_claim, a_verdict, lease, new_step

BROWSER = AgentCard(
    id="worker-browser-1", name="EspoCRM browser operator", role="worker", model_role="worker",
    skills=[AgentSkill(id=Skill.BROWSER_ESPOCRM, kinds=[StepKind.CRM_CREATE_CONTACT])], side_effects=True,
    container="worker-browser-1",
    tools=[ToolSpec(id="browser", type="browser", name="Playwright"),
           ToolSpec(id="crm-rest", type="rest", name="EspoCRM REST", detail="espocrm api")],
)
VERIFIER = AgentCard(id="verifier", name="Verifier", role="verifier", model_role="verifier",
                     tools=[ToolSpec(id="crm-rest", type="rest", name="EspoCRM REST")])


@pytest.fixture
def client(ns, r):
    with api_client(ns) as c:
        yield c


async def test_list_agents_liveness_lease_and_stats(client, r, keys):
    await agents.register_agent(r, keys, BROWSER)
    await agents.register_agent(r, keys, VERIFIER)
    step = await new_step(r, keys)
    await ledger.transition(r, keys, step.id, "ready", actor="o", actor_role="orchestrator")
    fence = await lease(r, keys, step.id, "worker-browser-1")
    await agents.set_alive(r, keys, "worker-browser-1", current_step=step.id, run_id=step.run_id, fence=fence)
    await ledger.claim(r, keys, step.id, a_claim("worker-browser-1", fence))
    await ledger.reject(r, keys, step.id, a_verdict(False, "no record"))

    rows = client.get("/agents").json()
    assert isinstance(rows, list) and [a["id"] for a in rows] == ["verifier", "worker-browser-1"]
    br = rows[1]
    assert br["alive"] is True and br["current_step"] == step.id and br["lease_fence"] == fence
    assert br["lease_ttl_ms"] and 0 < br["lease_ttl_ms"] <= 15_000
    assert br["lease_total_ms"] == 15_000
    assert br["heartbeat_age_ms"] is not None and br["heartbeat_age_ms"] >= 0
    assert br["rejections"] == 1 and br["steps_done"] == 0 and br["role"] == "worker"
    assert rows[0]["alive"] is False and rows[0]["current_step"] is None


async def test_agent_config_view_and_updates(client, r, keys):
    await agents.register_agent(r, keys, BROWSER)
    cfg = client.get("/agents/worker-browser-1/config").json()
    assert cfg["prompt_version"] == 0 and cfg["prompt"]
    layers = {x["id"]: x for x in cfg["layers"]}
    assert list(layers) == ["role", "playbook", "step", "history", "schema"]
    assert layers["history"]["locked"] and not layers["playbook"]["locked"]
    assert layers["role"]["tokens"] > 0
    tools = {t["id"]: t for t in cfg["tools"]}
    assert tools["crm-rest"]["enabled"] is False and tools["crm-rest"]["locked_reason"]

    res = client.put("/agents/worker-browser-1/config", json={"prompt": "Be careful.", "layers": {"schema": False}})
    assert res.status_code == 200, res.text
    body = res.json()
    assert body["prompt"] == "Be careful." and body["prompt_version"] == 1
    assert {x["id"]: x["enabled"] for x in body["layers"]}["schema"] is False
    types = {e.type for e in await read_events(r, keys)}
    assert {EventType.PROMPT_UPDATED, EventType.CONFIG_UPDATED} <= types

    assert client.put("/agents/worker-browser-1/config", json={"layers": {"history": False}}).status_code == 409
    # GUI sends the full ToolSpec list; enabling CRM REST on a worker is refused
    crm_on = [{**t, "enabled": True} if t["id"] == "crm-rest" else t for t in body["tools"]]
    assert client.put("/agents/worker-browser-1/config", json={"tools": crm_on}).status_code == 422
    # toggling + adding through the list
    new = [{**t, "enabled": False} if t["id"] == "browser" else t for t in body["tools"]]
    new.append({"id": "mcp-files", "type": "mcp", "name": "Files MCP", "enabled": True})
    after = client.put("/agents/worker-browser-1/config", json={"tools": new}).json()
    tools = {t["id"]: t["enabled"] for t in after["tools"]}
    assert tools == {"browser": False, "crm-rest": False, "mcp-files": True}
    assert any(e.type == EventType.TOOL_TOGGLED for e in await read_events(r, keys))
    # roll back to the built-in prompt
    back = client.put("/agents/worker-browser-1/config", json={"prompt_version": 0}).json()
    assert back["prompt_version"] == 0 and len(back["prompt_versions"]) == 1


def test_restart_and_shell_report_missing_docker_honestly(client):
    res = client.post("/agents/worker-browser-1/restart")
    assert res.status_code in (503, 404)
    with client.websocket_connect("/agents/worker-browser-1/exec") as ws:
        msg = ws.receive()
        text = msg.get("text") or (msg.get("bytes") or b"").decode()
        assert "no shell for worker-browser-1" in text
