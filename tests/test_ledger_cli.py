"""CLI: submit a hand-written spec, dependency release, formatting."""

from __future__ import annotations

import json
from pathlib import Path

from ledger_helpers import CSV, S

from ledger_core import cli, ledger
from ledger_core.events import read_events
from ledger_core.protocol import EventType, RunStatus, Skill
from ledger_core.verifier import Verifier
from ledger_core.worker_base import Worker, worker_card
from ledger_core.workers.parser import make_handler


def _spec() -> dict:
    spec = json.loads(Path("/repo/tests/specs/parse_one_step.json").read_text())
    spec["steps"][0]["inputs"]["file"] = CSV
    spec["steps"][0]["postcondition"]["args"]["file"] = CSV
    return spec


async def test_submit_spec_runs_parse_to_committed(r, keys):
    run, [step] = await cli.submit_spec(r, keys, _spec())
    assert (await ledger.get_run(r, keys, run.id)).status == RunStatus.RUNNING
    cfg = await ledger.get_run_config(r, keys, run.id)
    assert cfg.lease_ttl_s == 15 and run.config_hash == cfg.config_hash()
    assert (await ledger.get_step(r, keys, step.id)).status == S.READY
    w = Worker(r, keys, worker_card("p", "p", {"file.parse": ["file.parse"]}), make_handler(), block_ms=150)
    await w.run_until_idle()
    await Verifier(r, keys, block_ms=150).run_until_idle()
    assert (await ledger.get_step(r, keys, step.id)).status == S.COMMITTED
    lines = [cli.format_event(e) for e in await read_events(r, keys, run_id=run.id)]
    assert any("planned -> ready" in x for x in lines)
    assert any("ready -> leased" in x for x in lines)
    assert any("claimed_done -> verified" in x for x in lines)
    assert any("lead:1 = " in x for x in lines)
    assert "committed" in cli.format_steps(await ledger.list_steps(r, keys, run.id))


async def test_dependencies_wait_until_committed(r, keys):
    spec = _spec()
    spec["steps"].append({"ref": "create", "kind": "crm.create_contact", "inputs": {"lead": "fact:lead:1"},
                          "depends_on": ["parse"]})
    run, [parse, create] = await cli.submit_spec(r, keys, spec)
    assert create.depends_on == [parse.id] and create.skill == Skill.BROWSER_ESPOCRM
    assert create.postcondition.check == "crm.contact_exists"
    assert (await ledger.get_step(r, keys, create.id)).status == S.PLANNED
    assert await ledger.release_dependents(r, keys, run.id) == []
    w = Worker(r, keys, worker_card("p", "p", {"file.parse": ["file.parse"]}), make_handler(), block_ms=150)
    await w.run_until_idle()
    await Verifier(r, keys, block_ms=150).run_until_idle()
    assert await ledger.release_dependents(r, keys, run.id) == [create.id]
    assert await r.xlen(keys.queue("browser.espocrm")) == 1
    plan = [e for e in await read_events(r, keys, run_id=run.id) if e.type == EventType.PLAN_CREATED]
    assert [s["id"] for s in plan[0].payload["steps"]] == [parse.id, create.id]


async def test_demo_is_wired_and_example(capsys, monkeypatch):
    # Track G replaced the placeholder: `demo` runs the orchestrator end to end
    # (tests/test_orchestrator_crm.py); here only the wiring, never a live LLM call.
    seen = {}

    async def fake_demo(a):
        seen.update(goal=a.goal, file=a.file, path=a.crm_write_path)
        return 0

    monkeypatch.setattr(cli, "cmd_demo", fake_demo)
    assert await cli.main_async(["demo", "--crm-write-path", "api"]) == 0
    assert seen == {"goal": cli.DEMO_GOAL, "file": "event_attendees.csv", "path": "api"}
    assert await cli.main_async(["example", "parse"]) == 0
    assert json.loads(capsys.readouterr().out)["steps"][0]["kind"] == "file.parse"
