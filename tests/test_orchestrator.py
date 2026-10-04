"""Track G: orchestrator unit tests (real Redis, StubLLM; no CRM, no network).

Covers plan/criteria validation and the re-ask loop, understand -> plan ->
plan.created, deterministic fan-out on the demo CSV, lane progression and
binding, dependency release order, replanning after N rejections (B5), the
run.criteria_met sweep and every finish state (D5).
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from ledger_core import leases, ledger, llm
from ledger_core.checks import run as run_checks
from ledger_core.config import RunConfig
from ledger_core.llm_testing import StubLLM
from ledger_core.orchestrator import Orchestrator, submit_goal
from ledger_core.orchestrator_lanes import (
    BindError,
    group_lanes,
    lane_outcomes,
    next_skill,
    parse_decision,
    resolve_binds,
    route,
)
from ledger_core.orchestrator_llm import (
    InvalidOutput,
    Plan,
    Understanding,
    make_plan,
    understand,
    validate_plan,
    validate_understanding,
)
from ledger_core.orchestrator_replan import reject_policy
from ledger_core.postconditions import CheckContext
from ledger_core.protocol import (
    Claim,
    EventType,
    RunStatus,
    Skill,
    StepKind,
    StepStatus,
    Verdict,
)
from ledger_core.verifier import Verifier, default_context
from ledger_core.worker_base import Worker, worker_card
from ledger_core.workers.parser import make_handler

S = StepStatus
K = StepKind
REPO = Path("/repo")
FIXTURES = json.loads((REPO / "tests/fixtures/llm/orchestrator.json").read_text())
UNDERSTANDING = next(f["response"] for f in FIXTURES if f["schema"] == "Understanding")
PLAN = next(f["response"] for f in FIXTURES if f["schema"] == "Plan")
GOAL = "Add the leads from yesterday's event to the CRM and set up follow-ups."
PB_DIR = str(REPO / "playbooks")
DATA = str(REPO / "data")


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------


def stub(understanding=None, plan=None) -> StubLLM:
    s = StubLLM()
    s.on("orchestrator", Understanding, **(understanding or {"response": UNDERSTANDING}))
    s.on("orchestrator", Plan, **(plan or {"response": PLAN}))
    return s


async def no_crm_context(run, facts):
    return CheckContext(run_id=run.id, facts=facts, crm=None, data_dir=DATA)


def orch(r, keys, **kw) -> Orchestrator:
    kw.setdefault("context_factory", no_crm_context)
    return Orchestrator(r, keys, playbook_dir=PB_DIR, run_reaper=False, **kw)


async def submit(r, keys, config=None, input_file="event_attendees.csv"):
    return await submit_goal(r, keys, GOAL, input_file=input_file, config=config, playbook_dir=PB_DIR, actor="test")


async def finish_step(r, keys, step_id: str, data: dict, observed: dict | None = None, worker: str = "w-test"):
    """Act as worker + verifier: lease, claim `data`, commit with `observed`."""
    fence = await leases.acquire(r, keys, step_id, worker, 15_000)
    await ledger.transition(r, keys, step_id, S.LEASED, actor=worker, actor_role="worker", fence=fence)
    await ledger.claim(r, keys, step_id, Claim(worker=worker, fence=fence, summary="done", data=data))
    await leases.release(r, keys, step_id, worker)
    st = await ledger.get_step(r, keys, step_id)
    verdict = Verdict(ok=True, check=st.postcondition.check, reason="ok", observed=observed or {})
    return await ledger.commit(r, keys, step_id, verdict, {f"step:{step_id}": data})


async def reject_step(r, keys, step_id: str, reason: str = "CRM shows nothing", worker: str = "w-test",
                      cfg: RunConfig | None = None):
    fence = await leases.acquire(r, keys, step_id, worker, 15_000)
    await ledger.transition(r, keys, step_id, S.LEASED, actor=worker, actor_role="worker", fence=fence)
    await ledger.claim(r, keys, step_id, Claim(worker=worker, fence=fence, summary="done", data={}))
    await leases.release(r, keys, step_id, worker)
    st = await ledger.get_step(r, keys, step_id)
    verdict = Verdict(ok=False, check=st.postcondition.check, reason=reason)
    then = await reject_policy(st, verdict, cfg or RunConfig())
    return await ledger.reject(r, keys, step_id, verdict, then=then)


async def parse_for_real(r, keys):
    """Run the real parser worker and verifier (file.parsed_rows) on queued steps."""
    worker = Worker(r, keys, worker_card("parser-t", "parser", {"file.parse": ["file.parse"]}),
                    make_handler(DATA), block_ms=200)
    assert await worker.run_until_idle() >= 1

    async def ctx(step, r_, keys_):
        c = await default_context(step, r_, keys_)
        c.data_dir = DATA
        return c

    v = Verifier(r, keys, context_factory=ctx, block_ms=200)
    assert await v.run_until_idle() >= 1


async def planned_run(r, keys, config=None, **kw):
    """Submit, understand and plan with the stub; returns (orchestrator, run)."""
    o = orch(r, keys, **kw)
    run = await submit(r, keys, config)
    with stub().installed():
        run = await o.reconcile(run.id)
    return o, run


async def fanned_out(r, keys, config=None):
    o, run = await planned_run(r, keys, config)
    await parse_for_real(r, keys)
    run = await o.reconcile(run.id)
    return o, run, await ledger.list_steps(r, keys, run.id)


async def events(r, keys, run_id, etype):
    return [e for e in await ledger.run_events(r, keys, run_id) if e.type == etype]


# ---------------------------------------------------------------------------
# validation
# ---------------------------------------------------------------------------


def _plan(**step) -> Plan:
    base = {"ref": "parse", "kind": "file.parse", "inputs": {"file": "event_attendees.csv"},
            "postcondition": {"check": "file.parsed_rows", "args": {"file": "event_attendees.csv"}}}
    base.update(step)
    return Plan.model_validate({"steps": [base]})


def test_fixture_plan_and_criteria_are_valid():
    assert validate_understanding(Understanding.model_validate(UNDERSTANDING)) == []
    assert validate_plan(Plan.model_validate(PLAN), input_file="event_attendees.csv") == []


@pytest.mark.parametrize("bad, needle", [
    ({"postcondition": {"check": "crm.made_up"}}, "unknown check 'crm.made_up'"),
    ({"skill": "browser.salesforce"}, "unknown skill 'browser.salesforce'"),
    ({"skill": "email.send"}, "skill email.send cannot execute file.parse"),
    ({"kind": "crm.teleport"}, "unknown kind 'crm.teleport'"),
    ({"postcondition": {"check": "crm.contact_exists"}}, "does not verify a file.parse step"),
    ({"kind": "crm.create_contact", "postcondition": {"check": "crm.contact_exists"}}, "not allowed in the initial plan"),
    ({"kind": "run.verify", "postcondition": {"check": "run.criteria_met"}}, "no skill executes run.verify"),
    ({"depends_on": ["ghost"]}, "depends_on 'ghost' is not a step ref"),
    ({"inputs": {"file": "other.csv"}}, "parses 'other.csv'"),
])
def test_plan_validation_rejects(bad, needle):
    problems = validate_plan(_plan(**bad), input_file="event_attendees.csv")
    assert any(needle in p for p in problems), problems


def test_plan_needs_a_parse_step_for_the_input_file():
    assert any("needs a file.parse step" in p
               for p in validate_plan(Plan(steps=[]), input_file="event_attendees.csv"))


def test_plan_cycle_detected():
    p = Plan.model_validate({"steps": [
        {"ref": "a", "kind": "file.parse", "postcondition": {"check": "file.parsed_rows"}, "depends_on": ["b"]},
        {"ref": "b", "kind": "file.parse", "postcondition": {"check": "file.parsed_rows"}, "depends_on": ["a"]},
    ]})
    assert "depends_on has a cycle" in validate_plan(p, input_file=None)


@pytest.mark.parametrize("crit, needle", [
    ({"id": "c1", "text": "x", "check": "crm.vibes"}, "unknown check 'crm.vibes'"),
    ({"id": "c1", "text": "x", "check": "run.criteria_met"}, "run.criteria_met is the sweep itself"),
    ({"id": "c1", "text": "", "check": "crm.task_exists"}, "has no text"),
])
def test_understanding_validation_rejects(crit, needle):
    u = Understanding.model_validate({"criteria": [crit]})
    assert any(needle in p for p in validate_understanding(u))
    dup = Understanding.model_validate({"criteria": [UNDERSTANDING["criteria"][0]] * 2})
    assert any("used twice" in p for p in validate_understanding(dup))
    assert validate_understanding(Understanding(criteria=[])) != []


async def test_invalid_plan_is_reasked_with_the_problems():
    bad = {**PLAN, "steps": [{**PLAN["steps"][0], "postcondition": {"check": "crm.made_up"}}]}
    s = stub(plan={"responses": [bad, PLAN]})
    with s.installed():
        plan, asks = await make_plan(GOAL, [], _pb(), input_file="event_attendees.csv")
    assert asks == 2 and plan.steps[0].postcondition.check == "file.parsed_rows"
    calls = s.calls_for("orchestrator", Plan)
    assert len(calls) == 2
    assert "unknown check 'crm.made_up'" in calls[1].text and "Your previous answer was invalid" in calls[1].text
    assert "unknown check" not in calls[0].text


async def test_informational_per_lead_noise_costs_no_reask():
    # a live model once listed skills in per_lead; that must not burn a request
    noisy = {**PLAN, "per_lead": ["crm.search_contact", "browser.espocrm", "review", "crm.create_task"]}
    s = stub(plan={"response": noisy})
    with s.installed():
        plan, asks = await make_plan(GOAL, [], _pb(), input_file="event_attendees.csv")
    assert asks == 1 and plan.per_lead == ["crm.search_contact", "crm.create_task"]


async def test_invalid_criteria_are_reasked_then_give_up():
    bad = {"criteria": [{"id": "c1", "text": "x", "check": "crm.vibes"}]}
    s = stub(understanding={"response": bad})
    with s.installed(), pytest.raises(InvalidOutput) as exc:
        await understand(GOAL, _pb(), input_file="event_attendees.csv")
    assert len(s.calls_for("orchestrator", Understanding)) == 3
    assert "crm.vibes" in str(exc.value)


def _pb():
    from ledger_core import playbook

    return playbook.load("event-leads.md", PB_DIR)


async def test_prompts_carry_no_ids_and_are_stable():
    s = stub()
    with s.installed():
        await understand(GOAL, _pb(), input_file="event_attendees.csv", run_id="run_aaaaaaaaaa")
        await understand(GOAL, _pb(), input_file="event_attendees.csv", run_id="run_bbbbbbbbbb")
    a, b = s.calls_for("orchestrator", Understanding)
    assert a.messages == b.messages
    assert "run_aaaa" not in a.text and "Definitions of done" in a.text and "crm.task_exists" in a.text


# ---------------------------------------------------------------------------
# understand + plan through the orchestrator
# ---------------------------------------------------------------------------


async def test_understand_and_plan_create_criteria_and_parse_step(r, keys):
    o, run = await planned_run(r, keys, {"crm_write_path": "api"})
    assert run.status == RunStatus.RUNNING
    assert [c.check for c in run.criteria] == [c["check"] for c in UNDERSTANDING["criteria"]]
    assert all(c.status == "pending" for c in run.criteria)
    steps = await ledger.list_steps(r, keys, run.id)
    assert [(s.kind, s.skill, s.status) for s in steps] == [(K.FILE_PARSE, Skill.FILE_PARSE, S.READY)]
    assert steps[0].postcondition.check == "file.parsed_rows"
    understood = (await events(r, keys, run.id, EventType.RUN_UNDERSTOOD))[0]
    assert understood.payload["event"]["name"] == "Signal Summit"  # from the input's .meta.json
    assert len(understood.payload["criteria"]) == 6
    assert (await events(r, keys, run.id, EventType.PLAN_CREATED))[0].payload["steps"][0]["kind"] == "file.parse"
    statuses = [e.payload["to"] for e in await events(r, keys, run.id, EventType.RUN_STATUS)]
    assert statuses == ["understanding", "planning", "running"]
    # the run config was stored from the playbook front matter + overrides
    cfg = await ledger.get_run_config(r, keys, run.id)
    assert cfg.crm_write_path == "api" and cfg.replan_after_rejections == 2
    assert run.config_hash == cfg.config_hash()
    # idempotent: another pass changes nothing and asks the LLM nothing
    s = StubLLM()
    with s.installed():
        await o.reconcile(run.id)
    assert s.calls == [] and len(await ledger.list_steps(r, keys, run.id)) == 1


async def test_llm_budget_exhausted_fails_the_run(r, keys):
    o = orch(r, keys)
    run = await submit(r, keys)
    with stub(understanding={"exc": llm.LLMBudgetExhausted("daily budget used: 45/45")}).installed():
        run = await o.reconcile(run.id)
    assert run.status == RunStatus.FAILED
    failed = (await events(r, keys, run.id, EventType.RUN_FAILED))[0]
    assert "LLMBudgetExhausted" in failed.payload["reason"]


async def test_plan_that_stays_invalid_fails_the_run(r, keys):
    o = orch(r, keys)
    run = await submit(r, keys)
    bad = {**PLAN, "steps": [{**PLAN["steps"][0], "kind": "crm.create_contact"}]}
    with stub(plan={"response": bad}).installed():
        run = await o.reconcile(run.id)
    assert run.status == RunStatus.FAILED and await ledger.list_steps(r, keys, run.id) == []
    reasks = [e for e in await events(r, keys, run.id, EventType.RUN_STATUS) if e.payload.get("action") == "re-ask"]
    assert len(reasks) == 3


# ---------------------------------------------------------------------------
# fan-out (B4)
# ---------------------------------------------------------------------------


async def test_fan_out_waits_for_the_parse_commit(r, keys):
    o, run = await planned_run(r, keys)
    await o.reconcile(run.id)
    assert [s.kind for s in await ledger.list_steps(r, keys, run.id)] == [K.FILE_PARSE]


async def test_fan_out_creates_lanes_for_the_demo_csv(r, keys):
    o, run, steps = await fanned_out(r, keys, {"crm_write_path": "api"})
    lanes = group_lanes(steps)
    searches = [s for s in steps if s.kind == K.CRM_SEARCH_CONTACT]
    reviews = [s for s in steps if s.kind == K.REVIEW_AMBIGUITY]
    # 12 rows: row 6 duplicates row 3 (no lane), row 11 is phone-only (review)
    assert sorted(int(s.lane.split(":")[1]) for s in searches) == [1, 2, 3, 4, 5, 7, 8, 9, 10, 12]
    assert [s.lane for s in reviews] == ["lead:11"]
    assert reviews[0].inputs["reason"] == "phone_only" and reviews[0].skill == Skill.REVIEW
    assert reviews[0].inputs["lead"]["name"] == "Jo Park"
    assert len(lanes) == 11 and "lead:6" not in lanes
    parse = next(s for s in steps if s.kind == K.FILE_PARSE)
    for s in searches:
        assert s.skill == Skill.API_ESPOCRM and s.status == S.READY and s.depends_on == [parse.id]
        assert s.inputs["lead"] == f"fact:{s.lane}" and s.postcondition.check == "crm.lookup_matches"
        assert s.postcondition.args["threshold"] == 0.85
    revised = await events(r, keys, run.id, EventType.PLAN_REVISED)
    assert len(revised) == 1 and len(revised[0].payload["steps"]) == 11
    # again: no second fan-out
    await o.reconcile(run.id)
    assert len(await ledger.list_steps(r, keys, run.id)) == len(steps)


@pytest.mark.parametrize("path, skill", [("browser", Skill.BROWSER_ESPOCRM), ("auto", Skill.BROWSER_ESPOCRM),
                                         ("api", Skill.API_ESPOCRM)])
def test_routing_by_crm_write_path(path, skill):
    cfg = RunConfig(crm_write_path=path)
    assert route(K.CRM_CREATE_CONTACT, cfg) == skill
    assert route(K.FILE_PARSE, cfg) == Skill.FILE_PARSE and route(K.REVIEW_AMBIGUITY, cfg) == Skill.REVIEW
    assert next_skill(K.CRM_CREATE_CONTACT, skill, cfg) == (Skill.API_ESPOCRM if path == "auto" else None)


# ---------------------------------------------------------------------------
# lane progression, bindings, dependency release
# ---------------------------------------------------------------------------

LOOKUPS = {
    "lead:1": {"result": "none", "owner": "a.chen", "owner_reason": "region", "candidates": [], "contact_id": None},
    "lead:2": {"result": "matched", "contact_id": "c-marcus", "owner": "r.silva", "owner_reason": "existing_contact",
               "candidates": [{"id": "c-marcus"}], "open_deal": False},
    "lead:7": {"result": "ambiguous", "contact_id": None, "owner": "a.chen", "owner_reason": "account",
               "candidates": [{"id": "c-benjamin", "name": "Benjamin Ortiz", "score": 0.91, "phone_match": True}]},
    "lead:9": {"result": "none", "contact_id": None, "owner": None, "owner_reason": "ambiguous_account",
               "candidates": [], "account_candidates": [{"id": "a-inc", "name": "Lumen Inc", "owner": "r.silva"},
                                                        {"id": "a-health", "name": "Lumen Health", "owner": "a.chen"}]},
}


async def _commit_searches(r, keys, steps, lookups=LOOKUPS):
    for s in steps:
        if s.kind == K.CRM_SEARCH_CONTACT and s.lane in lookups:
            await finish_step(r, keys, s.id, lookups[s.lane])


async def test_lanes_follow_the_lookup_result(r, keys):
    o, run, steps = await fanned_out(r, keys, {"crm_write_path": "api"})
    await _commit_searches(r, keys, steps)
    await o.reconcile(run.id)
    lanes = group_lanes(await ledger.list_steps(r, keys, run.id))
    kinds = {lane: [s.kind for s in ls] for lane, ls in lanes.items()}
    assert kinds["lead:1"] == [K.CRM_SEARCH_CONTACT, K.CRM_CREATE_CONTACT, K.CRM_CREATE_TASK]
    assert kinds["lead:2"] == [K.CRM_SEARCH_CONTACT, K.CRM_UPDATE_CONTACT, K.CRM_CREATE_TASK]
    assert kinds["lead:7"] == [K.CRM_SEARCH_CONTACT, K.REVIEW_AMBIGUITY]  # probable fuzzy match -> review
    assert kinds["lead:9"] == [K.CRM_SEARCH_CONTACT, K.REVIEW_AMBIGUITY]  # Lumen: two accounts -> review
    assert kinds["lead:3"] == [K.CRM_SEARCH_CONTACT]  # lookup not committed yet

    create, task = lanes["lead:1"][1:]
    assert create.status == S.READY and create.skill == Skill.API_ESPOCRM and create.side_effect
    assert create.inputs["owner"] == "a.chen" and create.postcondition.expect == {"owner": "a.chen"}
    assert create.postcondition.args == {"email": "priya@northwind.com"}
    assert task.status == S.PLANNED and task.depends_on == [create.id]
    assert task.inputs["contact_id"] == "bind:contact.contact_id"
    assert task.inputs["subject"] == "Follow up: Signal Summit"
    assert task.postcondition.expect["owner"] == "a.chen" and task.postcondition.expect["due"] == task.inputs["due"]

    update = lanes["lead:2"][1]
    assert update.inputs["contact_id"] == "c-marcus" and update.postcondition.expect["owner"] == "r.silva"
    assert update.postcondition.expect["emails"] == ["marcus.lee@acme.com"]

    rev7, rev9 = lanes["lead:7"][1], lanes["lead:9"][1]
    assert rev7.inputs["reason"] == "probable_match" and rev7.status == S.READY and rev7.skill == Skill.REVIEW
    assert [o_["value"] for o_ in rev7.inputs["options"]] == ["match_existing:c-benjamin", "create_new"]
    assert rev9.inputs["reason"] == "ambiguous_account"
    assert [o_["value"] for o_ in rev9.inputs["options"]] == ["link_account:a-inc", "link_account:a-health", "skip"]

    # dependency release: the task waits for its contact, then gets the verified contact id bound
    await finish_step(r, keys, create.id, {"contact_id": "c-claimed", "action": "created"},
                      observed={"contact_id": "c-priya"})
    await o.reconcile(run.id)
    task = await ledger.get_step(r, keys, task.id)
    assert task.status == S.READY
    assert task.inputs["contact_id"] == "c-priya" == task.postcondition.args["contact_id"]  # REST-observed id wins
    ready = [e for e in await events(r, keys, run.id, EventType.STEP_READY) if e.step_id == task.id]
    assert len(ready) == 1


def test_bindings_resolve_only_from_committed_steps():
    from ledger_core.protocol import Postcondition, Step

    contact = Step(run_id="r", kind=K.CRM_UPDATE_CONTACT, skill=Skill.API_ESPOCRM, lane="lead:2",
                   postcondition=Postcondition(check="crm.contact_exists"), status=S.CLAIMED_DONE,
                   claim=Claim(worker="w", fence=1, summary="x", data={"contact_id": "c1"}))
    with pytest.raises(BindError):
        resolve_binds({"contact_id": "bind:contact.contact_id"}, [contact])
    contact.status = S.COMMITTED
    assert resolve_binds({"a": ["bind:contact.contact_id"]}, [contact]) == {"a": ["c1"]}


async def test_review_decisions_continue_the_lane(r, keys):
    """W3 hook: a decision fact review:<lane> + the committed review step."""
    o, run, steps = await fanned_out(r, keys, {"crm_write_path": "api"})
    await _commit_searches(r, keys, steps)
    await o.reconcile(run.id)
    lanes = group_lanes(await ledger.list_steps(r, keys, run.id))
    decisions = {
        "lead:7": {"decision": "match_existing", "value": "c-benjamin", "confidence": 0.91},
        "lead:9": {"decision": "link_account:a-health", "confidence": 0.95},
        "lead:11": {"decision": "skip", "confidence": 0.88, "reason": "phone-only row"},
    }
    for lane, d in decisions.items():
        review = lanes[lane][-1]
        await ledger.commit_fact(r, keys, run.id, f"review:{lane}", d, source_step=review.id, actor="meta-reviewer")
        await finish_step(r, keys, review.id, {"decision": d["decision"]}, worker="meta-reviewer")
    await o.reconcile(run.id)
    lanes = group_lanes(await ledger.list_steps(r, keys, run.id))
    upd = lanes["lead:7"][2]
    assert upd.kind == K.CRM_UPDATE_CONTACT and upd.inputs["contact_id"] == "c-benjamin" and upd.status == S.READY
    assert upd.postcondition.expect["emails"] == ["ben.ortiz@gmail.com"]
    create9 = lanes["lead:9"][2]
    assert create9.kind == K.CRM_CREATE_CONTACT and create9.inputs["account_id"] == "a-health"
    assert create9.inputs["owner"] == "a.chen"  # the linked account's owner
    assert [s.kind for s in lanes["lead:11"]] == [K.REVIEW_AMBIGUITY]  # skipped: nothing more
    outcome = {x["lane"]: x for x in lane_outcomes(await ledger.list_steps(r, keys, run.id),
                                                    await ledger.get_facts(r, keys, run.id))}
    assert outcome["lead:11"]["status"] == "skipped" and outcome["lead:7"]["status"] == "in_progress"


def test_parse_decision_forms():
    assert parse_decision("skip") == {"decision": "skip"}
    assert parse_decision({"decision": "match_existing:c1"})["value"] == "c1"
    assert parse_decision({"decision": "link_account", "value": "link_account:a1"})["value"] == "a1"
    assert parse_decision(None) is None and parse_decision({}) is None


# ---------------------------------------------------------------------------
# replanning (B5)
# ---------------------------------------------------------------------------


async def test_replan_moves_the_lane_to_api_after_n_rejections(r, keys):
    o, run, steps = await fanned_out(r, keys, {"crm_write_path": "auto", "replan_after_rejections": 2})
    cfg = await ledger.get_run_config(r, keys, run.id)
    await _commit_searches(r, keys, steps, {"lead:1": LOOKUPS["lead:1"]})
    await o.reconcile(run.id)
    create, task = group_lanes(await ledger.list_steps(r, keys, run.id))["lead:1"][1:]
    assert create.skill == Skill.BROWSER_ESPOCRM
    st = await reject_step(r, keys, create.id, "no CRM contact has email priya@northwind.com", cfg=cfg)
    assert st.status == S.READY  # first rejection: plain retry
    await o.reconcile(run.id)
    assert (await ledger.get_step(r, keys, create.id)).status == S.READY
    st = await reject_step(r, keys, create.id, "still nothing in the CRM", cfg=cfg)
    assert st.status == S.REJECTED  # policy holds it for the orchestrator
    await o.reconcile(run.id)
    lane = group_lanes(await ledger.list_steps(r, keys, run.id))["lead:1"]
    old = {s.id: s for s in lane}
    assert old[create.id].status == S.REPLANNED and old[task.id].status == S.REPLANNED
    new_create, new_task = [s for s in lane if s.status != S.REPLANNED and s.kind != K.CRM_SEARCH_CONTACT]
    assert new_create.skill == Skill.API_ESPOCRM and new_create.status == S.READY and new_create.attempt == 0
    assert new_task.skill == Skill.API_ESPOCRM and new_task.depends_on == [new_create.id]
    assert new_create.inputs == create.inputs and new_create.postcondition == create.postcondition
    rev = [e for e in await events(r, keys, run.id, EventType.PLAN_REVISED) if e.payload.get("replaces")][0]
    assert rev.payload["replaces"] == {create.id: new_create.id, task.id: new_task.id}
    assert rev.payload["to_skill"] == "api.espocrm" and "still nothing" in rev.payload["last_rejection"]
    replanned = [e for e in await events(r, keys, run.id, EventType.STEP_REPLANNED)]
    assert {e.step_id for e in replanned} == {create.id, task.id}


async def test_replan_from_ready_when_the_verifier_used_the_default_policy(r, keys):
    o, run, steps = await fanned_out(r, keys, {"crm_write_path": "auto", "replan_after_rejections": 2})
    await _commit_searches(r, keys, steps, {"lead:1": LOOKUPS["lead:1"]})
    await o.reconcile(run.id)
    create = group_lanes(await ledger.list_steps(r, keys, run.id))["lead:1"][1]
    for _ in range(2):  # default policy: rejected -> ready each time
        fence = await leases.acquire(r, keys, create.id, "w", 15_000)
        await ledger.transition(r, keys, create.id, S.LEASED, actor="w", actor_role="worker", fence=fence)
        await ledger.claim(r, keys, create.id, Claim(worker="w", fence=fence, summary="x"))
        await leases.release(r, keys, create.id, "w")
        await ledger.reject(r, keys, create.id, Verdict(ok=False, check="crm.contact_exists", reason="nope"))
    await o.reconcile(run.id)
    assert (await ledger.get_step(r, keys, create.id)).status == S.REPLANNED


async def test_no_replan_without_an_alternative_skill(r, keys):
    o, run, steps = await fanned_out(r, keys, {"crm_write_path": "browser", "replan_after_rejections": 1,
                                               "max_attempts": 2})
    cfg = await ledger.get_run_config(r, keys, run.id)
    await _commit_searches(r, keys, steps, {"lead:1": LOOKUPS["lead:1"]})
    await o.reconcile(run.id)
    create, task = group_lanes(await ledger.list_steps(r, keys, run.id))["lead:1"][1:]
    assert (await reject_step(r, keys, create.id, cfg=cfg)).status == S.READY  # retried, not held
    await o.reconcile(run.id)
    assert (await ledger.get_step(r, keys, create.id)).status == S.READY
    assert (await reject_step(r, keys, create.id, cfg=cfg)).status == S.DEAD  # max_attempts
    await o.reconcile(run.id)
    assert (await ledger.get_step(r, keys, task.id)).status == S.DEAD  # dependent fails with the reason
    steps = await ledger.list_steps(r, keys, run.id)
    lane1 = next(x for x in lane_outcomes(steps, await ledger.get_facts(r, keys, run.id)) if x["lane"] == "lead:1")
    # Track N: no other skill, so the lane goes to a human instead of failing the run
    handoff = [s for s in steps if s.lane == "lead:1" and s.kind == K.HUMAN_DECIDE]
    assert len(handoff) == 1 and handoff[0].inputs["dead_step"] == create.id and handoff[0].skill == Skill.REVIEW
    assert lane1["status"] == "waiting" and "CRM shows nothing" in lane1["reason"]


async def test_dead_step_is_replanned_on_auto(r, keys):
    o, run, steps = await fanned_out(r, keys, {"crm_write_path": "auto", "replan_after_rejections": 0})
    await _commit_searches(r, keys, steps, {"lead:1": LOOKUPS["lead:1"]})
    await o.reconcile(run.id)
    create = group_lanes(await ledger.list_steps(r, keys, run.id))["lead:1"][1]
    await ledger.transition(r, keys, create.id, S.DEAD, actor="reaper", actor_role="reaper", reason="lease expired 3 times")
    await o.reconcile(run.id)
    lane = group_lanes(await ledger.list_steps(r, keys, run.id))["lead:1"]
    assert [(s.kind, s.skill, s.status) for s in lane[3:]] == [
        (K.CRM_CREATE_CONTACT, Skill.API_ESPOCRM, S.READY), (K.CRM_CREATE_TASK, Skill.API_ESPOCRM, S.PLANNED)]


async def test_reject_policy():
    from ledger_core.protocol import Postcondition, Step

    st = Step(run_id="r", kind=K.CRM_CREATE_CONTACT, skill=Skill.BROWSER_ESPOCRM,
              postcondition=Postcondition(check="crm.contact_exists"))
    v = Verdict(ok=False, check="crm.contact_exists", reason="x")
    auto = RunConfig(crm_write_path="auto", replan_after_rejections=1)
    assert await reject_policy(st, v, auto) is None
    assert await reject_policy(st, v, RunConfig(crm_write_path="browser", replan_after_rejections=1)) == S.READY
    assert await reject_policy(st, v, RunConfig(crm_write_path="auto", replan_after_rejections=0)) == S.READY
    st.max_attempts = 1
    assert await reject_policy(st, v, RunConfig(crm_write_path="browser")) == S.DEAD


# ---------------------------------------------------------------------------
# finish (D5) and the criteria sweep
# ---------------------------------------------------------------------------


async def _run_with(r, keys, tmp_path, csv: str, criteria: list[dict]):
    f = tmp_path / "leads.csv"
    f.write_text(csv)
    o = orch(r, keys)
    run = await submit(r, keys, {"crm_write_path": "api"}, input_file=str(f))
    plan = {**PLAN, "steps": [{**PLAN["steps"][0], "inputs": {"file": str(f)},
                               "postcondition": {"check": "file.parsed_rows", "args": {"file": str(f)}}}]}
    with stub(understanding={"response": {**UNDERSTANDING, "criteria": criteria}}, plan={"response": plan}).installed():
        run = await o.reconcile(run.id)
    await parse_for_real(r, keys)
    return o, await o.reconcile(run.id)


HEADER = "Full Name,Email,Company,Phone,Country\n"


async def test_finish_completed_when_every_criterion_is_verified(r, keys, tmp_path):
    o, run = await _run_with(r, keys, tmp_path, HEADER, [
        {"id": "c1", "text": "File parsed", "check": "file.parsed_rows"},
        {"id": "c2", "text": "Rows without email skipped", "check": "review.decided"}])
    assert run.status == RunStatus.COMPLETED
    assert [c.status for c in run.criteria] == ["verified", "verified"]
    done = (await events(r, keys, run.id, EventType.RUN_COMPLETED))[0]
    assert done.payload["criteria"][0]["status"] == "verified"


async def test_finish_pending_input_while_a_review_waits(r, keys, tmp_path):
    o, run = await _run_with(r, keys, tmp_path, HEADER + "Jo Park,,,+1 415 555 0182,US\n", [
        {"id": "c1", "text": "File parsed", "check": "file.parsed_rows"},
        {"id": "c2", "text": "Rows without email skipped", "check": "review.decided"}])
    assert run.status == RunStatus.COMPLETED_PENDING_INPUT
    assert {c.id: c.status for c in run.criteria} == {"c1": "verified", "c2": "pending"}
    ev = (await events(r, keys, run.id, EventType.RUN_COMPLETED_PENDING_INPUT))[0]
    assert ev.payload["lanes"] == {"waiting": 1} and len(ev.payload["waiting_steps"]) == 1
    # the answer arrives: decision fact + committed review -> swept again -> completed
    review = next(s for s in await ledger.list_steps(r, keys, run.id) if s.kind == K.REVIEW_AMBIGUITY)
    await ledger.commit_fact(r, keys, run.id, "review:lead:1", {"decision": "skip"}, source_step=review.id, actor="human")
    await finish_step(r, keys, review.id, {"decision": "skip"}, worker="human")
    run = await o.reconcile(run.id)
    assert run.status == RunStatus.COMPLETED
    assert [c.status for c in run.criteria] == ["verified", "verified"]


async def test_finish_waits_while_a_live_reviewer_serves_the_queue(r, keys, tmp_path):
    from ledger_core import agents

    await agents.register_agent(r, keys, worker_card("meta-reviewer", "Meta", {"review": ["review.ambiguity"]}))
    await agents.set_alive(r, keys, "meta-reviewer")
    o, run = await _run_with(r, keys, tmp_path, HEADER + "Jo Park,,,+1 415 555 0182,US\n", [
        {"id": "c1", "text": "File parsed", "check": "file.parsed_rows"}])
    assert run.status == RunStatus.RUNNING  # the review is moving, not waiting


async def test_dead_lane_step_waits_for_a_human_instead_of_failing_the_run(r, keys, tmp_path):
    o, run = await _run_with(r, keys, tmp_path, HEADER + "Ann Lee,ann@x.test,Xco,,US\n", [
        {"id": "c1", "text": "File parsed", "check": "file.parsed_rows"},
        {"id": "c2", "text": "Contacts exist", "check": "crm.contact_exists"}])
    search = next(s for s in await ledger.list_steps(r, keys, run.id) if s.kind == K.CRM_SEARCH_CONTACT)
    await ledger.transition(r, keys, search.id, S.DEAD, actor="verifier", actor_role="orchestrator",
                            reason="CRM unreachable")
    run = await o.reconcile(run.id)
    assert run.status == RunStatus.COMPLETED_PENDING_INPUT  # Track N (was: failed)
    assert {c.id: c.status for c in run.criteria} == {"c1": "verified", "c2": "pending"}
    assert "waiting on lead:1" in run.criteria[1].evidence


async def test_a_failed_lane_still_fails_its_criterion():
    """Without a handoff (e.g. a lane outcome built elsewhere) a failed lane is a failed criterion."""
    crit = run_checks.Criterion(id="c2", text="Contacts exist", check="crm.contact_exists")
    lanes = [{"lane": "lead:1", "status": "failed", "reason": "crm.search_contact dead", "email": "a@x.test"}]
    [res] = await run_checks.evaluate_criteria([crit], lanes, CheckContext(run_id="run_x", facts={}))
    assert res.status == run_checks.FAILED and "lane failed" in res.evidence


async def test_finish_failed_when_parse_dies(r, keys):
    o, run = await planned_run(r, keys)
    parse = (await ledger.list_steps(r, keys, run.id))[0]
    await ledger.transition(r, keys, parse.id, S.DEAD, actor="reaper", actor_role="reaper", reason="x")
    run = await o.reconcile(run.id)
    assert run.status == RunStatus.FAILED
    assert "parse step is dead" in (await events(r, keys, run.id, EventType.RUN_FAILED))[0].payload["reason"]


async def test_sweep_statuses_per_lane():
    lanes = [
        {"lane": "lead:1", "status": "done", "kinds": {"crm.search_contact": "committed"}, "email": "a@x"},
        {"lane": "lead:2", "status": "waiting", "kinds": {"crm.search_contact": "committed"},
         "review": {"decision": None}},
        {"lane": "lead:3", "status": "skipped", "kinds": {}, "review": {"decision": {"decision": "skip"}}},
    ]
    ctx = CheckContext(run_id="r", crm=None)
    res = await run_checks.evaluate_criteria([
        {"id": "a", "text": "t", "check": "crm.lookup_matches"},
        {"id": "b", "text": "t", "check": "review.decided"},
        {"id": "c", "text": "t", "check": "email.sent"},
        {"id": "d", "text": "t", "check": "crm.contact_exists"},
        {"id": "e", "text": "t", "check": "crm.task_exists", "status": "waived", "evidence": "by a human"},
    ], lanes, ctx)
    got = {x.id: x.status for x in res}
    assert got == {"a": "verified", "b": "pending", "c": "pending", "d": "failed", "e": "waived"}
    assert "no CRM reader" in res[3].evidence  # never a vacuous pass without the read channel
    assert not run_checks.all_met(res)
    from ledger_core.postconditions import run_check

    out = await run_check("run.criteria_met", {"criteria": [{"id": "a", "text": "t", "check": "crm.lookup_matches"}],
                                               "lanes": lanes[:1]}, {}, ctx)
    assert out.ok and out.observed["criteria"][0]["status"] == "verified"
