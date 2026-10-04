"""Per-agent config: versioned prompts, layer toggles (history locked), tool switches (H9, H10)."""

from __future__ import annotations

import asyncio

import pytest

from ledger_core import agent_config as ac
from ledger_core.events import read_events
from ledger_core.prompts import ROLE_INSTRUCTIONS
from ledger_core.protocol import AgentCard, EventType, ToolSpec

CRM_REST = ToolSpec(id="espocrm-rest", type="rest", name="espocrm REST", detail="http://espocrm/api/v1")
CSV = ToolSpec(id="csv.read", type="function", name="csv.read", detail="ledger_core.workers.parser")
LOCKED = ToolSpec(id="smtp", type="smtp", name="mailpit", locked_reason="Mailer only")

WORKER = AgentCard(id="worker-browser-1", name="Browser operator 1", role="worker", tools=[CRM_REST, CSV, LOCKED])
VERIFIER = AgentCard(id="verifier", name="Verifier", role="verifier", tools=[CRM_REST])


async def events_of(r, keys, type_):
    return [e for e in await read_events(r, keys) if e.type == type_]


async def test_prompt_versions_and_rollback(r, keys):
    aid = "worker-drafter"
    assert await ac.prompt_text(r, keys, aid, "worker") == ROLE_INSTRUCTIONS["worker"]  # version 0
    v1 = await ac.update_prompt(r, keys, aid, "  Write short emails.  ", actor="gui", note="shorter")
    v2 = await ac.update_prompt(r, keys, aid, "Write very short emails.", actor="gui")
    assert (v1.version, v2.version) == (1, 2) and v1.text == "Write short emails."
    assert await ac.prompt_text(r, keys, aid, "worker") == "Write very short emails."
    assert [p.version for p in await ac.list_prompts(r, keys, aid)] == [1, 2]
    cfg = await ac.activate_prompt(r, keys, aid, 1)
    assert cfg.prompt_version == 1 and cfg.prompt_latest == 2
    assert (await ac.get_prompt(r, keys, aid)).text == "Write short emails."
    v3 = await ac.update_prompt(r, keys, aid, "Third.")
    assert v3.version == 3  # versions never reused after a rollback
    evs = await events_of(r, keys, EventType.PROMPT_UPDATED)
    assert [e.payload["version"] for e in evs] == [1, 2, 1, 3]
    assert evs[0].actor == "gui" and evs[0].payload["note"] == "shorter" and evs[2].payload["activated"]
    with pytest.raises(ac.AgentConfigError):
        await ac.update_prompt(r, keys, aid, "   ")
    with pytest.raises(ac.AgentConfigError):
        await ac.activate_prompt(r, keys, aid, 9)


async def test_concurrent_prompt_updates_get_unique_versions(r, keys):
    made = await asyncio.gather(*(ac.update_prompt(r, keys, "orchestrator", f"text {i}") for i in range(6)))
    assert sorted(p.version for p in made) == [1, 2, 3, 4, 5, 6]
    assert (await ac.get_config(r, keys, "orchestrator")).prompt_latest == 6


async def test_layers_toggle_and_history_is_locked(r, keys):
    aid = "worker-parser"
    with pytest.raises(ac.LockedLayerError, match="D3"):
        await ac.set_layer(r, keys, aid, "history", False)
    with pytest.raises(ac.LockedLayerError):
        await ac.set_layer(r, keys, aid, "step", False)
    with pytest.raises(ac.AgentConfigError):
        await ac.set_layer(r, keys, aid, "vibes", False)
    cfg = await ac.set_layer(r, keys, aid, "playbook", False, actor="gui")
    assert ac.effective_layers(cfg) == {"role": True, "playbook": False, "step": True, "history": True, "schema": True}
    await ac.set_layer(r, keys, aid, "playbook", False)  # no change, no event
    evs = await events_of(r, keys, EventType.CONFIG_UPDATED)
    assert len(evs) == 1 and evs[0].payload == {
        "scope": "agent", "agent_id": aid, "field": "layers.playbook", "old": True, "new": False, "revision": 1}


async def test_workers_never_get_crm_rest(r, keys):
    aid = WORKER.id
    cfg = await ac.get_config(r, keys, aid)
    tools = {t.id: t for t in ac.effective_tools(WORKER, cfg)}
    assert tools["espocrm-rest"].enabled is False and tools["espocrm-rest"].locked_reason == ac.CRM_REST_LOCK
    with pytest.raises(ac.ToolLockedError, match="CRM REST"):
        await ac.set_tool(r, keys, aid, "espocrm-rest", True, card=WORKER)
    with pytest.raises(ac.ToolLockedError, match="Mailer only"):
        await ac.set_tool(r, keys, aid, "smtp", False, card=WORKER)
    added = await ac.add_tool(r, keys, aid, ToolSpec(id="crm2", type="rest", name="EspoCRM API"), card=WORKER)
    crm2 = next(t for t in ac.effective_tools(WORKER, added) if t.id == "crm2")
    assert crm2.enabled is False and crm2.locked_reason == ac.CRM_REST_LOCK
    # The verifier owns that channel and may switch it.
    vcfg = await ac.set_tool(r, keys, "verifier", "espocrm-rest", False, card=VERIFIER)
    assert ac.effective_tools(VERIFIER, vcfg)[0].enabled is False


async def test_tool_toggle_add_remove_emit_events(r, keys):
    aid = WORKER.id
    cfg = await ac.set_tool(r, keys, aid, "csv.read", False, card=WORKER, actor="gui")
    assert next(t for t in ac.effective_tools(WORKER, cfg) if t.id == "csv.read").enabled is False
    mcp = ToolSpec(id="playwright-mcp", type="mcp", name="playwright-mcp", detail="stdio")
    cfg = await ac.add_tool(r, keys, aid, mcp, card=WORKER)
    assert any(t.id == "playwright-mcp" and t.enabled for t in ac.effective_tools(WORKER, cfg))
    with pytest.raises(ac.AgentConfigError):
        await ac.add_tool(r, keys, aid, mcp, card=WORKER)
    with pytest.raises(ac.AgentConfigError):
        await ac.remove_tool(r, keys, aid, "csv.read")  # card tools can only be switched off
    cfg = await ac.remove_tool(r, keys, aid, "playwright-mcp")
    assert all(t.id != "playwright-mcp" for t in ac.effective_tools(WORKER, cfg))
    evs = await events_of(r, keys, EventType.TOOL_TOGGLED)
    assert [(e.payload["tool_id"], e.payload["enabled"]) for e in evs] == [
        ("csv.read", False), ("playwright-mcp", True), ("playwright-mcp", False)]
    assert evs[0].actor == "gui" and evs[1].payload["added"] and evs[2].payload["removed"]
    with pytest.raises(ac.AgentConfigError):
        await ac.set_tool(r, keys, aid, "nope", True, card=WORKER)


async def test_apply_update_validates_before_writing(r, keys):
    aid = "meta-reviewer"
    with pytest.raises(ac.LockedLayerError):
        await ac.apply_update(r, keys, aid, prompt="new", layers={"history": False})
    assert (await ac.get_config(r, keys, aid)).prompt_latest == 0  # nothing written
    cfg = await ac.apply_update(r, keys, aid, prompt="Decide carefully.", layers={"schema": False})
    assert cfg.prompt_version == 1 and cfg.layers["schema"] is False
    cfg = await ac.apply_update(r, keys, aid, prompt="Decide carefully.")  # same text: no new version
    assert cfg.prompt_latest == 1
