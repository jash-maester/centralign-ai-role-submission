"""Track I: the browser worker on the ledger (make test-browser).

Real Redis, the seeded EspoCRM, the real Verifier (CRM checks over REST with
the read-only key) and real browser workers. CRM state is asserted through
REST as the admin user, never through the operator.
"""

from __future__ import annotations

import asyncio
import contextlib
import os
import signal
import subprocess
import sys
import time
from pathlib import Path

import httpx
import pytest

pytest.importorskip("playwright")
worker_mod = pytest.importorskip("browser_worker.worker")

from browser_worker.operator import Operator  # noqa: E402
from browser_worker.recovery import PageState, RecoveryAction  # noqa: E402
from ledger_core import ledger, reaper, verifier  # noqa: E402
from ledger_core.config import RunConfig  # noqa: E402
from ledger_core.crm_api import reader_from_redis  # noqa: E402
from ledger_core.llm_testing import StubLLM  # noqa: E402
from ledger_core.protocol import EventType, Postcondition, Skill, Step, StepKind, StepStatus  # noqa: E402
from ledger_core.settings import get_settings  # noqa: E402
from ledger_core.worker_base import Worker  # noqa: E402

pytestmark = pytest.mark.browser
S = StepStatus


# ---------------------------------------------------------------- fixtures
@pytest.fixture
async def admin():
    s = get_settings()
    async with httpx.AsyncClient(
        base_url=f"{s.crm_internal_url}/api/v1", auth=(s.espo_admin_user, s.espo_admin_password), timeout=30
    ) as client:
        yield client


async def contacts_by_email(admin, email: str) -> list[dict]:
    params = {"where[0][type]": "equals", "where[0][attribute]": "emailAddress", "where[0][value]": email}
    resp = await admin.get("/Contact", params=params)
    resp.raise_for_status()
    return resp.json()["list"]


async def tasks_of(admin, cid: str) -> list[dict]:
    params = {"where[0][type]": "equals", "where[0][attribute]": "parentId", "where[0][value]": cid,
              "select": "id,name,dateEndDate,assignedUserName"}
    resp = await admin.get("/Task", params=params)
    resp.raise_for_status()
    return resp.json()["list"]


@pytest.fixture
async def cleanup(admin):
    """Emails whose contacts (and their tasks) are deleted after the test."""
    emails: list[str] = []
    yield emails
    for e in emails:
        for c in await contacts_by_email(admin, e):
            for t in await tasks_of(admin, c["id"]):
                await admin.delete(f"/Task/{t['id']}")
            await admin.delete(f"/Contact/{c['id']}")


async def crm_context(step, r, keys):
    ctx = await verifier.default_context(step, r, keys)
    ctx.crm = await reader_from_redis(r)  # seed stores the CRM keys in the default namespace
    return ctx


def make_verifier(r, keys) -> verifier.Verifier:
    return verifier.Verifier(r, keys, context_factory=crm_context, block_ms=500)


async def new_run(r, keys, **cfg) -> str:
    run = await ledger.create_run(r, keys, goal="browser worker test", actor="test")
    await ledger.set_run_config(r, keys, run.id, RunConfig(**cfg), actor="test")
    return run.id


def contact_step(run_id: str, uniq: str, email: str, **kw) -> Step:
    return Step(
        run_id=run_id, kind=StepKind.CRM_CREATE_CONTACT, skill=Skill.BROWSER_ESPOCRM, title="create contact",
        lane="lead:1",
        inputs={"lead": {"name": f"Ada Lovelace{uniq}", "email": email, "phone": "+1 415 555 0142",
                         "company": "Nonexistent Co", "country": "US"}, "owner": "a.chen"},
        postcondition=Postcondition(check="crm.contact_exists", args={"email": email},
                                    expect={"owner": "a.chen"}),
        **kw,
    )


def task_step(run_id: str, contact_id: str, subject: str, due: str) -> Step:
    return Step(
        run_id=run_id, kind=StepKind.CRM_CREATE_TASK, skill=Skill.BROWSER_ESPOCRM, title="follow-up task",
        lane="lead:1",
        inputs={"contact_id": contact_id, "subject": subject, "due": due, "owner": "a.chen"},
        postcondition=Postcondition(check="crm.task_exists", args={"contact_id": contact_id},
                                    expect={"subject": subject, "due": due, "owner": "a.chen"}),
    )


async def make_ready(r, keys, step: Step) -> str:
    created = await ledger.create_step(r, keys, step, actor="test")
    await ledger.transition(r, keys, created.id, S.READY, actor="orchestrator", actor_role="orchestrator")
    return created.id


async def drive(r, keys, worker: Worker, vf: verifier.Verifier, step_id: str) -> Step:
    assert await worker.run_until_idle(max_messages=1) == 1
    await vf.run_until_idle(max_messages=1)
    return await ledger.get_step(r, keys, step_id)


def assert_screens(step: Step) -> None:
    root = Path(get_settings().evidence_dir)
    ev = step.claim.evidence
    assert any(p.endswith("_before.png") for p in ev) and any(p.endswith("_after.png") for p in ev), ev
    for p in ev:
        assert (root / p).is_file(), p


@pytest.fixture
async def browser(tmp_path, uniq):
    op = Operator(f"tw-{uniq}", state_dir=str(tmp_path))
    await op.start()
    yield op
    await op.close()


# ---------------------------------------------------------------- pure glue
def test_normalize_inputs_planner_shapes():
    n = worker_mod.normalize_inputs
    got = n(StepKind.CRM_CREATE_CONTACT, {"lead": {"name": "sam ito", "email": "S@x.test", "company": "Acme"},
                                          "owner": "r.silva"})
    assert got == {"first_name": "Sam", "last_name": "Ito", "email": "S@x.test", "owner": "r.silva",
                   "account_name": "Acme"}
    got = n(StepKind.CRM_CREATE_TASK, {"contact_id": "c1", "event_name": "Expo", "event_date": "2026-10-02"})
    assert got == {"contact_id": "c1", "subject": "Follow up: Expo", "due_date": "2026-10-06"}  # Fri -> Tue
    got = n(StepKind.CRM_UPDATE_CONTACT, {"contact_id": "c1", "lead": {"email": "b@x.test", "phone": "1"}})
    assert got == {"contact_id": "c1", "secondary_email": "b@x.test", "phone": "1"}
    with pytest.raises(worker_mod.BrowserInputError):
        n(StepKind.CRM_CREATE_CONTACT, {"lead": {"name": "No Mail", "phone": "1"}, "owner": "a.chen"})
    with pytest.raises(worker_mod.BrowserInputError):
        n(StepKind.CRM_CREATE_CONTACT, {"lead": {"name": "No Owner", "email": "o@x.test"}})


def test_search_claim_vocabulary():
    SR = worker_mod.SkillResult
    hit = SR(skill="crm.search_contact", data={"candidates": [{"id": "c1", "name": "A B", "account": "X"}]})
    assert worker_mod.search_claim({}, hit, None)["result"] == "matched"
    none = SR(skill="crm.search_contact", data={"candidates": []})
    fuzzy = SR(skill="crm.search_contact", data={"candidates": [
        {"id": "c2", "name": "Ben Ortiz", "account": "Quarry", "company_score": 1.0, "name_score": 0.9},
        {"id": "c3", "name": "Bo Other", "account": "Elsewhere", "company_score": 0.1, "name_score": 0.95}]})
    out = worker_mod.search_claim({"threshold": 0.85}, none, fuzzy)
    assert out["result"] == "ambiguous" and [c["id"] for c in out["candidates"]] == ["c2"]
    assert worker_mod.search_claim({}, none, None)["result"] == "none"


async def test_llm_recovery_chooser_is_bounded_to_the_action_set():
    stub = StubLLM().on("worker", worker_mod.RecoveryChoice, response={"action": "relogin", "reason": "login form"})
    chooser = worker_mod.LLMRecoveryChooser()
    with stub.installed():
        assert await chooser(PageState.LOGIN, 1, "SessionLost") is RecoveryAction.RELOGIN
    assert chooser.calls[-1]["action"] == "relogin"
    broken = StubLLM().on("worker", worker_mod.RecoveryChoice, exc=RuntimeError("outage"))
    with broken.installed():  # LLM failure -> deterministic default policy
        assert await chooser(PageState.LOGIN, 1, "SessionLost") is RecoveryAction.RELOGIN
    assert "fallback" in chooser.calls[-1]


# ---------------------------------------------------------------- planned -> committed
async def test_create_contact_and_task_commit_through_browser(r, keys, uniq, admin, cleanup, browser):
    email = f"ada.{uniq}@browser-i.test"
    cleanup.append(email)
    run_id = await new_run(r, keys)
    w = Worker(r, keys, worker_mod.browser_card(browser.agent_id), worker_mod.BrowserHandler(browser), block_ms=500)
    vf = make_verifier(r, keys)

    sid = await make_ready(r, keys, contact_step(run_id, uniq, email))
    step = await drive(r, keys, w, vf, sid)
    assert step.status is S.COMMITTED, (step.status, step.history[-1].verdict)
    assert step.claim.acted and step.claim.data["action"] == "created" and step.claim.data["channel"] == "browser"
    assert_screens(step)
    contacts = await contacts_by_email(admin, email)
    assert [c["id"] for c in contacts] == [step.claim.data["contact_id"]]
    facts = await ledger.get_facts(r, keys, run_id)
    # checks/crm_facts.py (Track F) commits lane facts "<lead prefix>.contact_id"
    from ledger_core.checks.crm_facts import lead_prefix
    assert facts[f"{lead_prefix(step)}.contact_id"] == contacts[0]["id"]

    subject, due = f"Follow up: Expo {uniq}", "2026-10-06"
    tid = await make_ready(r, keys, task_step(run_id, contacts[0]["id"], subject, due))
    task = await drive(r, keys, w, vf, tid)
    assert task.status is S.COMMITTED, (task.status, task.history[-1].verdict)
    assert_screens(task)
    tasks = await tasks_of(admin, contacts[0]["id"])
    assert [(t["id"], t["name"], t["dateEndDate"]) for t in tasks] == [(task.claim.data["task_id"], subject, due)]

    events = await ledger.run_events(r, keys, run_id)
    obs = [e for e in events if e.type == EventType.STEP_OBSERVATION and e.step_id == sid]
    assert any("created contact" in (e.payload.get("seen") or "") for e in obs), [e.payload for e in obs]

    # A re-run of the same work (as after a crash) acts nothing and still commits.
    again = await drive(r, keys, w, vf, await make_ready(r, keys, contact_step(run_id, uniq, email)))
    assert again.status is S.COMMITTED and not again.claim.acted and again.claim.data["action"] == "exists"
    assert len(await contacts_by_email(admin, email)) == 1


async def test_expired_session_relogin_then_step_completes(r, keys, uniq, admin, cleanup, browser):
    email = f"grace.{uniq}@browser-i.test"
    cleanup.append(email)
    run_id = await new_run(r, keys)
    await browser.ensure_logged_in()
    logins = browser.login_count
    await r.hset(keys.faults, "expire_session", "on")
    w = Worker(r, keys, worker_mod.browser_card(browser.agent_id), worker_mod.BrowserHandler(browser), block_ms=500)

    sid = await make_ready(r, keys, contact_step(run_id, uniq, email))
    step = await drive(r, keys, w, make_verifier(r, keys), sid)
    assert step.status is S.COMMITTED, (step.status, step.history[-1].verdict)
    assert browser.login_count == logins + 1  # re-authenticated exactly once
    assert not await r.hexists(keys.faults, "expire_session")  # one shot, even when "on"
    events = await ledger.run_events(r, keys, run_id)
    assert any(e.type == EventType.FAULT_INJECTED and e.payload.get("fault") == "expire_session" for e in events)
    seen = [e.payload.get("seen", "") for e in events if e.type == EventType.STEP_OBSERVATION]
    assert any("login form shown" in s for s in seen) and any(s.startswith("logged in") for s in seen), seen
    assert len(await contacts_by_email(admin, email)) == 1


# ---------------------------------------------------------------- kill -> takeover
def spawn_replica(agent_id: str, ns: str, pause: str) -> subprocess.Popen:
    env = {**os.environ, "AGENT_ID": agent_id, "LEDGER_NS": ns, "LEDGER_BROWSER_PAUSE": pause,
           "PYTHONPATH": os.environ.get("PYTHONPATH", "/app")}
    return subprocess.Popen([sys.executable, "-m", "browser_worker"], env=env, cwd="/app",
                            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, start_new_session=True)


async def wait_for(pred, timeout: float, what: str, every: float = 0.25):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        got = await pred()
        if got:
            return got
        await asyncio.sleep(every)
    raise AssertionError(f"timed out after {timeout}s waiting for {what}")


async def test_kill_replica_mid_step_other_takes_over(r, keys, ns, uniq, admin, cleanup):
    """Two browser replicas (own process + own Chromium each). The one holding the
    create_contact lease is SIGKILLed after it saved the contact but before it
    claimed (the `docker kill` case). The lease expires, the reaper requeues, the
    other replica takes over with a higher fence, its check finds the contact, and
    it claims acted=False. Then it creates the task: exactly one contact, one task."""
    email = f"kate.{uniq}@browser-i.test"
    cleanup.append(email)
    run_id = await new_run(r, keys, lease_ttl_s=6)
    ids = [f"wb1-{uniq}", f"wb2-{uniq}"]
    procs = {a: spawn_replica(a, ns, "after_save:60:crm.create_contact") for a in ids}
    vf = make_verifier(r, keys)
    bg = [asyncio.create_task(reaper.run_reaper(r, keys, interval_s=0.5)), asyncio.create_task(vf.run())]
    try:
        await wait_for(lambda: _all_alive(r, keys, ids), 60, "both replicas registered")
        sid = await make_ready(r, keys, contact_step(run_id, uniq, email))

        async def saved_by_holder():
            step = await ledger.get_step(r, keys, sid)
            if step.status is S.LEASED and await contacts_by_email(admin, email):
                return step
            return None

        held = await wait_for(saved_by_holder, 90, "holder saved the contact and paused before claiming")
        victim = await r.get(keys.lease(sid))
        first_fence = held.fence
        assert victim in procs
        os.killpg(procs[victim].pid, signal.SIGKILL)  # what `docker kill` does to the container
        procs[victim].wait(timeout=10)
        survivor = next(a for a in ids if a != victim)

        async def committed():
            step = await ledger.get_step(r, keys, sid)
            return step if step.status in (S.COMMITTED, S.DEAD) else None

        step = await wait_for(committed, 120, "takeover commit")
        assert step.status is S.COMMITTED, step.history[-1].verdict
        assert step.claim.worker == survivor and step.claim.fence > first_fence
        assert step.claim.acted is False and step.claim.data["action"] == "exists"
        assert any(a.outcome == "lease_expired" for a in step.history), [a.outcome for a in step.history]
        contacts = await contacts_by_email(admin, email)
        assert len(contacts) == 1 and contacts[0]["id"] == step.claim.data["contact_id"]

        subject = f"Follow up: Expo {uniq}"
        tid = await make_ready(r, keys, task_step(run_id, contacts[0]["id"], subject, "2026-10-06"))

        async def task_done():
            t = await ledger.get_step(r, keys, tid)
            return t if t.status in (S.COMMITTED, S.DEAD) else None

        task = await wait_for(task_done, 120, "task commit")
        assert task.status is S.COMMITTED and task.claim.worker == survivor, task.history[-1].verdict
        assert [t["name"] for t in await tasks_of(admin, contacts[0]["id"])] == [subject]
        events = await ledger.run_events(r, keys, run_id)
        assert any(e.type == EventType.STEP_LEASE_EXPIRED for e in events)
    finally:
        for t in bg:
            t.cancel()
        for t in bg:
            with contextlib.suppress(BaseException):
                await t
        for p in procs.values():
            if p.poll() is None:
                os.killpg(p.pid, signal.SIGTERM)
        for p in procs.values():
            try:
                p.wait(timeout=15)
            except subprocess.TimeoutExpired:
                os.killpg(p.pid, signal.SIGKILL)


async def _all_alive(r, keys, ids) -> bool:
    from ledger_core import agents

    for a in ids:
        if not await agents.get_liveness(r, keys, a):
            return False
    return True
