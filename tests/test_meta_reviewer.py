"""Track J: meta-reviewer unit tests (real Redis, scripted/stub LLM; no CRM, no network).

Covers the scripted demo (Ben Ortiz -> match_existing 0.91, Jo Park -> skip 0.88,
Sam Ito -> 0.52 escalated: Lumen Inc vs Lumen Health), thresholds on both
sides, the escalation shape, policy guards, review.decided, saved playbook
rules, email approvals, and that unrelated lanes continue while an
escalation is open.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from ledger_core import escalations, ledger, llm
from ledger_core.checks.review import review_decided
from ledger_core.config import RunConfig
from ledger_core.judge import BatchJudgeVerdict
from ledger_core.llm_scripted import ScriptedBackend
from ledger_core.llm_testing import StubLLM
from ledger_core.meta_reviewer import (
    FORCED_ACCOUNTS,
    MetaReviewer,
    decide_approval,
    match_option,
)
from ledger_core.orchestrator_lanes import group_lanes, lane_outcomes
from ledger_core.postconditions import CheckContext
from ledger_core.protocol import (
    EventType,
    Postcondition,
    ReviewDecision,
    ReviewOption,
    Skill,
    Step,
    StepKind,
    StepStatus,
)
from ledger_core.verifier import Verifier, default_context
from test_orchestrator import _commit_searches, events, fanned_out

S = StepStatus
K = StepKind
REPO = Path("/repo")
PB_DIR = str(REPO / "playbooks")
FIXTURES = REPO / "tests/fixtures/llm"


def reviewer(r, keys, **kw) -> MetaReviewer:
    kw.setdefault("playbook_dir", PB_DIR)
    return MetaReviewer(r, keys, agent_id="mr-test", use_crm=False, block_ms=200, **kw)


async def verify_all(r, keys) -> int:
    return await Verifier(r, keys, block_ms=200).run_until_idle()


@pytest.fixture
def scripted(r, keys):
    previous = llm.get_backend()
    llm.set_backend(ScriptedBackend(FIXTURES, r=r, keys=keys))
    yield
    llm.set_backend(previous)


async def demo_reviews(r, keys, config=None):
    """Fan out the demo CSV, commit Track G's sample lookups: review steps for
    lead:7 (Ben, probable match), lead:9 (Sam, two Lumen accounts), lead:11 (Jo, phone only)."""
    o, run, steps = await fanned_out(r, keys, {"crm_write_path": "api", **(config or {})})
    await _commit_searches(r, keys, steps)
    await o.reconcile(run.id)
    lanes = group_lanes(await ledger.list_steps(r, keys, run.id))
    reviews = {lane: ls[-1] for lane, ls in lanes.items() if ls[-1].kind == K.REVIEW_AMBIGUITY}
    assert sorted(reviews) == ["lead:11", "lead:7", "lead:9"]
    return o, run, reviews


async def test_scripted_demo_resolves_ben_and_jo_and_escalates_only_sam(r, keys, scripted):
    o, run, reviews = await demo_reviews(r, keys)
    assert await reviewer(r, keys).run_until_idle() == 3
    assert await verify_all(r, keys) >= 2

    st = {lane: (await ledger.get_step(r, keys, s.id)).status for lane, s in reviews.items()}
    assert st == {"lead:7": S.COMMITTED, "lead:11": S.COMMITTED, "lead:9": S.INPUT_REQUIRED}

    facts = await ledger.get_facts(r, keys, run.id)
    ben, jo = facts["review:lead:7"], facts["review:lead:11"]
    assert (ben["decision"], ben["value"], ben["confidence"], ben["threshold"]) == ("match_existing", "c-benjamin", 0.91, 0.8)
    assert ben["decided_by"] == "meta-reviewer" and ben["model"] == "scripted:meta_reviewer.json"
    assert any("phone" in e for e in ben["evidence"]) and ben["source"] == "llm"
    assert (jo["decision"], jo["confidence"]) == ("skip", 0.88)
    assert "review:lead:9" not in facts

    # E6: the claim (what the report reads) carries confidence, threshold, evidence, model
    ben_step = await ledger.get_step(r, keys, reviews["lead:7"].id)
    assert {"decision", "confidence", "threshold", "evidence", "decided_by", "model", "options"} <= set(ben_step.claim.data)
    assert ben_step.verdict.ok and ben_step.verdict.check == "review.decided"

    (esc,) = await escalations.list_escalations(r, keys)
    assert esc.lane == "lead:9" and esc.step_id == reviews["lead:9"].id and esc.status == "open"
    assert (esc.confidence, esc.threshold) == (0.52, 0.8)
    assert [o_.value for o_ in esc.options] == ["link_account:a-inc", "link_account:a-health", "skip"]
    assert any("asked scripted:meta_reviewer.json: link_account:Lumen Inc at confidence 0.52" in t for t in esc.tried)
    assert any(FORCED_ACCOUNTS in t for t in esc.tried)
    assert esc.question.startswith("'Lumen' matches 2 CRM accounts")
    sam = await ledger.get_step(r, keys, esc.step_id)
    assert sam.inputs["escalation_id"] == esc.id
    assert await r.xlen(keys.queue(Skill.HUMAN.value)) == 1

    types = [e.type for e in await ledger.run_events(r, keys, run.id)]
    assert types.count(EventType.REVIEW_RESOLVED) == 2 and types.count(EventType.REVIEW_ESCALATED) == 1
    assert types.count(EventType.INPUT_REQUESTED) == 1
    (escalated,) = await events(r, keys, run.id, EventType.REVIEW_ESCALATED)
    assert escalated.payload["confidence"] == 0.52 and escalated.payload["forced"] == FORCED_ACCOUNTS

    # the orchestrator continues Ben (update) and closes Jo (skipped); Sam waits; others go on
    await o.reconcile(run.id)
    lanes = group_lanes(await ledger.list_steps(r, keys, run.id))
    assert [s.kind for s in lanes["lead:7"]][-2:] == [K.CRM_UPDATE_CONTACT, K.CRM_CREATE_TASK]
    assert lanes["lead:7"][2].inputs["contact_id"] == "c-benjamin" and lanes["lead:7"][2].status == S.READY
    assert [s.kind for s in lanes["lead:9"]] == [K.CRM_SEARCH_CONTACT, K.REVIEW_AMBIGUITY]
    assert lanes["lead:1"][1].status == S.READY  # unrelated lane is not held by the escalation
    out = {x["lane"]: x["status"] for x in lane_outcomes(await ledger.list_steps(r, keys, run.id),
                                                         await ledger.get_facts(r, keys, run.id))}
    assert out["lead:11"] == "skipped" and out["lead:9"] == "waiting" and out["lead:7"] == "in_progress"


@pytest.mark.parametrize("conf,resolved", [(0.80, True), (0.7999, False)])
async def test_threshold_both_sides(r, keys, conf, resolved):
    o, run, reviews = await demo_reviews(r, keys, {"review_auto_threshold": 0.8})
    stub = StubLLM().on("meta_reviewer", ReviewDecision,
                        response={"decision": "skip", "confidence": conf, "threshold": 0.5, "evidence": ["x"]})
    with stub.installed():
        await reviewer(r, keys).run_until_idle()
    await verify_all(r, keys)
    jo = await ledger.get_step(r, keys, reviews["lead:11"].id)
    assert jo.status == (S.COMMITTED if resolved else S.INPUT_REQUIRED)
    facts = await ledger.get_facts(r, keys, run.id)
    if resolved:
        assert facts["review:lead:11"]["threshold"] == 0.8  # the run's threshold, not the model's
    else:
        esc = await escalations.for_step(r, keys, jo.id)
        assert esc is not None and esc.confidence == round(conf, 4) and esc.threshold == 0.8
        assert "review:lead:11" not in facts


async def test_raising_the_threshold_escalates_ben(r, keys, scripted):
    o, run, reviews = await demo_reviews(r, keys, {"review_auto_threshold": 0.95})
    await reviewer(r, keys).run_until_idle()
    await verify_all(r, keys)
    opened = {e.lane for e in await escalations.list_escalations(r, keys)}
    assert opened == {"lead:7", "lead:9", "lead:11"}


async def test_answer_releases_exactly_that_lane(r, keys, scripted):
    o, run, reviews = await demo_reviews(r, keys)
    await reviewer(r, keys).run_until_idle()
    await verify_all(r, keys)
    (esc,) = await escalations.list_escalations(r, keys)
    before = {s.id: s.status for s in await ledger.list_steps(r, keys, run.id)}

    with pytest.raises(escalations.EscalationError):
        await escalations.answer(r, keys, esc.id, "link_account:nope")
    out = await escalations.answer(r, keys, esc.id, "Link to Lumen Health", by="tester")  # label works too
    assert out["fact_key"] == "review:lead:9" and out["step_status"] == "ready"
    fact = (await ledger.get_facts(r, keys, run.id))["review:lead:9"]
    assert (fact["decision"], fact["value"], fact["decided_by"]) == ("link_account", "a-health", "human")
    assert await escalations.list_escalations(r, keys) == []
    with pytest.raises(escalations.EscalationError, match="already answered"):
        await escalations.answer(r, keys, esc.id, "skip")
    (answered,) = await events(r, keys, run.id, EventType.INPUT_ANSWERED)
    assert answered.payload["answer"] == "link_account:a-health" and answered.payload["lane"] == "lead:9"

    after = {s.id: s.status for s in await ledger.list_steps(r, keys, run.id)}
    changed = {sid for sid in before if before[sid] != after[sid]}
    assert changed == {esc.step_id}

    # the reviewer claims the human decision (no LLM call), the verifier commits, the lane moves on
    with StubLLM().installed() as stub:
        assert await reviewer(r, keys).run_until_idle() == 1
        assert stub.calls == []
    await verify_all(r, keys)
    sam = await ledger.get_step(r, keys, esc.step_id)
    assert sam.status == S.COMMITTED and sam.claim.data["decided_by"] == "human"
    assert sam.verdict.observed["escalation_id"] == esc.id
    await o.reconcile(run.id)
    lane9 = group_lanes(await ledger.list_steps(r, keys, run.id))["lead:9"]
    assert lane9[2].kind == K.CRM_CREATE_CONTACT and lane9[2].inputs["account_id"] == "a-health"
    assert lane9[2].status == S.READY


async def test_save_as_rule_writes_a_temp_playbook_and_decides_next_time(r, keys, scripted, tmp_path):
    pb_dir = tmp_path / "playbooks"
    pb_dir.mkdir()
    original = (REPO / "playbooks/event-leads.md").read_text()
    (pb_dir / "event-leads.md").write_text(original)
    o, run, reviews = await demo_reviews(r, keys)
    await reviewer(r, keys, playbook_dir=str(pb_dir)).run_until_idle()
    (esc,) = await escalations.list_escalations(r, keys)
    out = await escalations.answer(r, keys, esc.id, "link_account:a-inc", save_as_rule=True, playbook_dir=pb_dir)
    text = (pb_dir / "event-leads.md").read_text()
    assert out["rule"]["section"] == "Escalation rules"
    assert "[rule reason=ambiguous_account company=lumen domain=lumen.io -> link_account:a-inc]" in text
    assert (REPO / "playbooks/event-leads.md").read_text() == original  # the real playbook is untouched
    assert out["rule"]["version"] != None  # noqa: E711 - bumped front-matter version
    ev = [e for e in await ledger.run_events(r, keys, run.id) if e.type == EventType.CONFIG_UPDATED]
    assert ev and ev[-1].payload["scope"] == "playbook"

    # next run: the same case resolves from the saved rule, with no LLM call
    o2, run2, reviews2 = await demo_reviews(r, keys)
    with StubLLM().on("meta_reviewer", ReviewDecision,
                      response={"decision": "skip", "confidence": 0.9, "threshold": 0.8}).installed() as stub:
        await reviewer(r, keys, playbook_dir=str(pb_dir)).run_until_idle()
    assert len(stub.calls) == 2  # Ben and Jo asked the model; Sam did not
    await verify_all(r, keys)
    fact = (await ledger.get_facts(r, keys, run2.id))["review:lead:9"]
    assert (fact["decision"], fact["value"], fact["confidence"], fact["source"]) == ("link_account", "a-inc", 1.0, "rule")
    assert fact["evidence"][0].startswith("playbook rule:")


async def test_model_outage_and_bad_answers_escalate(r, keys):
    o, run, reviews = await demo_reviews(r, keys)
    stub = StubLLM()
    stub.on("meta_reviewer", ReviewDecision, responses=[
        {"decision": "link_account", "value": "acc-unknown", "confidence": 0.99, "threshold": 0.8},
        llm.LLMBudgetExhausted("budget gone"), llm.LLMBudgetExhausted("budget gone")])
    with stub.installed():
        await reviewer(r, keys).run_until_idle()
    opened = await escalations.list_escalations(r, keys)
    assert {e.lane for e in opened} == {"lead:7", "lead:9", "lead:11"}
    tried = " | ".join(t for e in opened for t in e.tried)
    assert "is not one of the options" in tried and "unavailable" in tried


def test_match_option_forms():
    opts = [ReviewOption(label="Link to Lumen Inc", value="link_account:a1"),
            ReviewOption(label="Link to Lumen Health", value="link_account:a2"),
            ReviewOption(label="Skip this lead", value="skip")]

    def d(dec, val=None):
        return ReviewDecision(decision=dec, value=val, confidence=1, threshold=0.8)

    assert match_option(d("link_account", "a2"), opts).value == "link_account:a2"
    assert match_option(d("link_account", "link_account:a1"), opts).value == "link_account:a1"
    assert match_option(d("link_account", "Lumen Health"), opts).value == "link_account:a2"
    assert match_option(d("link_account", "Lumen"), opts) is None  # ambiguous name
    assert match_option(d("link_account"), opts) is None
    assert match_option(d("skip"), opts).value == "skip"
    assert match_option(d("create_new"), opts) is None


# ---------------------------------------------------------------------------
# review.decided
# ---------------------------------------------------------------------------


def _review_step(**kw) -> Step:
    return Step(run_id="run_x", kind=kw.pop("kind", K.REVIEW_AMBIGUITY), skill=Skill.REVIEW, lane="lead:7",
                inputs={"options": [{"label": "A", "value": "match_existing:c1"}, {"label": "B", "value": "create_new"}]},
                postcondition=Postcondition(check="review.decided", args={"lane": "lead:7", "decision_key": "review:lead:7"}),
                **kw)


async def test_review_decided_check():
    step = _review_step()
    ctx = lambda facts, claim, cfg=None: CheckContext(  # noqa: E731
        run_id="run_x", step_id=step.id, claim=claim, facts=facts,
        extra={"step": step, "config": cfg or RunConfig()})
    args = {"lane": "lead:7", "decision_key": "review:lead:7"}
    ok = {"decision": "match_existing", "value": "c1", "confidence": 0.85, "threshold": 0.8, "decided_by": "meta-reviewer"}
    claim = {**ok, "value": "match_existing:c1"}
    assert (await review_decided(args, {}, ctx({"review:lead:7": ok}, claim))).ok
    assert not (await review_decided(args, {}, ctx({}, claim))).ok  # no fact
    res = await review_decided(args, {}, ctx({"review:lead:7": ok}, claim, RunConfig(review_auto_threshold=0.9)))
    assert not res.ok and "must escalate" in res.reason
    res = await review_decided(args, {}, ctx({"review:lead:7": ok}, {**claim, "decision": "create_new", "value": None}))
    assert not res.ok and "claim says" in res.reason
    bad = {**ok, "value": "c9"}
    assert not (await review_decided(args, {}, ctx({"review:lead:7": bad}, {**bad}))).ok  # not an option
    forced = {**ok, "forced_reason": "x"}
    assert not (await review_decided(args, {}, ctx({"review:lead:7": forced}, forced))).ok
    human = {**ok, "decided_by": "human", "escalation_id": "esc_1"}
    assert not (await review_decided(args, {}, ctx({"review:lead:7": human}, human))).ok  # no escalation record


# ---------------------------------------------------------------------------
# approvals (review.approval, Track K's batch)
# ---------------------------------------------------------------------------

DRAFTS = [{"id": "lead:1", "to": "priya@northwind.com", "subject": "Good to meet you at Signal Summit, Priya",
           "body": "Hi Priya, great to meet you at Signal Summit. Talk soon, Alex", "checks_ok": True},
          {"id": "lead:3", "to": "lena@kestrel-labs.io", "subject": "Good to meet you at Signal Summit, Lena",
           "body": "Hi Lena, thanks for stopping by at Signal Summit. Best, Rui", "checks_ok": True}]


def judge_stub(*scores, flags=None) -> StubLLM:
    items = [{"id": d["id"], "score": s, "flags": (flags or {}).get(d["id"], []), "reasoning": "ok"}
             for d, s in zip(DRAFTS, scores)]
    return StubLLM().on("verifier", BatchJudgeVerdict, response={"items": items})


def approval_step(drafts=DRAFTS) -> Step:
    return Step(run_id="run_x", kind=K.REVIEW_APPROVAL, skill=Skill.REVIEW, lane="emails",
                title="Approve follow-up emails", inputs={"drafts": drafts},
                postcondition=Postcondition(check="review.decided", args={"decision_key": "approval:emails"}))


@pytest.mark.parametrize("scores,cfg,auto,why", [
    ((0.95, 0.92), {}, True, None),
    ((0.95, 0.90), {}, True, None),
    ((0.95, 0.89), {}, False, None),
    ((0.95, 0.95), {"always_ask_human_email": True}, False, "always_ask_human_email"),
    ((0.95, 0.95), {"llm_judge_enabled": False}, False, "llm_judge_enabled"),
])
async def test_approval_rules(scores, cfg, auto, why):
    with judge_stub(*scores).installed() as stub:
        out = await decide_approval(approval_step(), {"drafts": DRAFTS}, RunConfig(**cfg))
    assert out.auto is auto
    assert out.decision.confidence == (min(scores) if not why else 0.0)
    if why:
        assert why in out.forced_reason and stub.calls == []  # no judge call when a human must decide
    else:
        assert len(stub.calls) == 1  # one batch call for every draft


async def test_approval_flags_and_failed_checks_escalate():
    with judge_stub(0.97, 0.97, flags={"lead:3": ["pricing"]}).installed():
        out = await decide_approval(approval_step(), {"drafts": DRAFTS}, RunConfig())
    assert not out.auto and out.forced_reason == "judge raised policy flags"
    bad = [DRAFTS[0], {**DRAFTS[1], "checks_ok": False, "check_reason": "subject mismatch"}]
    with judge_stub(0.97, 0.97).installed():
        out = await decide_approval(approval_step(bad), {"drafts": bad}, RunConfig())
    assert not out.auto and "deterministic" in out.forced_reason


async def test_approval_step_end_to_end(r, keys):
    run = await ledger.create_run(r, keys, goal="approve emails", actor="test")
    step = approval_step()
    step.run_id = run.id
    await ledger.create_steps(r, keys, [step], actor="test")
    await ledger.transition(r, keys, step.id, S.READY, actor="test", actor_role="orchestrator")
    with judge_stub(0.96, 0.93).installed():
        await reviewer(r, keys).run_until_idle()
    await verify_all(r, keys)
    st = await ledger.get_step(r, keys, step.id)
    assert st.status == S.COMMITTED
    fact = (await ledger.get_facts(r, keys, run.id))["approval:emails"]
    assert fact["decision"] == "approve" and fact["confidence"] == 0.93 and fact["scores"] == {"lead:1": 0.96, "lead:3": 0.93}
    assert [e.type for e in await ledger.run_events(r, keys, run.id)].count(EventType.APPROVAL_AUTO) == 1
