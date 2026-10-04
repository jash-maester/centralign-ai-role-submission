"""Track F, D4: the LLM judge is layered on email.draft_valid, only after the
deterministic check passed, with role "verifier" (StubLLM; no network)."""

from __future__ import annotations

import pytest
from ledger_helpers import S, lease, new_run

from ledger_core import judge, leases, ledger
from ledger_core.config import RunConfig
from ledger_core.judge import BatchJudgeVerdict, JudgeVerdict, judge_drafts, make_judge
from ledger_core.llm import LLMBudgetExhausted
from ledger_core.llm_testing import StubLLM
from ledger_core.protocol import Claim, Postcondition, Skill, Step, StepKind
from ledger_core.verifier import Verifier

ARGS = {"recipient": "priya@northwind.com", "first_name": "Priya", "event_name": "Signal Summit",
        "owner_name": "Alex Chen"}
GOOD = {"to": "priya@northwind.com", "subject": "Good to meet you at Signal Summit, Priya",
        "body": "Hi Priya,\n\nGreat to meet you at Signal Summit. Happy to continue the conversation "
                "whenever suits you.\n\nBest,\nAlex Chen"}
BAD = {**GOOD, "body": GOOD["body"].replace("Happy to", "We can offer 20% off pricing if you")}


async def _claimed_draft(r, keys, draft: dict, cfg: RunConfig | None = None) -> Step:
    run = await new_run(r, keys)
    if cfg is not None:
        await ledger.set_run_config(r, keys, run.id, cfg, actor="test")
    step = await ledger.create_step(r, keys, Step(
        run_id=run.id, kind=StepKind.EMAIL_DRAFT, skill=Skill.EMAIL_DRAFT, title="draft", lane="lead:1",
        postcondition=Postcondition(check="email.draft_valid", args=ARGS)), actor="test")
    await ledger.transition(r, keys, step.id, S.READY, actor="o", actor_role="orchestrator")
    fence = await lease(r, keys, step.id, "drafter")
    await ledger.claim(r, keys, step.id, Claim(worker="drafter", fence=fence, summary="drafted", data=draft))
    await leases.release(r, keys, step.id, "drafter")
    return step


@pytest.fixture(autouse=True)
def _playbook_dir(monkeypatch, repo):
    monkeypatch.setattr(judge, "PLAYBOOK_DIR", f"{repo}/playbooks")


def _verifier(r, keys) -> Verifier:
    return Verifier(r, keys, judge=make_judge(), block_ms=150)


async def test_judge_skipped_when_deterministic_check_fails(r, keys):
    stub = StubLLM().on("verifier", JudgeVerdict, response={"score": 1.0, "flags": [], "reasoning": "fine"})
    step = await _claimed_draft(r, keys, BAD)
    with stub.installed():
        s = await _verifier(r, keys).verify(step.id)
    assert s.status == S.READY  # rejected -> retry
    assert "policy flags" in s.history[-1].verdict.reason
    assert stub.calls == []  # the judge never ran
    assert s.history[-1].verdict.model is None


async def test_judge_runs_after_pass_and_is_recorded(r, keys):
    stub = StubLLM().on("verifier", JudgeVerdict, response={"score": 0.93, "flags": [], "reasoning": "warm, short"})
    step = await _claimed_draft(r, keys, GOOD)
    with stub.installed():
        s = await _verifier(r, keys).verify(step.id)
    assert s.status == S.COMMITTED
    assert len(stub.calls) == 1 and stub.calls[0].role == "verifier" and stub.calls[0].step_id == step.id
    assert "Signal Summit" in stub.calls[0].text and "Follow-up policy" in stub.calls[0].text
    v = s.verdict
    assert v.model == "stub" and v.observed["judge"]["score"] == 0.93
    assert v.observed["judge"]["reasoning"] == "warm, short" and "judge 0.93" in v.reason


async def test_judge_can_fail_a_deterministic_pass(r, keys):
    stub = StubLLM().on("verifier", JudgeVerdict, responses=[
        {"score": 0.4, "flags": [], "reasoning": "generic, could be sent to anyone"},
        {"score": 0.9, "flags": ["promise"], "reasoning": "implies a delivery date"},
    ])
    step = await _claimed_draft(r, keys, GOOD)
    v = _verifier(r, keys)
    with stub.installed():
        s = await v.verify(step.id)
        assert s.status == S.READY
        assert "LLM judge (stub) failed the draft: score 0.40" in s.history[-1].verdict.reason
        assert s.history[-1].verdict.model == "stub"
        # next attempt: the judge flags a policy -> rejected again
        fence = await lease(r, keys, step.id, "drafter")
        await ledger.claim(r, keys, step.id, Claim(worker="drafter", fence=fence, summary="v2", data=GOOD))
        await leases.release(r, keys, step.id, "drafter")
        s = await v.verify(step.id)
    assert "policy flags promise" in s.history[-1].verdict.reason
    assert ledger.rejection_count(s) == 2


async def test_judge_disabled_by_run_config(r, keys):
    stub = StubLLM()
    step = await _claimed_draft(r, keys, GOOD, RunConfig(llm_judge_enabled=False))
    with stub.installed():
        s = await _verifier(r, keys).verify(step.id)
    assert s.status == S.COMMITTED and stub.calls == []
    assert s.verdict.observed["judge"] == {"skipped": "llm_judge_enabled=false"}


async def test_judge_unavailable_keeps_deterministic_verdict(r, keys):
    stub = StubLLM().on("verifier", JudgeVerdict, exc=LLMBudgetExhausted("daily budget used"))
    step = await _claimed_draft(r, keys, GOOD)
    with stub.installed():
        s = await _verifier(r, keys).verify(step.id)
    assert s.status == S.COMMITTED
    assert "LLMBudgetExhausted" in s.verdict.observed["judge"]["skipped"] and s.verdict.model is None


async def test_other_checks_never_call_the_judge(r, keys):
    from ledger_helpers import drive_to, new_step

    stub = StubLLM()
    step = await new_step(r, keys)
    await drive_to(r, keys, step, S.CLAIMED_DONE)
    with stub.installed():
        await _verifier(r, keys).verify(step.id)
    assert stub.calls == []


async def test_judge_drafts_scores_a_batch_in_one_call():
    drafts = [{"id": "lead:1", **GOOD}, {"id": "lead:3", **GOOD, "to": "lena@kestrel-labs.io"},
              {"id": "lead:4", **GOOD, "to": "dana@helixbio.com"}]
    stub = StubLLM().on("verifier", BatchJudgeVerdict, fn=lambda call: {"items": [
        {"id": "lead:1", "score": 0.95, "flags": [], "reasoning": "good"},
        {"id": "lead:3", "score": 0.7, "flags": ["tone"], "reasoning": "stiff"},
    ]})
    with stub.installed():
        got, model = await judge_drafts(drafts, run_id="run_x")
    assert len(stub.calls) == 1 and model == "stub"
    assert all(d["id"] in stub.calls[0].text for d in drafts)
    assert got["lead:1"].score == 0.95 and judge.passes(got["lead:1"])
    assert got["lead:3"].flags == ["tone"] and not judge.passes(got["lead:3"])
    assert got["lead:4"].score == 0 and got["lead:4"].flags == ["missing"]
    assert await judge_drafts([]) == ({}, None)
