"""Track K: drafter worker (email.draft) with the real verifier (email.draft_valid
+ LLM judge), real Redis and a StubLLM. No CRM, no network.

- a draft is built from committed facts only; the recipient is the lead's
  committed email, never the model's choice
- a draft-validity rejection (and a judge rejection) feeds the next attempt:
  the second prompt carries the verifier's reason, and the fixed draft commits
- the committed draft becomes the fact lead:<n>.draft (what approval and the
  mailer work from)
- the scripted fixtures produce the 8 demo drafts and every one passes the
  deterministic check
"""

from __future__ import annotations

import json
from pathlib import Path

from ledger_core import ledger, llm, postconditions
from ledger_core.checks.email import template_subject
from ledger_core.judge import JudgeVerdict, make_judge
from ledger_core.llm_scripted import ScriptedBackend
from ledger_core.llm_testing import StubLLM
from ledger_core.postconditions import CheckContext, run_check
from ledger_core.protocol import Postcondition, Run, Skill, Step, StepKind, StepStatus
from ledger_core.services.worker_drafter import build_worker
from ledger_core.verifier import Verifier
from ledger_core.workers.drafter import EmailDraft

REPO = Path("/repo")
PB_DIR = str(REPO / "playbooks")
EVENT = "Signal Summit"
LEAD = {"row": 1, "name": "Priya Raman", "first_name": "Priya", "last_name": "Raman", "email": "priya@northwind.com",
        "company": "Northwind", "title": "VP Sales", "notes": "Asked about Q1 pilot", "country": "United States"}
GOOD = {"subject": template_subject(EVENT, "Priya"),
        "body": "Hi Priya,\n\nThanks for stopping by at Signal Summit. You asked about a Q1 pilot and I would be glad "
                "to walk you through it. Would you be open to a 20-minute call?\n\nBest regards,\nAlex Chen"}


async def setup_draft(r, keys, lead=LEAD, lane="lead:1", owner_name="Alex Chen") -> tuple[Run, Step]:
    run = await ledger.create_run(r, keys, goal="test drafts", actor="test")
    await ledger.commit_fact(r, keys, run.id, lane, lead, source_step="stp_parse", actor="verifier")
    step = Step(
        run_id=run.id, kind=StepKind.EMAIL_DRAFT, skill=Skill.EMAIL_DRAFT, lane=lane, status=StepStatus.READY,
        title="Draft", inputs={"lead": f"fact:{lane}", "lane": lane, "to": lead["email"],
                               "first_name": lead["first_name"], "event_name": EVENT, "owner": "a.chen",
                               "owner_name": owner_name, "from_email": "a.chen@ledger-demo.test"},
        postcondition=Postcondition(check="email.draft_valid",
                                    args={"recipient": lead["email"], "first_name": lead["first_name"],
                                          "event_name": EVENT, "owner_name": owner_name, "max_words": 120},
                                    expect={"subject": template_subject(EVENT, lead["first_name"])}),
    )
    await ledger.create_steps(r, keys, [step], actor="test")
    return run, step


async def drive(worker, verifier, rounds: int = 4) -> None:
    for _ in range(rounds):
        n = await worker.run_until_idle()
        m = await verifier.run_until_idle()
        if not n and not m:
            break


async def test_draft_from_facts_recipient_fixed_and_fact_committed(r, keys):
    run, step = await setup_draft(r, keys)
    stub = StubLLM()
    # the model tries to pick another recipient: the schema has no "to" field, so it cannot
    stub.on("worker", EmailDraft, response=GOOD)
    worker = build_worker(r, keys, "drafter-t", block_ms=200, playbook_dir=PB_DIR)
    verifier = Verifier(r, keys, agent_id="verifier-t", block_ms=200)
    with stub.installed():
        await drive(worker, verifier)
    st = await ledger.get_step(r, keys, step.id)
    assert st.status == StepStatus.COMMITTED, st.verdict
    assert st.claim.data["to"] == "priya@northwind.com"
    call = stub.calls[0]
    assert call.role == "worker"
    assert "Good to meet you at Signal Summit, Priya" in call.text  # template from the inputs
    assert "Follow-up policy" in call.text or "under 120 words" in call.text  # playbook layer
    assert "[fixture_key: lead:1]" in call.text
    assert "run_" not in call.text and "stp_" not in call.text  # no volatile ids in prompts
    draft = (await ledger.get_facts(r, keys, run.id))["lead:1.draft"]
    assert draft["draft_step"] == step.id and draft["to"] == "priya@northwind.com"
    assert draft["subject"] == GOOD["subject"] and draft["owner_name"] == "Alex Chen"


async def test_draft_validity_rejection_feeds_next_attempt(r, keys):
    run, step = await setup_draft(r, keys)
    bad = {"subject": "Following up", "body": "Hi [Name],\n\nGreat to meet you at Signal Summit.\n\nAlex Chen"}
    stub = StubLLM()
    stub.on("worker", EmailDraft, responses=[bad, GOOD])
    worker = build_worker(r, keys, "drafter-t", block_ms=200, playbook_dir=PB_DIR)
    verifier = Verifier(r, keys, agent_id="verifier-t", block_ms=200)
    with stub.installed():
        await drive(worker, verifier)
    st = await ledger.get_step(r, keys, step.id)
    assert st.status == StepStatus.COMMITTED
    assert [a.outcome for a in st.history] == ["rejected", "committed"]
    reason = st.history[0].verdict.reason
    assert "placeholder" in reason and "subject" in reason
    calls = stub.calls_for("worker", EmailDraft)
    assert len(calls) == 2
    marker = "placeholder text ['[Name]']"
    assert marker in reason
    assert marker in calls[1].text and marker not in calls[0].text  # D3: the reason is in the next prompt
    facts = await ledger.get_facts(r, keys, run.id)
    assert facts["lead:1.draft"]["body"] == GOOD["body"]


async def test_judge_rejection_feeds_next_attempt(r, keys):
    _run, step = await setup_draft(r, keys)
    pushy = {**GOOD, "body": GOOD["body"].replace("Would you be open", "You really must book")}
    stub = StubLLM()
    stub.on("worker", EmailDraft, responses=[pushy, GOOD])
    stub.on("verifier", JudgeVerdict, responses=[
        {"score": 0.35, "flags": ["tone"], "reasoning": "Pushy call to action; the playbook asks for plain, warm."},
        {"score": 0.95, "flags": [], "reasoning": "Warm, specific, signed."}])
    worker = build_worker(r, keys, "drafter-t", block_ms=200, playbook_dir=PB_DIR)
    verifier = Verifier(r, keys, agent_id="verifier-t", block_ms=200, judge=make_judge())
    with stub.installed():
        await drive(worker, verifier)
    st = await ledger.get_step(r, keys, step.id)
    assert st.status == StepStatus.COMMITTED
    assert "Pushy call to action" in st.history[0].verdict.reason
    assert "Pushy call to action" in stub.calls_for("worker", EmailDraft)[1].text
    assert st.verdict.observed["judge"]["score"] == 0.95


async def test_scripted_fixtures_give_the_eight_demo_drafts():
    """Every demo draft in tests/fixtures/llm/drafter.json passes email.draft_valid."""
    postconditions.load_all()
    owners = {1: "Alex Chen", 2: "Rita Silva", 3: "Rita Silva", 4: "Alex Chen", 5: "Rita Silva", 8: "Alex Chen",
              10: "Alex Chen", 12: "Rita Silva"}
    firsts = {1: "Priya", 2: "Marcus", 3: "Lena", 4: "Dana", 5: "Tom", 8: "Hannah", 10: "Omar", 12: "Grace"}
    fx = json.loads((REPO / "tests/fixtures/llm/drafter.json").read_text())
    assert sorted(int(f["fixture_key"].split(":")[1]) for f in fx) == sorted(owners)
    backend = ScriptedBackend(REPO / "tests/fixtures/llm", emit_events=False)
    for row, first in firsts.items():
        msgs = [{"role": "system", "content": f"x\n\n[fixture_key: lead:{row}]"}, {"role": "user", "content": "y"}]
        d = await backend.complete("worker", msgs, EmailDraft)
        res = await run_check("email.draft_valid",
                              {"draft": {"to": f"x{row}@demo.test", **d.model_dump()}, "recipient": f"x{row}@demo.test",
                               "first_name": first, "event_name": EVENT, "owner_name": owners[row]},
                              {"subject": f"Good to meet you at {EVENT}, {first}"}, CheckContext(run_id="r"))
        assert res.ok, (row, res.reason)
    # Ben Ortiz (row 7, open deal) has no draft fixture: drafting him would be a scripted miss
    miss = [{"role": "system", "content": "[fixture_key: lead:7]"}, {"role": "user", "content": "y"}]
    try:
        await backend.complete("worker", miss, EmailDraft)
        raise AssertionError("lead:7 must not have a draft fixture")
    except llm.LLMError:
        pass
