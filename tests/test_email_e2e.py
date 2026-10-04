"""Track K acceptance: the one-line goal end to end with email lanes, against the
seeded EspoCRM and Mailpit (LLM_BACKEND=scripted fixtures: orchestrator,
drafter, judge). Needs `make up && make seed` (redis, espocrm, mailpit).

In process, on a namespaced ledger: the parser, api.espocrm, drafter and mailer
workers, the verifier (CRM reader + Mailpit + the LLM judge for drafts) and the
orchestrator. Track J's meta-reviewer is not part of this track, so the test
plays it where the run needs a decision:
  - row 7 (Ben Ortiz, probable match): match_existing -> update + task, then the
    open-deal rule keeps him from getting an email;
  - the approval batch: ONE judge.judge_drafts() call for all drafts, the
    playbook policy (approval.policy_decisions), approval facts committed.

Checks: 8 drafts verified (rows 1,2,3,4,5,8,10,12), one approval step, nothing
sent before the approval fact, then exactly 8 emails in Mailpit, none for Ben.
Leaves the 8 messages in Mailpit (evidence); CRM changes are scrubbed.
"""

from __future__ import annotations

import asyncio

import pytest

from ledger_core import approval, judge, ledger, llm
from ledger_core.config import RunConfig
from ledger_core.llm_scripted import ScriptedBackend
from ledger_core.mailpit import MailpitClient
from ledger_core.orchestrator import Orchestrator, submit_goal
from ledger_core.orchestrator_email import exclusions
from ledger_core.orchestrator_lanes import lane_outcomes
from ledger_core.orchestrator_local import (
    api_worker,
    crm_context_factory,
    drafter_worker,
    mailer_worker,
    parser_worker,
)
from ledger_core.orchestrator_replan import reject_policy
from ledger_core.protocol import RunStatus, StepKind, StepStatus
from ledger_core.verifier import Verifier

from crm_helpers import admin_client, crm_secrets  # noqa: F401 - fixture
from test_orchestrator import finish_step
from test_orchestrator_crm import _scrub_crm

pytestmark = pytest.mark.crm

S, K = StepStatus, StepKind
GOAL = "Add the leads from yesterday's event to the CRM and set up follow-ups."
RECIPIENTS = {"lead:1": "priya@northwind.com", "lead:2": "marcus.lee@acme.com", "lead:3": "lena@kestrel-labs.io",
              "lead:4": "dana@helixbio.com", "lead:5": "tom.becker@orbitalfreight.com",
              "lead:8": "hannah@fieldstone.dev", "lead:10": "omar@brightline.co", "lead:12": "grace.wu@tallgrass.com"}
FIRST = {"lead:1": "Priya", "lead:2": "Marcus", "lead:3": "Lena", "lead:4": "Dana", "lead:5": "Tom",
         "lead:8": "Hannah", "lead:10": "Omar", "lead:12": "Grace"}
BEN = ("ben.ortiz@gmail.com", "benjamin@quarrydata.com")
SUBJECT = "Follow up: Signal Summit"


class CountingScripted(ScriptedBackend):
    def __init__(self, *a, **kw) -> None:
        super().__init__(*a, emit_events=False, **kw)
        self.calls: list[tuple[str, str, str | None]] = []

    async def complete(self, role, messages, schema, **kw):
        self.calls.append((role, schema.__name__, kw.get("step_id")))
        return await super().complete(role, messages, schema, **kw)


async def _benjamin(a) -> dict | None:
    params = {"where[0][type]": "equals", "where[0][attribute]": "emailAddress", "where[0][value]": BEN[1]}
    hits = (await a.get("/Contact", params=params)).json().get("list", [])
    return (await a.get(f"/Contact/{hits[0]['id']}")).json() if hits else None


@pytest.fixture
async def clean_world(crm_secrets):
    """Scrub demo records (Track G's helper) and Mailpit; snapshot the seeded
    Benjamin Ortiz so the review decision's secondary email can be undone."""
    await _scrub_crm()
    async with admin_client() as a:
        ben = await _benjamin(a)
    assert ben is not None, "seed contact benjamin@quarrydata.com missing (make seed)"
    async with MailpitClient() as mp:
        for to in [*RECIPIENTS.values(), *BEN]:
            await mp.delete_to(to)
    yield ben
    async with admin_client() as a:
        await a.put(f"/Contact/{ben['id']}", json={"emailAddressData": ben.get("emailAddressData"),
                                                   "phoneNumberData": ben.get("phoneNumberData")})
        params = {"where[0][type]": "equals", "where[0][attribute]": "parentId", "where[0][value]": ben["id"]}
        for t in (await a.get("/Task", params=params)).json().get("list", []):
            if t.get("name") == SUBJECT:
                await a.delete(f"/Task/{t['id']}")
    await _scrub_crm()


@pytest.fixture
async def scripted(repo):
    prev = llm.get_backend()
    backend = CountingScripted(f"{repo}/tests/fixtures/llm")
    llm.set_backend(backend)
    yield backend
    llm.set_backend(prev)


async def test_email_lanes_end_to_end(r, keys, repo, clean_world, scripted):
    data, pbs = f"{repo}/data", f"{repo}/playbooks"
    verifier = Verifier(r, keys, agent_id="verifier-k", context_factory=crm_context_factory(data),
                        reject_policy=reject_policy, judge=judge.make_judge(), block_ms=300)
    agents = [parser_worker(r, keys, data_dir=data, block_ms=300), api_worker(r, keys, block_ms=300), verifier,
              drafter_worker(r, keys, block_ms=300, playbook_dir=pbs), mailer_worker(r, keys, block_ms=300)]
    orch = Orchestrator(r, keys, playbook_dir=pbs, run_reaper=False)
    tasks = [asyncio.create_task(a.run()) for a in agents]
    mp = MailpitClient()
    try:
        run = await submit_goal(r, keys, GOAL, input_file="event_attendees.csv", config={"crm_write_path": "api"},
                                playbook_dir=pbs, actor="test")
        run = await orch.run_until_settled(run.id, timeout=240)

        # -- 1. drafts verified, one approval step for all of them, nothing sent ---------
        steps = await ledger.list_steps(r, keys, run.id)
        drafts = {s.lane: s for s in steps if s.kind == K.EMAIL_DRAFT}
        assert sorted(drafts) == sorted(RECIPIENTS)
        for lane, d in drafts.items():
            assert d.status == S.COMMITTED, (lane, d.verdict)
            assert d.claim.data["to"] == RECIPIENTS[lane]
            assert d.claim.data["subject"] == f"Good to meet you at Signal Summit, {FIRST[lane]}"
            assert d.verdict.observed["judge"]["score"] >= 0.9  # deterministic check, then the judge
        appr = [s for s in steps if s.kind == K.REVIEW_APPROVAL]
        assert len(appr) == 1 and appr[0].skill.value == "review" and appr[0].status == S.READY
        appr = appr[0]
        assert sorted(it["lane"] for it in appr.inputs["items"]) == sorted(RECIPIENTS)
        assert not [s for s in steps if s.kind == K.EMAIL_SEND]
        for to in RECIPIENTS.values():
            assert await mp.messages_to(to) == []  # no approval fact yet -> nothing sent
        assert run.status == RunStatus.COMPLETED_PENDING_INPUT
        facts = await ledger.get_facts(r, keys, run.id)
        assert all(f"{lane}.draft" in facts for lane in RECIPIENTS)
        assert not [k for k in facts if k.startswith("approval:")]

        # -- 2. (simulated meta-reviewer) Ben Ortiz is the seeded Benjamin -> update + task
        review7 = next(s for s in steps if s.kind == K.REVIEW_AMBIGUITY and s.lane == "lead:7")
        match = next(o["value"] for o in review7.inputs["options"] if o["value"].startswith("match_existing:"))
        decision = {"decision": "match_existing", "value": match.split(":", 1)[1], "decided_by": "test"}
        await ledger.commit_fact(r, keys, run.id, "review:lead:7", decision, source_step=review7.id, actor="test")
        await finish_step(r, keys, review7.id, decision)
        run = await orch.run_until_settled(run.id, timeout=120)
        steps = await ledger.list_steps(r, keys, run.id)
        ben = [s for s in steps if s.lane == "lead:7"]
        kinds = {s.kind: s.status for s in ben}
        assert kinds[K.CRM_UPDATE_CONTACT] == S.COMMITTED and kinds[K.CRM_CREATE_TASK] == S.COMMITTED
        assert K.EMAIL_DRAFT not in kinds and K.EMAIL_SEND not in kinds  # open deal: task, no email
        assert exclusions(run.id) == {"lead:7": "open_deal"}
        assert len([s for s in steps if s.kind == K.REVIEW_APPROVAL]) == 1  # still one batch

        # -- 3. (simulated meta-reviewer) approve the batch: ONE judge call for all drafts --
        inputs = await ledger.resolve_inputs(r, keys, appr)  # the drafts arrive as committed facts
        batch_drafts = {it["lane"]: it["draft"] for it in inputs["items"]}
        before = len(scripted.calls)
        verdicts, model = await judge.judge_drafts(
            [{"id": lane, **{k: d[k] for k in ("to", "subject", "body")}} for lane, d in batch_drafts.items()],
            run_id=run.id, step_id=appr.id)
        assert [c[:2] for c in scripted.calls[before:]] == [("verifier", "BatchJudgeVerdict")]
        items, escalate = approval.policy_decisions(batch_drafts, verdicts, RunConfig(), model=model)
        assert escalate == []
        batch = await approval.commit_decisions(r, keys, run.id, appr.id, items, decided_by="meta-reviewer (test)",
                                                actor="test", model=model, threshold=0.9)
        assert batch["decision"] == "approve" and batch["approved"] == sorted(RECIPIENTS)
        await finish_step(r, keys, appr.id, batch, worker="meta-reviewer-test")  # claim + verifier commit

        # -- 4. sends: exactly one email per approved draft, none for Ben ----------------
        run = await orch.run_until_settled(run.id, timeout=120)
        steps = await ledger.list_steps(r, keys, run.id)
        sends = {s.lane: s for s in steps if s.kind == K.EMAIL_SEND}
        assert sorted(sends) == sorted(RECIPIENTS)
        for lane, s in sends.items():
            assert s.status == S.COMMITTED, (lane, s.verdict)
            assert appr.id in s.depends_on and drafts[lane].id in s.depends_on
        for lane, to in RECIPIENTS.items():
            msgs = await mp.messages_to(to)
            assert len(msgs) == 1, (to, msgs)
            assert msgs[0]["Subject"] == f"Good to meet you at Signal Summit, {FIRST[lane]}"
        for to in BEN:
            assert await mp.messages_to(to) == []
        lanes = {x["lane"]: x for x in lane_outcomes(steps, await ledger.get_facts(r, keys, run.id))}
        assert lanes["lead:7"]["status"] == "done" and lanes["lead:7"]["task"]["status"] == "committed"
        crit = {c.check: c for c in run.criteria}
        sent = crit["email.sent"]
        assert sent.status == "pending" and sent.evidence.startswith("8 sent with approval; waiting on lead:9")
        assert run.status == RunStatus.COMPLETED_PENDING_INPUT  # rows 9 (Sam Ito) and 11 (Jo Park) await review
        print("\nMAILPIT:", {to: [m["Subject"] for m in await mp.messages_to(to)] for to in RECIPIENTS.values()})
    finally:
        for a in agents:
            a.stop()
        await asyncio.gather(*tasks, return_exceptions=True)
        await mp.aclose()
        if orch._crm is not None:
            await orch._crm.aclose()
