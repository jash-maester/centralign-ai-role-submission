"""Track M: unit tests for the integration fixes the e2e suite surfaced
(real Redis, scripted/stub LLM, no CRM).

- meta-reviewer reads Track K's review.approval batch (inputs.items with the
  lead:N.draft fact refs) and commits the per-email approval:<lane> facts the
  mailer needs, for auto decisions and for human answers;
- the scripted backend honours the model_outage fault (F4) like OpenRouter;
- the orchestrator does not finish a run on a step snapshot it did not act on.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from ledger_core import approval, escalations, faults, ledger, llm
from ledger_core.judge import BatchJudgeVerdict, JudgeVerdict
from ledger_core.llm_scripted import ScriptedBackend
from ledger_core.llm_testing import StubLLM
from ledger_core.meta_reviewer import MetaReviewer
from ledger_core.orchestrator import Orchestrator, _fingerprint
from ledger_core.protocol import EventType, FaultName, Postcondition, RunStatus, Skill, Step, StepKind, StepStatus
from ledger_core.run_config import defaults
from ledger_core.verifier import Verifier
from ledger_core.workers.drafter import EmailDraft

S, K = StepStatus, StepKind
REPO = Path("/repo")
DRAFTS = {
    "lead:1": {"lane": "lead:1", "to": "priya@northwind.com", "subject": "Good to meet you at Signal Summit, Priya",
               "body": "Hi Priya, great to meet you at Signal Summit. Talk soon, Alex Chen", "draft_step": "stp_d1"},
    "lead:3": {"lane": "lead:3", "to": "lena@kestrel-labs.io", "subject": "Good to meet you at Signal Summit, Lena",
               "body": "Hi Lena, thanks for stopping by at Signal Summit. Best, Rita Silva", "draft_step": "stp_d3"},
}


def judge(*scores: float) -> StubLLM:
    items = [{"id": lane, "score": s, "flags": [], "reasoning": "ok"} for lane, s in zip(DRAFTS, scores)]
    return StubLLM().on("verifier", BatchJudgeVerdict, response={"items": items})


async def k_batch(r, keys) -> tuple[str, Step]:
    """A run with two committed draft facts and Track K's approval step shape."""
    run = await ledger.create_run(r, keys, goal="approve emails", actor="test")
    for lane, d in DRAFTS.items():
        await ledger.commit_fact(r, keys, run.id, approval.draft_key(lane), d, source_step=d["draft_step"],
                                 actor="test")
    items = [{"lane": lane, "draft_step": d["draft_step"], "draft": f"fact:{approval.draft_key(lane)}",
              "to": d["to"], "subject": d["subject"], "approval_key": approval.approval_key(lane)}
             for lane, d in DRAFTS.items()]
    step = Step(run_id=run.id, kind=K.REVIEW_APPROVAL, skill=Skill.REVIEW, lane=None, title="Approve 2 emails",
                inputs={"items": items, "decision_key": approval.BATCH_KEY,
                        "options": [{"label": "Approve all", "value": "approve"},
                                    {"label": "Reject", "value": "reject"}]},
                postcondition=Postcondition(check="review.decided", args={"decision_key": approval.BATCH_KEY,
                                                                          "lanes": list(DRAFTS), "kind": "approval"}))
    await ledger.create_steps(r, keys, [step], actor="test")
    await ledger.transition(r, keys, step.id, S.READY, actor="test", actor_role="orchestrator")
    return run.id, step


def reviewer(r, keys) -> MetaReviewer:
    return MetaReviewer(r, keys, agent_id="mr-test", use_crm=False, block_ms=200,
                        playbook_dir=str(REPO / "playbooks"))


async def test_k_batch_auto_approval_commits_per_email_facts(r, keys):
    run_id, step = await k_batch(r, keys)
    with judge(0.95, 0.93).installed() as stub:
        await reviewer(r, keys).run_until_idle()
    assert len(stub.calls) == 1
    await Verifier(r, keys, block_ms=200).run_until_idle()
    assert (await ledger.get_step(r, keys, step.id)).status == S.COMMITTED
    facts = await ledger.get_facts(r, keys, run_id)
    for lane, d in DRAFTS.items():
        rec = approval.approval_for(facts, lane, approval_step=step.id)
        assert approval.is_approved(rec) and rec["source"] == approval.approval_key(lane)
        assert approval.mismatch(rec, d) is None
        assert rec["to"] == d["to"] and rec["draft_step"] == d["draft_step"]
    batch = facts[approval.BATCH_KEY]
    assert batch["decision"] == "approve" and batch["approved"] == sorted(DRAFTS) and batch["confidence"] == 0.93


async def test_k_batch_human_answer_commits_per_email_facts(r, keys):
    run_id, step = await k_batch(r, keys)
    with judge(0.95, 0.70).installed():  # below approval_auto_threshold -> escalate
        await reviewer(r, keys).run_until_idle()
    assert (await ledger.get_step(r, keys, step.id)).status == S.INPUT_REQUIRED
    facts = await ledger.get_facts(r, keys, run_id)
    assert approval.approval_for(facts, "lead:1") is None  # nothing approved yet
    esc = (await escalations.list_escalations(r, keys, run_id=run_id))[0]
    await escalations.answer(r, keys, esc.id, "approve", by="pat")
    await reviewer(r, keys).run_until_idle()
    await Verifier(r, keys, block_ms=200).run_until_idle()
    assert (await ledger.get_step(r, keys, step.id)).status == S.COMMITTED
    facts = await ledger.get_facts(r, keys, run_id)
    for lane in DRAFTS:
        rec = approval.approval_for(facts, lane)
        assert approval.is_approved(rec) and rec["decided_by"] == "human", rec


async def test_scripted_backend_honours_model_outage(r, keys):
    backend = ScriptedBackend(REPO / "tests/fixtures/llm", r=r, keys=keys)
    await faults.set_fault(r, keys, FaultName.MODEL_OUTAGE, shots=1, actor="test", run_id="run_m")
    msgs = [{"role": "system", "content": "[fixture_key: lead:1]"}, {"role": "user", "content": "draft"}]
    out = await backend.complete("worker", msgs, EmailDraft, run_id="run_m", step_id="stp_m")
    assert out.subject.startswith("Good to meet you")
    again = await backend.complete("worker", msgs, EmailDraft, run_id="run_m", step_id="stp_m")
    assert again == out
    evs = [e for e in await ledger.run_events(r, keys, "run_m")]
    fb = [e for e in evs if e.type == EventType.MODEL_FALLBACK]
    consumed = [e for e in evs if e.type == EventType.FAULT_INJECTED and e.payload.get("phase") == "consumed"]
    assert len(fb) == 1 and len(consumed) == 1  # one shot, one fallback
    assert fb[0].payload["to"] == "scripted:drafter.json" and "model_outage" in fb[0].payload["reason"]
    # roles outside MODEL_OUTAGE_ROLES never consume the switch
    await faults.set_fault(r, keys, FaultName.MODEL_OUTAGE, shots=1, actor="test")
    verdict = await backend.complete("verifier", msgs, JudgeVerdict, run_id="run_m")
    assert verdict.score > 0.9
    assert await faults.active(r, keys)
    assert sum(e.type == EventType.MODEL_FALLBACK for e in await ledger.run_events(r, keys, "run_m")) == 1


async def test_orchestrator_does_not_finish_on_a_stale_snapshot(r, keys):
    run = await ledger.create_run(r, keys, goal="race", actor="test")
    await ledger.set_run_status(r, keys, run.id, RunStatus.RUNNING, actor="test")
    step = Step(run_id=run.id, kind=K.FILE_PARSE, skill=Skill.FILE_PARSE, title="parse",
                inputs={"file": "event_attendees.csv"}, postcondition=Postcondition(check="file.parsed_rows"))
    await ledger.create_steps(r, keys, [step], actor="test")
    await ledger.transition(r, keys, step.id, S.DEAD, actor="test", actor_role="orchestrator")
    orch = Orchestrator(r, keys, playbook_dir=str(REPO / "playbooks"), run_reaper=False)
    run = await ledger.get_run(r, keys, run.id)
    cfg = defaults()
    stale = await orch._maybe_finish(run, cfg, basis="not-what-the-ledger-holds-now")
    assert stale.status == RunStatus.RUNNING
    fresh = await orch._maybe_finish(run, cfg, basis=_fingerprint(await ledger.list_steps(r, keys, run.id)))
    assert fresh.status == RunStatus.FAILED  # the dead parse ends the run once judged on current state


@pytest.fixture(autouse=True)
def _no_live_llm():
    prev = llm.get_backend()
    yield
    llm.set_backend(prev)
