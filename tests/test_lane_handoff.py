"""Track N, through the orchestrator (real Redis, stub LLM; no CRM, no network):

- per-email approvals: the batch judge (ONE call) approves the drafts that clear
  the threshold; the one that does not gets its own review.approval step for its
  lane and its own escalation, and only that lane waits;
- a dead email draft does not fail the run: the lane is handed to a human
  (human.decide, escalation with "send manually / skip" and every attempt that
  was tried), the run ends completed_pending_input and the report shows the
  lane with its reason; answering "manual" lets the run complete.
"""

from __future__ import annotations

from ledger_core import approval, escalations, ledger, report
from ledger_core.judge import BatchJudgeVerdict
from ledger_core.llm_testing import StubLLM
from ledger_core.orchestrator_handoff import handoff_key
from ledger_core.orchestrator_lanes import lane_outcomes
from ledger_core.protocol import EventType, RunStatus, Skill, StepKind, StepStatus

from test_meta_reviewer import reviewer, verify_all
from test_orchestrator import HEADER, _run_with, finish_step, reject_step

S, K = StepStatus, StepKind
ANN = "Ann Lee,ann@x.test,Xco,,US\n"
BOB = "Bob Ray,bob@y.test,Yco,,US\n"
CRITERIA = [{"id": "c1", "text": "File parsed", "check": "file.parsed_rows"},
            {"id": "c2", "text": "Follow-ups sent with approval", "check": "email.sent"}]


async def _owner(_owner_id):
    return {"name": "Alex Chen", "email": "a.chen@ledger-demo.test"}


async def _crm_done(r, keys, o, run, lanes: dict[str, str]) -> None:
    """Commit search (none, routed to a.chen), contact (created) and task for each lane."""
    o.owner_directory = _owner
    for lane in lanes:
        search = next(s for s in await ledger.list_steps(r, keys, run.id)
                      if s.lane == lane and s.kind == K.CRM_SEARCH_CONTACT)
        await finish_step(r, keys, search.id, {"result": "none", "owner": "a.chen", "owner_reason": "region",
                                               "candidates": [], "contact_id": None})
    await o.reconcile(run.id)
    for kind in (K.CRM_CREATE_CONTACT, K.CRM_CREATE_TASK):
        for lane in lanes:
            st = next(s for s in await ledger.list_steps(r, keys, run.id) if s.lane == lane and s.kind == kind)
            await finish_step(r, keys, st.id, {"contact_id": f"c-{lane}", "owner": "a.chen", "action": "created"},
                              {"contact_id": f"c-{lane}", "owner": "a.chen"})
        await o.reconcile(run.id)


async def _commit_draft(r, keys, step) -> None:
    """Act as drafter + verifier: commit the draft and its lead:<n>.draft fact."""
    first = step.inputs["first_name"]
    draft = {"lane": step.lane, "to": step.inputs["to"], "subject": f"Good to meet you at Signal Summit, {first}",
             "body": f"Hi {first}, great to meet you at Signal Summit. Talk soon, Alex", "draft_step": step.id,
             "owner": "a.chen", "first_name": first}
    from ledger_core import leases
    from ledger_core.protocol import Claim, Verdict

    fence = await leases.acquire(r, keys, step.id, "drafter-t", 15_000)
    await ledger.transition(r, keys, step.id, S.LEASED, actor="drafter-t", actor_role="worker", fence=fence)
    await ledger.claim(r, keys, step.id, Claim(worker="drafter-t", fence=fence, summary="drafted", data=draft))
    await leases.release(r, keys, step.id, "drafter-t")
    await ledger.commit(r, keys, step.id, Verdict(ok=True, check="email.draft_valid", reason="ok"),
                        {f"step:{step.id}": draft, approval.draft_key(step.lane): draft})


def judge(scores: dict[str, float]) -> StubLLM:
    return StubLLM().on("verifier", BatchJudgeVerdict, response={"items": [
        {"id": lane, "score": s, "flags": [], "reasoning": "fine" if s >= 0.9 else "too pushy"}
        for lane, s in scores.items()]})


async def test_one_low_judge_score_escalates_only_that_lane(r, keys, tmp_path):
    o, run = await _run_with(r, keys, tmp_path, HEADER + ANN + BOB, CRITERIA)
    await _crm_done(r, keys, o, run, {"lead:1": "Ann", "lead:2": "Bob"})
    drafts = [s for s in await ledger.list_steps(r, keys, run.id) if s.kind == K.EMAIL_DRAFT]
    assert sorted(d.lane for d in drafts) == ["lead:1", "lead:2"]
    for d in drafts:
        await _commit_draft(r, keys, d)
    await o.reconcile(run.id)
    [batch] = [s for s in await ledger.list_steps(r, keys, run.id) if s.kind == K.REVIEW_APPROVAL]
    with judge({"lead:1": 0.95, "lead:2": 0.55}).installed() as stub:
        await reviewer(r, keys).run_until_idle()
    await verify_all(r, keys)
    assert (await ledger.get_step(r, keys, batch.id)).status == S.COMMITTED

    run = await o.reconcile(run.id)
    steps = await ledger.list_steps(r, keys, run.id)
    single = [s for s in steps if s.kind == K.REVIEW_APPROVAL and s.lane]
    assert [s.lane for s in single] == ["lead:2"] and single[0].inputs["decision_key"] == "approval:lead:2"
    sends = [s for s in steps if s.kind == K.EMAIL_SEND]
    assert [s.lane for s in sends] == ["lead:1"] and batch.id in sends[0].depends_on  # lead:1 goes out at once

    with judge({"lead:2": 0.55}).installed() as again:
        await reviewer(r, keys).run_until_idle()  # the single step escalates without another judge call
    assert len(stub.calls) == 1 and again.calls == []
    [esc] = await escalations.list_escalations(r, keys, run_id=run.id)
    assert esc.lane == "lead:2" and esc.step_id == single[0].id and [o_.value for o_ in esc.options] == [
        "approve", "reject"]
    assert any("0.55" in t for t in esc.tried) and "0.55" in esc.question
    lanes = {x["lane"]: x for x in lane_outcomes(await ledger.list_steps(r, keys, run.id),
                                                  await ledger.get_facts(r, keys, run.id))}
    assert lanes["lead:2"]["status"] == "waiting" and lanes["lead:1"]["status"] != "waiting"

    # the human approves lead:2: its own send depends on its own approval step
    await escalations.answer(r, keys, esc.id, "approve", by="tester")
    await reviewer(r, keys).run_until_idle()
    await verify_all(r, keys)
    await o.reconcile(run.id)
    steps = await ledger.list_steps(r, keys, run.id)
    send2 = next(s for s in steps if s.kind == K.EMAIL_SEND and s.lane == "lead:2")
    assert single[0].id in send2.depends_on
    facts = await ledger.get_facts(r, keys, run.id)
    assert approval.approval_for(facts, "lead:2")["decided_by"] == "human"
    assert facts[approval.BATCH_KEY]["approved"] == ["lead:1", "lead:2"]
    assert facts[approval.BATCH_KEY]["escalated"] == {}


async def test_dead_draft_hands_the_lane_to_a_human_and_the_run_still_finishes(r, keys, tmp_path):
    o, run = await _run_with(r, keys, tmp_path, HEADER + ANN, CRITERIA)
    await _crm_done(r, keys, o, run, {"lead:1": "Ann"})
    [draft] = [s for s in await ledger.list_steps(r, keys, run.id) if s.kind == K.EMAIL_DRAFT]
    for _ in range(draft.max_attempts):
        last = await reject_step(r, keys, draft.id, reason="LLM judge failed the draft: score 0.31 < 0.6: pushy")
    assert last.status == S.DEAD
    run = await o.reconcile(run.id)

    # -- the lane waits on a human; the run is not failed -----------------------------
    assert run.status == RunStatus.COMPLETED_PENDING_INPUT
    steps = await ledger.list_steps(r, keys, run.id)
    [hand] = [s for s in steps if s.kind == K.HUMAN_DECIDE]
    assert hand.lane == "lead:1" and hand.skill == Skill.REVIEW and hand.inputs["dead_step"] == draft.id
    assert [o_["value"] for o_ in hand.inputs["options"]] == ["manual", "skip"]
    assert len(hand.inputs["tried"]) == 3 and "score 0.31" in hand.inputs["tried"][0]
    lane = lane_outcomes(steps, await ledger.get_facts(r, keys, run.id))[0]
    assert lane["status"] == "waiting" and "score 0.31" in lane["reason"]
    crit = {c.check: c for c in run.criteria}
    assert crit["email.sent"].status == "pending" and "waiting on lead:1" in crit["email.sent"].evidence
    rep = await report.build_report(r, keys, run.id)
    [lead] = rep["leads"]
    assert lead["outcome"] == "created" and "score 0.31" in lead["outcome_detail"]
    assert lead["email_status"].startswith("waiting on you") and "score 0.31" in rep["markdown"]

    # -- the meta-reviewer escalates it (one Escalation for the lane, what was tried) --
    await reviewer(r, keys).run_until_idle()
    [esc] = await escalations.list_escalations(r, keys, run_id=run.id)
    assert esc.lane == "lead:1" and esc.step_id == hand.id and [o_.value for o_ in esc.options] == ["manual", "skip"]
    assert sum("attempt" in t for t in esc.tried) == 3
    ev = [e for e in await ledger.run_events(r, keys, run.id) if e.type == EventType.REVIEW_ESCALATED]
    assert ev[-1].payload["lane"] == "lead:1"
    rep = await report.build_report(r, keys, run.id)
    assert rep["decision_counts"]["open"] == 1 and "still open" in rep["summary"]

    # -- "I'll send it manually": the lane counts as handled, the run completes --------
    await escalations.answer(r, keys, esc.id, "manual", by="tester")
    await reviewer(r, keys).run_until_idle()
    await verify_all(r, keys)
    assert (await ledger.get_step(r, keys, hand.id)).status == S.COMMITTED
    assert (await ledger.get_facts(r, keys, run.id))[handoff_key("lead:1")]["decision"] == "manual"
    run = await o.reconcile(run.id)
    assert run.status == RunStatus.COMPLETED, [(c.id, c.status, c.evidence) for c in run.criteria]
    crit = {c.check: c for c in run.criteria}
    assert "handled by you: lead:1 (sent manually by you" in crit["email.sent"].evidence
    rep = await report.build_report(r, keys, run.id)
    assert rep["leads"][0]["email_status"] == "sent manually by you"
    assert rep["decision_counts"] == {"total": 1, "auto": 0, "human": 1, "open": 0}
    assert "1 decision: 0 made automatically, 1 by you." in rep["summary"]
