"""W2 integration regressions: who may orchestrate a run.

- A hand-written plan (CLI spec / POST /runs with steps) is born `running`
  without criteria; the orchestrator never calls the LLM for it.
- A run pinned to one orchestrator (submit_goal(orchestrator=...), used by a
  scripted `make demo` next to a live service) is left alone by the others.
"""

from __future__ import annotations

from ledger_core import llm
from ledger_core.cli import submit_spec
from ledger_core.orchestrator import Orchestrator, submit_goal
from ledger_core.protocol import RunStatus


class _NoLLM:
    def __getattr__(self, name):  # any LLM use fails the test
        raise AssertionError(f"orchestrator used the LLM ({name}) on a run it must not plan")


async def test_hand_planned_run_never_reaches_the_llm(r, keys):
    spec = {"goal": "hand plan", "steps": [{"ref": "p", "kind": "file.parse", "title": "parse",
                                            "inputs": {"file": "x.csv"},
                                            "postcondition": {"check": "file.parsed_rows", "args": {}}}]}
    run, steps = await submit_spec(r, keys, spec, actor="test")
    assert run.status == RunStatus.RUNNING and not run.criteria
    prev = llm.get_backend()
    llm.set_backend(_NoLLM())
    try:
        out = await Orchestrator(r, keys, run_reaper=False).reconcile(run.id)
    finally:
        llm.set_backend(prev)
    assert out.status == RunStatus.RUNNING  # its step is still open; nothing planned on top
    assert not out.criteria


async def test_pinned_run_is_left_to_its_orchestrator(r, keys, repo):
    run = await submit_goal(r, keys, "pinned goal", orchestrator="orchestrator-local", playbook_dir=f"{repo}/playbooks")
    other = await Orchestrator(r, keys, agent_id="orchestrator", run_reaper=False).reconcile(run.id)
    assert other.status == RunStatus.CREATED and not other.criteria
    free = await submit_goal(r, keys, "unpinned goal", playbook_dir=f"{repo}/playbooks")
    owned_only = Orchestrator(r, keys, agent_id="orchestrator-local", run_reaper=False, owned_only=True)
    assert (await owned_only.reconcile(free.id)).status == RunStatus.CREATED


async def test_pinned_run_waiting_on_a_human_is_adopted_once_its_orchestrator_is_gone(r, keys, repo):
    """W3: a scripted demo exits with the run completed_pending_input; the answer to its
    escalation must still be picked up by the service orchestrator."""
    from ledger_core import agents, ledger

    run = await submit_goal(r, keys, "pinned goal", orchestrator="orchestrator-local", playbook_dir=f"{repo}/playbooks")
    pending = (await ledger.get_run(r, keys, run.id)).model_copy(update={"status": RunStatus.COMPLETED_PENDING_INPUT})
    await r.hset(keys.run(run.id), "json", pending.model_dump_json())
    await agents.set_alive(r, keys, "orchestrator-local")
    svc = Orchestrator(r, keys, agent_id="orchestrator", run_reaper=False)
    await svc.reconcile(run.id)
    assert await r.hget(keys.run(run.id), "orchestrator") == "orchestrator-local"  # owner alive: left alone
    await agents.clear_alive(r, keys, "orchestrator-local")
    await svc.reconcile(run.id)
    assert await r.hget(keys.run(run.id), "orchestrator") == "orchestrator"  # owner gone: adopted
