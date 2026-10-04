"""Track K: mailer worker (email.send) + approval gate. Real Redis, real Mailpit
(SMTP + API, as in test_email_checks), no CRM, no LLM.

- nothing is ever sent without a committed approval fact (asserted on every
  send the mailer makes), nor for a rejected or a different (re-drafted) draft
- the orchestrator's send stage creates email.send steps only for approved lanes
- a takeover after the first holder sent but died before claiming does not
  send a second copy (check-then-act on the Message-ID through Mailpit)
- RunConfig.dry_run sends nothing, and email.sent verifies exactly that
"""

from __future__ import annotations

from typing import Any

import pytest

from ledger_core import approval, leases, ledger
from ledger_core.config import RunConfig
from ledger_core.mailpit import MailpitClient
from ledger_core.orchestrator_email import _send_stage, approval_step
from ledger_core.protocol import Claim, Postcondition, Run, Skill, Step, StepKind, StepStatus, Verdict
from ledger_core.services.worker_mailer import build_worker
from ledger_core.verifier import Verifier
from ledger_core.worker_base import WorkContext
from ledger_core.workers import mailer

S = StepStatus
SUBJECT = "Good to meet you at Signal Summit, Priya"


@pytest.fixture
async def mp():
    c = MailpitClient()
    yield c
    await c.aclose()


def draft_fact(to: str, draft_step: str = "stp_draft1") -> dict[str, Any]:
    return {"lane": "lead:1", "to": to, "subject": SUBJECT, "owner": "a.chen", "owner_name": "Alex Chen",
            "from_email": "a.chen@ledger-demo.test", "draft_step": draft_step,
            "body": "Hi Priya,\n\nGood to meet you at Signal Summit. Would a 20-minute call suit you?\n\nAlex Chen"}


async def setup_send(r, keys, to: str, *, approve: Any = None, config: RunConfig | None = None) -> tuple[Run, Step]:
    run = await ledger.create_run(r, keys, goal="test sends", actor="test")
    if config is not None:
        await ledger.set_run_config(r, keys, run.id, config, actor="test")
    await ledger.commit_fact(r, keys, run.id, "lead:1.draft", draft_fact(to), source_step="stp_draft1",
                             actor="verifier")
    if approve is not None:
        await ledger.commit_fact(r, keys, run.id, "approval:lead:1", approve, source_step="stp_appr", actor="test")
    step = Step(run_id=run.id, kind=StepKind.EMAIL_SEND, skill=Skill.EMAIL_SEND, lane="lead:1", status=S.READY,
                side_effect=True, title="Send",
                inputs={"lane": "lead:1", "draft": "fact:lead:1.draft", "approval_step": "stp_appr",
                        "to": to, "subject": SUBJECT, "idempotency_key": f"email.send:{to}"},
                postcondition=Postcondition(check="email.sent", args={"to": to, "subject": SUBJECT},
                                            expect={"subject": SUBJECT, "exactly_once": True}))
    await ledger.create_steps(r, keys, [step], actor="test")
    return run, step


class Outbox:
    """Fake SMTP: records what would be sent, and the approval fact on record at that moment."""

    def __init__(self, r, keys, run_id: str) -> None:
        self.r, self.keys, self.run_id = r, keys, run_id
        self.sent: list[tuple[Any, Any]] = []

    async def __call__(self, msg) -> None:
        facts = await ledger.get_facts(self.r, self.keys, self.run_id)
        self.sent.append((msg, approval.approval_for(facts, msg["X-Ledger-Lane"])))


@pytest.mark.parametrize("approve, why", [
    (None, "no committed approval fact"),
    ({"decision": "reject", "decided_by": "human"}, "not approve"),
    ({"decision": "approve", "draft_step": "stp_older_draft"}, "differs from the approved one"),
    ({"decision": "approve", "to": "someone.else@example.test"}, "differs from the approved one"),
])
async def test_never_sends_without_a_committed_approval(r, keys, uniq, approve, why):
    to = f"priya@{uniq}.test"
    run, step = await setup_send(r, keys, to, approve=approve)
    outbox = Outbox(r, keys, run.id)
    worker = build_worker(r, keys, "mailer-t", block_ms=200, send=outbox)
    assert await worker.run_until_idle() == 1
    st = await ledger.get_step(r, keys, step.id)
    assert outbox.sent == []
    assert st.status == S.CLAIMED_DONE and st.claim.acted is False and st.claim.data["blocked"] is True
    assert why in st.claim.data["reason"]
    # and the verifier does not take the claim for a send: Mailpit has nothing
    v = Verifier(r, keys, agent_id="verifier-t", block_ms=200)
    await v.run_until_idle()
    st = await ledger.get_step(r, keys, step.id)
    assert st.history[-1].outcome == "rejected" and "Mailpit has no message" in st.history[-1].verdict.reason


async def test_sends_only_with_approval_on_record(r, keys, uniq, mp):
    to = f"priya@{uniq}.test"
    run, step = await setup_send(r, keys, to, approve={"decision": "approve", "draft_step": "stp_draft1",
                                                        "to": to, "subject": SUBJECT, "decided_by": "meta-reviewer"})
    outbox = Outbox(r, keys, run.id)
    real = []

    async def send(msg):
        await outbox(msg)
        real.append(msg)
        await mailer.smtp_send(msg)

    try:
        worker = build_worker(r, keys, "mailer-t", block_ms=200, send=send)
        await worker.run_until_idle()
        await Verifier(r, keys, agent_id="verifier-t", block_ms=200).run_until_idle()
        st = await ledger.get_step(r, keys, step.id)
        assert st.status == S.COMMITTED, st.verdict
        assert len(outbox.sent) == 1
        for _msg, rec in outbox.sent:  # every send had an approval on record when it went out
            assert approval.is_approved(rec)
        msg = outbox.sent[0][0]
        assert msg["From"] == "Alex Chen <a.chen@ledger-demo.test>" and msg["To"] == to
        facts = await ledger.get_facts(r, keys, run.id)
        assert facts["lead:1.email"]["to"] == to and facts["lead:1.email"]["action"] == "sent"
        assert len(await mp.messages_to(to)) == 1
    finally:
        await mp.delete_to(to)


async def test_takeover_does_not_double_send(r, keys, uniq, mp):
    to = f"priya@{uniq}.test"
    run, step = await setup_send(r, keys, to, approve={"decision": "approve", "decided_by": "meta-reviewer"})
    try:
        # holder A: leases, sends through real SMTP ... and dies before it can claim
        fence = await leases.acquire(r, keys, step.id, "mailer-a", 15_000)
        st = await ledger.transition(r, keys, step.id, S.LEASED, actor="mailer-a", actor_role="worker", fence=fence)
        ctx = WorkContext(r=r, keys=keys, agent_id="mailer-a", step=st, fence=fence,
                          inputs=await ledger.resolve_inputs(r, keys, st), facts=await ledger.get_facts(r, keys, run.id),
                          attempts=[], config=RunConfig())
        out = await mailer.make_handler()(st, ctx)
        assert out.acted and out.data["action"] == "sent"
        await leases.release(r, keys, step.id, "mailer-a")  # SIGKILL: the lease lapses ...
        await ledger.expire_lease(r, keys, step.id)  # ... and the reaper requeues the step
        assert (await ledger.get_step(r, keys, step.id)).status == S.READY

        # holder B takes over: check-then-act finds the Message-ID in Mailpit, sends nothing
        sends: list[Any] = []

        async def send(msg):
            sends.append(msg)
            await mailer.smtp_send(msg)

        await build_worker(r, keys, "mailer-b", block_ms=200, send=send).run_until_idle()
        await Verifier(r, keys, agent_id="verifier-t", block_ms=200).run_until_idle()
        st = await ledger.get_step(r, keys, step.id)
        assert sends == []
        assert st.status == S.COMMITTED, st.verdict
        assert st.claim.worker == "mailer-b" and st.claim.acted is False and st.claim.data["action"] == "exists"
        assert [a.outcome for a in st.history] == ["lease_expired", "committed"]
        assert len(await mp.messages_to(to)) == 1  # exactly one email, despite two holders
    finally:
        await mp.delete_to(to)


async def test_dry_run_sends_nothing_and_verifies_that(r, keys, uniq, mp):
    to = f"priya@{uniq}.test"
    run, step = await setup_send(r, keys, to, approve={"decision": "approve"}, config=RunConfig(dry_run=True))
    sends: list[Any] = []

    async def send(msg):
        sends.append(msg)

    await build_worker(r, keys, "mailer-t", block_ms=200, send=send).run_until_idle()
    await Verifier(r, keys, agent_id="verifier-t", block_ms=200).run_until_idle()
    st = await ledger.get_step(r, keys, step.id)
    assert sends == [] and st.claim.data["dry_run"] is True
    assert st.status == S.COMMITTED and st.verdict.observed["dry_run"] is True
    assert await mp.messages_to(to) == []


async def test_dry_run_claim_is_not_trusted_without_dry_run_config(r, keys, uniq):
    """A worker claiming dry_run on a live run is a false claim: rejected."""
    to = f"priya@{uniq}.test"
    run, step = await setup_send(r, keys, to, approve={"decision": "approve"})
    fence = await leases.acquire(r, keys, step.id, "liar", 15_000)
    await ledger.transition(r, keys, step.id, S.LEASED, actor="liar", actor_role="worker", fence=fence)
    await ledger.claim(r, keys, step.id, Claim(worker="liar", fence=fence, summary="dry run",
                                                data={"dry_run": True}, acted=False))
    await Verifier(r, keys, agent_id="verifier-t", block_ms=200).run_until_idle()
    st = await ledger.get_step(r, keys, step.id)
    assert st.history[-1].outcome == "rejected"


def _committed(step: Step, data: dict[str, Any]) -> Step:
    return step.model_copy(update={"status": S.COMMITTED, "claim": Claim(worker="w", fence=1, summary="", data=data),
                                   "verdict": Verdict(ok=True, check=step.postcondition.check, reason="ok")})


def test_send_steps_only_for_approved_lanes():
    run = Run(goal="g")
    cfg = RunConfig()
    drafts = []
    for row in (1, 2, 3):
        d = Step(run_id=run.id, kind=StepKind.EMAIL_DRAFT, skill=Skill.EMAIL_DRAFT, lane=f"lead:{row}",
                 postcondition=Postcondition(check="email.draft_valid"))
        drafts.append(_committed(d, {"to": f"p{row}@x.test", "subject": f"S{row}"}))
    appr = approval_step(run, drafts, cfg, "Signal Summit", 1, {"lead:7": "open_deal"})
    assert appr.kind == StepKind.REVIEW_APPROVAL and appr.skill == Skill.REVIEW and appr.lane is None
    assert [it["lane"] for it in appr.inputs["items"]] == ["lead:1", "lead:2", "lead:3"]
    assert appr.inputs["items"][0]["draft"] == "fact:lead:1.draft"
    assert sorted(appr.depends_on) == sorted(d.id for d in drafts)

    steps = [*drafts, appr]
    assert _send_stage(run, steps, {}, cfg) == []  # approval step exists, nothing decided: no sends
    appr_done = appr.model_copy(update={"status": S.COMMITTED})
    assert _send_stage(run, [*drafts, appr_done], {}, cfg) == []  # committed without a decision fact: no sends
    facts = {"approval:lead:1": {"decision": "approve", "draft_step": drafts[0].id},
             "approval:lead:2": {"decision": "reject"},
             "approval:lead:3": {"decision": "approve", "draft_step": "stp_someother"}}
    sends = _send_stage(run, [*drafts, appr_done], facts, cfg)
    assert [s.lane for s in sends] == ["lead:1"]
    s = sends[0]
    assert s.kind == StepKind.EMAIL_SEND and s.skill == Skill.EMAIL_SEND and s.side_effect
    assert set(s.depends_on) == {drafts[0].id, appr.id}
    assert s.postcondition.check == "email.sent" and s.postcondition.expect["exactly_once"] is True
    # batch fact form works too (meta-reviewer may commit only approval:emails)
    batch = approval.batch_record({"lead:2": {"decision": "approve"}, "lead:3": {"decision": "approve"}},
                                  decided_by="meta-reviewer")
    sends = _send_stage(run, [*drafts, appr_done], {"approval:emails": batch}, cfg)
    assert [s.lane for s in sends] == ["lead:1", "lead:2", "lead:3"][1:]


def test_policy_decisions_follow_the_playbook():
    cfg = RunConfig()
    drafts = {"lead:1": {"to": "a@x.test", "subject": "S", "draft_step": "d1"},
              "lead:2": {"to": "b@x.test", "subject": "S", "draft_step": "d2"},
              "lead:3": {"to": "c@x.test", "subject": "S", "draft_step": "d3"}}
    verdicts = {"lead:1": {"score": 0.95, "flags": []}, "lead:2": {"score": 0.85, "flags": []},
                "lead:3": {"score": 0.97, "flags": ["pricing"]}}
    items, escalate = approval.policy_decisions(drafts, verdicts, cfg, model="m")
    assert items["lead:1"]["decision"] == "approve"
    assert escalate == ["lead:2", "lead:3"]
    items, escalate = approval.policy_decisions(drafts, verdicts, RunConfig(always_ask_human_email=True))
    assert escalate == ["lead:1", "lead:2", "lead:3"]
