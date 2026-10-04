"""Track F / Phase 2 against real Redis + the seeded EspoCRM: the api.espocrm
worker (Track B handlers on worker_base) and the verifier with its read-only
CRM reader. "A claim is not a fact."""

from __future__ import annotations

import asyncio
import json
from pathlib import Path

import pytest

from ledger_core import faults, ledger
from ledger_core.cli import submit_spec
from ledger_core.events import read_events
from ledger_core.keys import Keys
from ledger_core.protocol import EventType, Postcondition, Skill, Step, StepKind
from ledger_core.services.worker_api_crm import build_worker, make_handler
from ledger_core.services.worker_parser import parser_card
from ledger_core.verifier import Verifier, make_context_factory
from ledger_core.worker_base import Worker
from ledger_core.workers.parser import make_handler as parser_handler

from crm_helpers import *  # noqa: F403
from crm_helpers import admin_client
from ledger_helpers import S, new_run, wait_for

pytestmark = pytest.mark.crm

SECRETS = Keys()  # the seed writes CRM keys to the default namespace


def lead(uniq: str, n: int = 1, **kw) -> dict:
    base = {"row": n, "name": f"Phase Two{n}", "first_name": "Phase", "last_name": f"Two{n}",
            "email": f"p2.{n}@{uniq}.test", "company": f"Co {uniq}", "country": "United States",
            "phone": None, "title": None}
    base.update(kw)
    return base


async def ready_crm_step(r, keys, kind: StepKind, inputs: dict, pc: Postcondition, lane="lead:1",
                         run_id: str | None = None) -> Step:
    run_id = run_id or (await new_run(r, keys)).id
    step = await ledger.create_step(r, keys, Step(
        run_id=run_id, kind=kind, skill=Skill.API_ESPOCRM, title=kind.value, inputs=inputs, postcondition=pc,
        lane=lane, side_effect=True), actor="test")
    await ledger.transition(r, keys, step.id, S.READY, actor="o", actor_role="orchestrator")
    return step


def api_worker(r, keys, agent_id="worker-api", **kw):
    w = build_worker(r, keys, agent_id, secrets_keys=SECRETS, **kw)
    w.block_ms = 150
    return w


@pytest.fixture
async def verifier(r, keys):
    factory = make_context_factory(secrets_keys=SECRETS)
    yield Verifier(r, keys, context_factory=factory, block_ms=150)
    await factory.aclose()


async def admin_contacts(email: str) -> list[dict]:
    async with admin_client() as a:
        params = {"where[0][type]": "equals", "where[0][attribute]": "emailAddress", "where[0][value]": email}
        return (await a.get("/Contact", params=params)).json().get("list", [])


def create_pc(email: str, **expect) -> Postcondition:
    return Postcondition(check="crm.contact_exists", args={"email": email}, expect=expect)


async def test_false_claim_rejected_then_retry_creates_exactly_one(r, keys, verifier, cleanup, uniq):
    ld = lead(uniq)
    cleanup.email(ld["email"])
    step = await ready_crm_step(r, keys, StepKind.CRM_CREATE_CONTACT, {"lead": ld},
                                create_pc(ld["email"], owner="a.chen"))
    await faults.set_fault(r, keys, "false_claim", shots=1, scope=Skill.API_ESPOCRM.value, actor="test")
    worker = api_worker(r, keys)

    await worker.run_until_idle()
    s = await ledger.get_step(r, keys, step.id)
    assert s.status == S.CLAIMED_DONE and s.claim.acted is False and s.claim.summary == "done"
    assert await admin_contacts(ld["email"]) == []  # REST shows nothing

    await verifier.run_until_idle()
    s = await ledger.get_step(r, keys, step.id)
    assert s.status == S.READY
    reason = s.history[-1].verdict.reason
    assert s.history[-1].outcome == "rejected" and f"no CRM contact has email {ld['email']}" in reason

    await worker.run_until_idle()
    await verifier.run_until_idle()
    s = await ledger.get_step(r, keys, step.id)
    assert s.status == S.COMMITTED, s.history[-1].verdict
    assert [h.outcome for h in s.history] == ["rejected", "committed"]
    contacts = await admin_contacts(ld["email"])
    assert len(contacts) == 1
    facts = await ledger.get_facts(r, keys, step.run_id)
    assert facts["lead:1.contact_id"] == contacts[0]["id"] == s.claim.data["contact_id"]
    assert facts["lead:1.action"] == "created" and facts["lead:1.owner"] == "a.chen"
    assert facts["lead:1.crm_url"].endswith(f"#Contact/view/{contacts[0]['id']}")
    # timeline: claimed -> rejected -> claimed -> verified -> committed
    tl = [e.type for e in await read_events(r, keys, run_id=step.run_id) if e.step_id == step.id
          and e.type in (EventType.STEP_CLAIMED, EventType.STEP_REJECTED, EventType.STEP_VERIFIED,
                         EventType.STEP_COMMITTED)]
    assert tl == [EventType.STEP_CLAIMED, EventType.STEP_REJECTED, EventType.STEP_CLAIMED,
                  EventType.STEP_VERIFIED, EventType.STEP_COMMITTED]


async def test_rejection_reason_in_history_and_next_attempt_context(r, keys, verifier, cleanup, uniq):
    ld = lead(uniq)
    cleanup.email(ld["email"])
    step = await ready_crm_step(r, keys, StepKind.CRM_CREATE_CONTACT, {"lead": ld}, create_pc(ld["email"]))
    seen: list[list[str]] = []
    inner = make_handler(secrets_keys=SECRETS)

    async def spy(step, ctx):
        seen.append(list(ctx.rejection_reasons))
        return await inner(step, ctx)

    await faults.set_fault(r, keys, "false_claim", shots=1, scope="worker-spy")
    worker = Worker(r, keys, api_worker(r, keys, "worker-spy").card, spy, block_ms=150)
    await worker.run_until_idle()  # false claim: the handler is not called
    await verifier.run_until_idle()
    s = await ledger.get_step(r, keys, step.id)
    assert ledger.rejection_reasons(s) == [s.history[0].verdict.reason]
    await worker.run_until_idle()
    await inner.aclose()
    assert len(seen) == 1 and "no CRM contact has email" in seen[0][0]
    s = await ledger.get_step(r, keys, step.id)
    obs = s.history[-1].observations
    assert any("no CRM contact" in (o.get("retry_after_rejection") or "") for o in obs)
    assert any(o.get("acted") is True and o.get("contact_id") for o in obs)


async def test_simulated_takeover_creates_one_contact(r, keys, verifier, cleanup, uniq):
    """Worker A creates the contact, then loses its lease before claiming;
    worker B takes over: check-then-act finds the contact, no second write."""
    ld = lead(uniq)
    cleanup.email(ld["email"])
    step = await ready_crm_step(r, keys, StepKind.CRM_CREATE_CONTACT, {"lead": ld}, create_pc(ld["email"]))
    gate = asyncio.Event()
    inner_a = make_handler(secrets_keys=SECRETS)

    async def acts_then_hangs(step, ctx):
        out = await inner_a(step, ctx)
        await gate.wait()  # "dies" before claiming
        return out

    a = Worker(r, keys, api_worker(r, keys, "worker-a").card, acts_then_hangs, block_ms=150)
    await a.start()
    got = await a.consumer.next(1000)
    task = asyncio.create_task(a.process(got[0]))
    await wait_for(lambda: admin_contacts(ld["email"]), timeout=20)
    # the lease expires (A is wedged) and the reaper requeues the step
    await r.delete(keys.lease(step.id))
    s = await ledger.expire_lease(r, keys, step.id)
    assert s.status == S.READY

    b = api_worker(r, keys, "worker-b")
    await b.run_until_idle()
    gate.set()
    outcome = await task
    await inner_a.aclose()
    assert outcome in ("claim_refused", "lease_lost")
    s = await ledger.get_step(r, keys, step.id)
    assert s.status == S.CLAIMED_DONE and s.claim.worker == "worker-b"
    assert s.claim.acted is False and s.claim.data["action"] == "exists"
    await verifier.run_until_idle()
    s = await ledger.get_step(r, keys, step.id)
    assert s.status == S.COMMITTED
    assert len(await admin_contacts(ld["email"])) == 1
    assert (await ledger.get_facts(r, keys, s.run_id))["lead:1.action"] == "created"


async def test_create_contact_twice_same_run_is_one_contact(r, keys, verifier, cleanup, uniq):
    ld = lead(uniq)
    cleanup.email(ld["email"])
    run = await new_run(r, keys)
    s1 = await ready_crm_step(r, keys, StepKind.CRM_CREATE_CONTACT, {"lead": ld}, create_pc(ld["email"]),
                              run_id=run.id)
    s2 = await ready_crm_step(r, keys, StepKind.CRM_CREATE_CONTACT, {"lead": ld}, create_pc(ld["email"]),
                              run_id=run.id, lane="lead:2")
    await api_worker(r, keys).run_until_idle()
    await verifier.run_until_idle()
    steps = [await ledger.get_step(r, keys, x.id) for x in (s1, s2)]
    assert [s.status for s in steps] == [S.COMMITTED, S.COMMITTED]
    assert sorted(s.claim.acted for s in steps) == [False, True]
    assert len(await admin_contacts(ld["email"])) == 1


async def test_search_task_facts_and_blocked_input(r, keys, verifier, cleanup, uniq):
    run = await new_run(r, keys)
    # search an existing seeded contact (exact email, other casing)
    marcus = {"row": 2, "name": "Marcus Lee", "email": "MARCUS.LEE@ACME.COM", "company": "Acme Corp", "country": "UK"}
    search = await ready_crm_step(r, keys, StepKind.CRM_SEARCH_CONTACT, {"lead": marcus, "threshold": 0.85},
                                  Postcondition(check="crm.lookup_matches", args={"lead": marcus}),
                                  lane="lead:2", run_id=run.id)
    # create + follow-up task for a new lead
    ld = lead(uniq, 5)
    cleanup.email(ld["email"])
    create = await ready_crm_step(r, keys, StepKind.CRM_CREATE_CONTACT, {"lead": ld}, create_pc(ld["email"]),
                                  lane="lead:5", run_id=run.id)
    # no email: SkillInputError -> blocked observation -> rejected
    nomail = await ready_crm_step(r, keys, StepKind.CRM_CREATE_CONTACT, {"lead": lead(uniq, 11, email=None)},
                                  create_pc(f"nobody@{uniq}.test"), lane="lead:11", run_id=run.id)
    worker = api_worker(r, keys)
    await worker.run_until_idle()
    await verifier.run_until_idle()

    facts = await ledger.get_facts(r, keys, run.id)
    assert facts["lead:2.lookup"]["result"] == "matched"
    seeded = await admin_contacts("marcus.lee@acme.com")
    assert facts["lead:2.contact_id"] == seeded[0]["id"] == facts["lead:2.lookup"]["contact_id"]
    assert facts["lead:2.owner"] == "r.silva" and facts["lead:2.crm_url"]
    assert (await ledger.get_step(r, keys, search.id)).status == S.COMMITTED

    contact_id = facts["lead:5.contact_id"]
    assert (await ledger.get_step(r, keys, create.id)).status == S.COMMITTED

    s = await ledger.get_step(r, keys, nomail.id)
    assert s.status == S.READY and ledger.rejection_count(s) == 1
    assert any("needs lead.email" in (o.get("blocked") or "") and o.get("retryable") is False
               for o in s.history[0].observations)
    assert s.history[0].claim.data["blocked"] is True

    subject = f"Follow up: Signal Summit {uniq}"
    task = await ready_crm_step(r, keys, StepKind.CRM_CREATE_TASK,
                                {"contact_id": "fact:lead:5.contact_id", "subject": subject, "due": "2026-10-07"},
                                Postcondition(check="crm.task_exists", args={"contact_id": "fact:lead:5.contact_id"},
                                              expect={"subject": subject, "due": "2026-10-07", "owner": "a.chen"}),
                                lane="lead:5", run_id=run.id)
    await worker.run_until_idle()
    await verifier.run_until_idle()
    s = await ledger.get_step(r, keys, task.id)
    assert s.status == S.COMMITTED, s.history[-1].verdict
    facts = await ledger.get_facts(r, keys, run.id)
    assert facts["lead:5.task_id"] == s.claim.data["task_id"]
    assert facts["lead:5.task_due"] == "2026-10-07" and facts["lead:5.task_owner"] == "a.chen"
    assert s.claim.data["contact_id"] == contact_id


async def test_scripted_phase2_run_parse_then_three_contacts(r, keys, verifier, cleanup, repo):
    """Phase 2 acceptance in-process: parse -> create 3 contacts via api.espocrm
    -> verify, with false_claim armed for the first CRM step."""
    spec = json.loads(Path(repo, "tests/specs/phase2_crm.json").read_text())
    csv = f"{repo}/data/event_attendees.csv"  # in-process: absolute path (DATA_DIR differs in the test container)
    for st in spec["steps"]:
        if st["kind"] == "file.parse":
            st["inputs"]["file"] = st["postcondition"]["args"]["file"] = csv
    emails = [st["postcondition"]["args"]["email"] for st in spec["steps"] if st["kind"] == "crm.create_contact"]
    for e in emails:
        if await admin_contacts(e):
            pytest.skip(f"{e} already in the CRM (acceptance run left it); reseed for a clean run")
        cleanup.email(e)
    await faults.set_fault(r, keys, "false_claim", shots=1, scope=Skill.API_ESPOCRM.value)
    run, steps = await submit_spec(r, keys, spec, actor="test")
    parser = Worker(r, keys, parser_card("worker-parser"), parser_handler(f"{repo}/data"), block_ms=150)
    api = api_worker(r, keys)
    for _ in range(6):
        await parser.run_until_idle()
        await api.run_until_idle()
        await verifier.run_until_idle()
        await ledger.release_dependents(r, keys, run.id, actor="test")
        if all(s.status == S.COMMITTED for s in await ledger.list_steps(r, keys, run.id)):
            break
    final = await ledger.list_steps(r, keys, run.id)
    assert [s.status for s in final] == [S.COMMITTED] * 4, [(s.kind, s.status) for s in final]
    for e in emails:
        assert len(await admin_contacts(e)) == 1
    facts = await ledger.get_facts(r, keys, run.id)
    assert {facts[f"lead:{n}.owner"] for n in (1, 3, 4)} == {"a.chen", "r.silva"}
    assert facts["lead:3.owner"] == "r.silva" and facts["lead:1.owner"] == facts["lead:4.owner"] == "a.chen"
    rejected = [s for s in final if ledger.rejection_count(s) == 1]
    assert len(rejected) == 1 and rejected[0].kind == StepKind.CRM_CREATE_CONTACT
