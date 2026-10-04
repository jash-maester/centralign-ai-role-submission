"""Track J: human escalations through the API (GET/POST /escalations) and the CLI
(`approvals list|answer`). Real Redis, scripted LLM, no CRM."""

from __future__ import annotations

import json
import shutil
from pathlib import Path

import pytest

from api_helpers import api_client
from ledger_core import cli, escalations, ledger, llm
from ledger_core.llm_scripted import ScriptedBackend
from ledger_core.orchestrator_lanes import group_lanes
from ledger_core.protocol import EventType, StepKind, StepStatus
from ledger_core.settings import get_settings
from test_meta_reviewer import FIXTURES, demo_reviews, reviewer, verify_all

S = StepStatus


@pytest.fixture
def scripted(r, keys):
    previous = llm.get_backend()
    llm.set_backend(ScriptedBackend(FIXTURES, r=r, keys=keys))
    yield
    llm.set_backend(previous)


async def escalated_demo(r, keys):
    o, run, reviews = await demo_reviews(r, keys)
    await reviewer(r, keys).run_until_idle()
    await verify_all(r, keys)
    (esc,) = await escalations.list_escalations(r, keys)
    return o, run, esc


async def test_api_lists_and_answers(r, keys, ns, scripted, tmp_path, monkeypatch):
    o, run, esc = await escalated_demo(r, keys)
    pb_dir = tmp_path / "playbooks"
    shutil.copytree("/repo/playbooks", pb_dir)
    monkeypatch.setenv("PLAYBOOK_DIR", str(pb_dir))
    get_settings.cache_clear()
    try:
        with api_client(ns) as c:
            listed = c.get("/escalations").json()
            assert [e["id"] for e in listed] == [esc.id]
            e = listed[0]
            assert e["lane"] == "lead:9" and e["status"] == "open" and e["confidence"] == 0.52
            assert {"question", "options", "tried", "threshold", "step_id", "run_id"} <= set(e)
            assert c.get("/escalations", params={"run_id": "run_other"}).json() == []
            assert c.get(f"/escalations/{esc.id}").json()["id"] == esc.id
            assert c.get("/escalations/esc_missing").status_code == 404

            bad = c.post(f"/escalations/{esc.id}", json={"answer": "create_new"})
            assert bad.status_code == 422 and "not one of the options" in bad.json()["detail"]
            res = c.post(f"/escalations/{esc.id}", json={"answer": "link_account:a-inc", "save_as_rule": True})
            assert res.status_code == 200, res.text
            body = res.json()
            assert body["step_status"] == "ready" and body["fact_key"] == "review:lead:9"
            assert body["rule"]["playbook"] == "event-leads.md"
            assert c.post(f"/escalations/{esc.id}", json={"answer": "skip"}).status_code == 409
            assert c.get("/escalations").json() == []
            assert c.get("/escalations", params={"status": "answered"}).json()[0]["answer"] == "link_account:a-inc"
    finally:
        get_settings.cache_clear()
    assert "link_account:a-inc]" in (pb_dir / "event-leads.md").read_text()

    # the lane is released: reviewer claims the human answer, verifier commits, orchestrator continues
    await reviewer(r, keys).run_until_idle()
    await verify_all(r, keys)
    await o.reconcile(run.id)
    lane = group_lanes(await ledger.list_steps(r, keys, run.id))["lead:9"]
    assert lane[1].status == S.COMMITTED
    assert lane[2].kind == StepKind.CRM_CREATE_CONTACT and lane[2].inputs["account_id"] == "a-inc"


async def test_cli_approvals_list_and_answer(r, keys, ns, scripted, monkeypatch, capsys):
    o, run, esc = await escalated_demo(r, keys)
    monkeypatch.setenv("LEDGER_NS", ns)
    get_settings.cache_clear()
    try:
        assert await cli.main_async(["approvals", "list"]) == 0
        out = capsys.readouterr().out
        assert esc.id in out and "link_account:a-health" in out and "Lumen Health" in out
        assert await cli.main_async(["approvals", "list", "--json", "--run", run.id]) == 0
        assert json.loads(capsys.readouterr().out)[0]["id"] == esc.id
        assert await cli.main_async(["approvals", "answer", esc.id, "nonsense"]) == 2
        assert await cli.main_async(["approvals", "answer", esc.id, "skip", "--note", "unclear account"]) == 0
        out = capsys.readouterr().out
        assert f"answered {esc.id} (lead:9): skip -> fact review:lead:9" in out and "is ready" in out
        assert await cli.main_async(["approvals", "list"]) == 0
        assert "no open escalations" in capsys.readouterr().out
    finally:
        get_settings.cache_clear()
    (answered,) = [e for e in await ledger.run_events(r, keys, run.id) if e.type == EventType.INPUT_ANSWERED]
    assert answered.actor == "human:cli" and answered.payload["note"] == "unclear account"
    await reviewer(r, keys).run_until_idle()
    await verify_all(r, keys)
    sam = await ledger.get_step(r, keys, esc.step_id)
    assert sam.status == S.COMMITTED and sam.claim.data["decision"] == "skip"


async def _drive_crm(r, keys, o, run_id):
    """Act as the api.espocrm worker + verifier for every ready CRM step until nothing moves."""
    from ledger_core import leases
    from ledger_core.protocol import Claim, Verdict

    for _ in range(12):
        await o.reconcile(run_id)
        moved = False
        for s in await ledger.list_steps(r, keys, run_id):
            if s.status != S.READY or s.skill.value != "api.espocrm":
                continue
            f = await leases.acquire(r, keys, s.id, "w", 15_000)
            await ledger.transition(r, keys, s.id, S.LEASED, actor="w", actor_role="worker", fence=f)
            data = {"contact_id": f"c-{s.lane}", "action": "created", "task_id": f"t-{s.lane}"}
            await ledger.claim(r, keys, s.id, Claim(worker="w", fence=f, summary="ok", data=data))
            await leases.release(r, keys, s.id, "w")
            await ledger.commit(r, keys, s.id, Verdict(ok=True, check=s.postcondition.check, reason="ok",
                                                       observed={"contact_id": f"c-{s.lane}"}), {f"step:{s.id}": data})
            moved = True
        if not moved:
            return


async def test_open_escalation_holds_only_its_lane_and_run_pends(r, keys, tmp_path):
    """Sam waits on a human; Ann's lane runs to the end; the run finishes as
    completed_pending_input, resumes on the answer, and completes."""
    from ledger_core.llm_testing import StubLLM
    from ledger_core.protocol import ReviewDecision, RunStatus
    from test_orchestrator import HEADER, _run_with, finish_step

    o, run = await _run_with(r, keys, tmp_path, HEADER + "Ann Lee,ann@x.test,Xco,,US\nSam Ito,sam@lumen.io,Lumen,,\n", [
        {"id": "c1", "text": "File parsed", "check": "file.parsed_rows"},
        {"id": "c2", "text": "Ambiguous rows decided", "check": "review.decided"}])
    lookups = {
        "lead:1": {"result": "none", "owner": "a.chen", "owner_reason": "region", "candidates": []},
        "lead:2": {"result": "none", "owner": None, "owner_reason": "ambiguous_account", "candidates": [],
                   "account_candidates": [{"id": "a-inc", "name": "Lumen Inc", "website": "lumen.io"},
                                          {"id": "a-health", "name": "Lumen Health", "website": "lumen.io"}]},
    }
    for s in await ledger.list_steps(r, keys, run.id):
        if s.kind == StepKind.CRM_SEARCH_CONTACT:
            await finish_step(r, keys, s.id, lookups[s.lane])
    await o.reconcile(run.id)
    mr = reviewer(r, keys)
    with StubLLM().on("meta_reviewer", ReviewDecision, response={
            "decision": "link_account", "value": "Lumen Inc", "confidence": 0.52, "threshold": 0.8}).installed():
        assert await mr.run_until_idle() == 1
    (esc,) = await escalations.list_escalations(r, keys, run_id=run.id)
    assert esc.lane == "lead:2"
    await _drive_crm(r, keys, o, run.id)

    lanes = group_lanes(await ledger.list_steps(r, keys, run.id))
    assert [s.status for s in lanes["lead:1"]] == [S.COMMITTED] * 3  # search, create, task: not held
    assert lanes["lead:2"][-1].status == S.INPUT_REQUIRED
    run = await ledger.get_run(r, keys, run.id)
    assert run.status == RunStatus.COMPLETED_PENDING_INPUT
    assert {c.id: c.status for c in run.criteria} == {"c1": "verified", "c2": "pending"}

    await escalations.answer(r, keys, esc.id, "link_account:a-health")
    await mr.start()  # a live reviewer serves queue:review: the run is moving again
    run = await o.reconcile(run.id)
    assert run.status == RunStatus.RUNNING
    await mr.run_until_idle()
    await verify_all(r, keys)
    await _drive_crm(r, keys, o, run.id)
    lane2 = group_lanes(await ledger.list_steps(r, keys, run.id))["lead:2"]
    assert [s.kind for s in lane2] == [StepKind.CRM_SEARCH_CONTACT, StepKind.REVIEW_AMBIGUITY,
                                       StepKind.CRM_CREATE_CONTACT, StepKind.CRM_CREATE_TASK]
    assert all(s.status == S.COMMITTED for s in lane2) and lane2[2].inputs["account_id"] == "a-health"
    run = await o.reconcile(run.id)
    assert run.status == RunStatus.COMPLETED
