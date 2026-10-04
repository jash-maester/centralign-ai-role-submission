"""D2/D3/A5: the verifier core and the file.parsed_rows check."""

from __future__ import annotations

import copy

import pytest

from ledger_core import ledger, postconditions
from ledger_core.events import read_events
from ledger_core.postconditions import CheckContext, CheckResult
from ledger_core.protocol import Claim, EventType, Postcondition, StepKind
from ledger_core.verifier import Verifier, default_context, facts_from_claim
from ledger_core.workers.parser import parse_file

from ledger_helpers import CSV, S, drive_to, new_step

postconditions.load_all()


@pytest.fixture(scope="module")
def good() -> dict:
    return parse_file(CSV).as_claim_data()


async def _check(claim: dict | None, args=None, expect=None, data_dir="/repo/data") -> CheckResult:
    ctx = CheckContext(run_id="run_x", claim=claim, data_dir=data_dir)
    return await postconditions.run_check("file.parsed_rows", args or {"file": "event_attendees.csv"},
                                          expect or {}, ctx)


async def test_genuine_parse_passes(good):
    res = await _check(good, expect={"total_rows": 12, "usable": 10, "flagged": 2,
                                     "required_columns": ["name", "email", "company"]})
    assert res.ok, res.reason
    assert res.observed["file_rows"] == 12 and res.observed["claimed_usable"] == 10


async def test_false_claim_with_no_data_fails():
    res = await _check({})
    assert not res.ok and "0 of 12 rows" in res.reason


async def test_fabricated_email_fails(good):
    bad = copy.deepcopy(good)
    bad["rows"][0]["email"] = "someone@else.com"
    res = await _check(bad)
    assert not res.ok and "row 1: email" in res.reason


async def test_dropped_row_fails(good):
    bad = copy.deepcopy(good)
    bad["rows"] = bad["rows"][1:]
    res = await _check(bad)
    assert not res.ok and "row accounting" in res.reason


async def test_usable_row_wrongly_flagged_phone_only_fails(good):
    bad = copy.deepcopy(good)
    row1 = bad["rows"].pop(0)
    bad["flagged"].append({"row": 1, "reason": "phone_only", "record": row1})
    res = await _check(bad)
    assert not res.ok and "flagged phone_only" in res.reason


async def test_duplicate_kept_as_usable_fails(good):
    bad = copy.deepcopy(good)
    dup = next(f for f in bad["flagged"] if f["reason"] == "duplicate_in_file")
    bad["flagged"].remove(dup)
    bad["rows"].append(dup["record"])
    res = await _check(bad)
    assert not res.ok and "already used by row 3" in res.reason


async def test_sha_mismatch_and_expectations(good):
    bad = copy.deepcopy(good)
    bad["sha256"] = "0" * 64
    assert "sha256" in (await _check(bad)).reason
    res = await _check(good, expect={"usable": 11})
    assert not res.ok and "expected usable=11" in res.reason


async def test_missing_file_and_required_columns(good, tmp_path):
    assert "not found" in (await _check(good, args={"file": "nope.csv"})).reason
    p = tmp_path / "x.csv"
    p.write_text("Name,Company\nAda,Engines\n")
    res = await _check({"rows": [], "flagged": []}, args={"file": str(p)})
    assert not res.ok and "required columns: email" in res.reason


async def _claimed(r, keys, data: dict, **kw):
    step = await new_step(r, keys, **kw)
    info = await drive_to(r, keys, step, S.LEASED)
    await ledger.claim(r, keys, step.id, Claim(worker="w1", fence=info["fence"], summary="parsed", data=data))
    return step


async def test_verifier_commits_facts_only_after_verification(r, keys, good):
    v = Verifier(r, keys, block_ms=100)
    step = await _claimed(r, keys, good)
    assert await ledger.get_facts(r, keys, step.run_id) == {}  # claimed is not a fact (A5)
    s = await v.verify(step.id)
    assert s.status == S.COMMITTED and s.verdict.ok
    facts = await ledger.get_facts(r, keys, step.run_id)
    assert sorted(k for k in facts if k.startswith("lead:")) == sorted(f"lead:{n}" for n in
                                                                       [1, 2, 3, 4, 5, 7, 8, 9, 10, 12])
    assert facts["lead:2"]["email"] == "marcus.lee@acme.com" and facts["lead:2"]["region"] == "EMEA"
    assert facts["parse.summary"]["usable_rows"] == [1, 2, 3, 4, 5, 7, 8, 9, 10, 12]
    assert {f["row"]: f["reason"] for f in facts["parse.summary"]["flagged"]} == {6: "duplicate_in_file",
                                                                                 11: "phone_only"}
    records = await ledger.get_fact_records(r, keys, step.run_id)
    assert records["lead:1"].source_step == step.id
    types = [e.type for e in await read_events(r, keys, run_id=step.run_id)]
    assert types.count(EventType.FACT_COMMITTED) == 11
    assert types.index(EventType.STEP_VERIFIED) < types.index(EventType.STEP_COMMITTED) \
        < types.index(EventType.FACT_COMMITTED)


async def test_verifier_rejects_with_reason_and_requeues(r, keys):
    v = Verifier(r, keys, block_ms=100)
    step = await _claimed(r, keys, {})
    s = await v.verify(step.id)
    assert s.status == S.READY and s.history[-1].outcome == "rejected"
    assert "0 of 12 rows" in s.history[-1].verdict.reason
    assert await ledger.get_facts(r, keys, step.run_id) == {}
    assert await r.xlen(keys.queue("file.parse")) == 2  # first ready + requeue


async def test_verifier_dead_at_max_attempts(r, keys):
    v = Verifier(r, keys, block_ms=100)
    step = await new_step(r, keys, max_attempts=2)
    await ledger.transition(r, keys, step.id, S.READY, actor="o", actor_role="orchestrator")
    for expected in (S.READY, S.DEAD):
        from ledger_helpers import lease

        fence = await lease(r, keys, step.id, "w1")
        await ledger.claim(r, keys, step.id, Claim(worker="w1", fence=fence, summary="x", data={}))
        await r.delete(keys.lease(step.id))
        s = await v.verify(step.id)
        assert s.status == expected
    assert [h.outcome for h in s.history] == ["rejected", "rejected"]


async def test_context_factory_judge_and_reject_policy_are_pluggable(r, keys, good):
    seen = {}

    async def factory(step, r_, keys_):
        ctx = await default_context(step, r_, keys_)
        ctx.data_dir = "/repo/data"
        ctx.crm = "fake-crm"
        seen["ctx"] = ctx
        return ctx

    async def judge(step, result, ctx):
        return CheckResult(False, "judge: tone too pushy", {"score": 0.4})

    async def hold(step, verdict, cfg):
        return None

    v = Verifier(r, keys, context_factory=factory, judge=judge, reject_policy=hold)
    step = await _claimed(r, keys, good)
    s = await v.verify(step.id)
    assert seen["ctx"].crm == "fake-crm" and seen["ctx"].claim == good
    assert s.status == S.REJECTED and s.verdict.reason == "judge: tone too pushy"


async def test_crashing_check_is_a_rejection(r, keys):
    from ledger_core.postconditions import REGISTRY

    async def boom(args, expect, ctx):
        raise RuntimeError("kaboom")

    REGISTRY["review.decided"], old = boom, REGISTRY.get("review.decided")
    try:
        v = Verifier(r, keys)
        step = await _claimed(r, keys, {}, kind=StepKind.REVIEW_AMBIGUITY,
                              postcondition=Postcondition(check="review.decided"), skill="review")
        s = await v.verify(step.id)
        assert s.status == S.READY and "kaboom" in s.history[-1].verdict.reason
    finally:
        if old is None:
            REGISTRY.pop("review.decided", None)
        else:
            REGISTRY["review.decided"] = old


async def test_fact_refs_in_postcondition_args_are_resolved(r, keys, good):
    run_step = await new_step(r, keys)
    await ledger.commit_fact(r, keys, run_step.run_id, "input.file", "event_attendees.csv",
                             source_step="stp_seed", actor="test")
    step = await _claimed(r, keys, good, run_id=run_step.run_id,
                          postcondition=Postcondition(check="file.parsed_rows", args={"file": "fact:input.file"}))
    v = Verifier(r, keys)

    async def factory(s, r_, k_):
        ctx = await default_context(s, r_, k_)
        ctx.data_dir = "/repo/data"
        return ctx

    v.context_factory = factory
    s = await v.verify(step.id)
    assert s.status == S.COMMITTED, s.verdict


def test_default_facts_for_unregistered_kind():
    from ledger_core.protocol import Step

    # (email.send has an extractor since Track K; run.verify stays unregistered)
    step = Step(run_id="r", kind=StepKind.RUN_VERIFY, skill="review",
                postcondition=Postcondition(check="run.criteria_met"))
    claim = Claim(worker="w", fence=1, summary="sent", data={"message_id": "m1"})
    assert facts_from_claim(step, claim, CheckResult(True, "ok")) == {f"step:{step.id}": {"message_id": "m1"}}


async def test_verify_ignores_steps_not_claimed(r, keys):
    step = await new_step(r, keys)
    assert await Verifier(r, keys).verify(step.id) is None
