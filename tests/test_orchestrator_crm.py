"""Track G acceptance: the one-line goal end to end against the seeded EspoCRM.

Scripted LLM (tests/fixtures/llm/orchestrator.json) for understand + plan,
crm_write_path=api: the real parser worker, the api.espocrm handlers in a
local Worker, the verifier with the read-only CRM reader, and the
orchestrator, all in-process on a namespaced ledger. Needs `make up && make seed`.
"""

from __future__ import annotations

import asyncio

import pytest
from crm_helpers import admin_client, crm_secrets  # noqa: F401 - fixture

from ledger_core import ledger, llm
from ledger_core.llm_scripted import ScriptedBackend
from ledger_core.orchestrator import Orchestrator, submit_goal
from ledger_core.orchestrator_lanes import lane_outcomes
from ledger_core.orchestrator_local import api_worker, drafter_worker, local_verifier, parser_worker
from ledger_core.protocol import EventType, RunStatus, StepKind, StepStatus

pytestmark = pytest.mark.crm

GOAL = "Add the leads from yesterday's event to the CRM and set up follow-ups."
NEW_EMAILS = ["priya@northwind.com", "lena@kestrel-labs.io", "dana@helixbio.com", "tom.becker@orbitalfreight.com",
              "omar@brightline.co", "grace.wu@tallgrass.com", "sam@lumen.io"]
SEEDED = ["marcus.lee@acme.com", "hannah@fieldstone.dev"]
SUBJECT = "Follow up: Signal Summit"
EXTRA_KEEP = {"benjamin@quarrydata.com"}  # seeded addresses of contacts a review may enrich

# plans/02 "Demo data": expected outcome and owner per lane (rows 7, 9, 11 wait for review in W2)
EXPECTED = {
    "lead:1": ("done", "created", "a.chen"), "lead:2": ("done", "updated", "r.silva"),
    "lead:3": ("done", "created", "r.silva"), "lead:4": ("done", "created", "a.chen"),
    "lead:5": ("done", "created", "r.silva"), "lead:8": ("done", "updated", "a.chen"),
    "lead:10": ("done", "created", "a.chen"), "lead:12": ("done", "created", "r.silva"),
    "lead:7": ("waiting", None, None), "lead:9": ("waiting", None, None), "lead:11": ("waiting", None, None),
}


async def _scrub_crm() -> None:
    """Remove what earlier demo runs created: new demo contacts, and the
    follow-up tasks on the seeded ones. The seed itself is left as is."""
    async with admin_client() as a:
        async def contacts(email):
            params = {"where[0][type]": "equals", "where[0][attribute]": "emailAddress", "where[0][value]": email}
            return (await a.get("/Contact", params=params)).json().get("list", [])

        async def drop_tasks(cid):
            params = {"where[0][type]": "equals", "where[0][attribute]": "parentId", "where[0][value]": cid}
            for t in (await a.get("/Task", params=params)).json().get("list", []):
                if t.get("name") == SUBJECT:
                    await a.delete(f"/Task/{t['id']}")

        for e in NEW_EMAILS:
            for c in await contacts(e):
                await drop_tasks(c["id"])
                await a.delete(f"/Contact/{c['id']}")
        for e in SEEDED + ["benjamin@quarrydata.com"]:
            for c in await contacts(e):
                await drop_tasks(c["id"])
        # Track J: a resolved review (Ben Ortiz -> match_existing) adds the badge email
        # as a secondary address on the seeded Benjamin; drop it so the next run sees
        # the probable match again.
        for c in await contacts("ben.ortiz@gmail.com"):
            full = (await a.get(f"/Contact/{c['id']}")).json()
            keep = [x for x in full.get("emailAddressData") or [] if x.get("emailAddress", "").lower() in EXTRA_KEEP]
            if keep:
                keep[0]["primary"] = True
                await a.put(f"/Contact/{c['id']}", json={"emailAddressData": keep, "emailAddress": keep[0]["emailAddress"]})
            else:
                await drop_tasks(c["id"])
                await a.delete(f"/Contact/{c['id']}")


@pytest.fixture
async def clean_crm(crm_secrets):
    await _scrub_crm()
    yield
    await _scrub_crm()


@pytest.fixture
async def scripted(repo):
    prev = llm.get_backend()
    llm.set_backend(ScriptedBackend(f"{repo}/tests/fixtures/llm"))
    yield
    llm.set_backend(prev)


async def test_demo_goal_end_to_end_api_path(r, keys, repo, clean_crm, scripted):
    data, pbs = f"{repo}/data", f"{repo}/playbooks"
    agents = [parser_worker(r, keys, data_dir=data, block_ms=300), api_worker(r, keys, block_ms=300),
              local_verifier(r, keys, data_dir=data, block_ms=300),
              drafter_worker(r, keys, block_ms=300)]  # W3 (Track K): done lanes now also draft a follow-up
    orch = Orchestrator(r, keys, playbook_dir=pbs, run_reaper=False)
    tasks = [asyncio.create_task(a.run()) for a in agents]
    try:
        run = await submit_goal(r, keys, GOAL, input_file="event_attendees.csv", config={"crm_write_path": "api"},
                                playbook_dir=pbs, actor="test")
        run = await orch.run_until_settled(run.id, timeout=180)
    finally:
        for a in agents:
            a.stop()
        await asyncio.gather(*tasks, return_exceptions=True)
        if orch._crm is not None:
            await orch._crm.aclose()

    steps = await ledger.list_steps(r, keys, run.id)
    facts = await ledger.get_facts(r, keys, run.id)
    lanes = {x["lane"]: x for x in lane_outcomes(steps, facts)}
    got = {k: (v["status"], v["action"], v["owner"]) for k, v in lanes.items()}
    assert got == EXPECTED

    # every CRM write went through api.espocrm, none failed or was replanned
    crm = [s for s in steps if s.kind.value.startswith("crm.")]
    assert {s.skill.value for s in crm} == {"api.espocrm"}
    assert all(s.status == StepStatus.COMMITTED for s in crm)
    assert sum(s.kind == StepKind.CRM_CREATE_CONTACT for s in crm) == 6
    assert sum(s.kind == StepKind.CRM_UPDATE_CONTACT for s in crm) == 2
    assert sum(s.kind == StepKind.CRM_CREATE_TASK for s in crm) == 8
    reviews = {s.lane: s.inputs["reason"] for s in steps if s.kind == StepKind.REVIEW_AMBIGUITY}
    assert reviews == {"lead:7": "probable_match", "lead:9": "ambiguous_account", "lead:11": "phone_only"}

    # contact / task / owner criteria verified for every done lane; the run waits on reviews + email (W3)
    assert run.status == RunStatus.COMPLETED_PENDING_INPUT
    crit = {c.id: c for c in run.criteria}
    assert [c.check for c in run.criteria] == ["crm.no_duplicate", "crm.no_duplicate", "crm.task_exists",
                                                "crm.contact_exists", "review.decided", "email.sent"]
    for cid in ("c1", "c2", "c3", "c4"):
        assert crit[cid].status == "pending" and crit[cid].evidence.startswith("8/11 ")
        assert "waiting on lead:7, lead:9, lead:11" in crit[cid].evidence
    assert crit["c5"].status == "pending" and crit["c6"].status == "pending"
    ev = [e for e in await ledger.run_events(r, keys, run.id) if e.type == EventType.RUN_COMPLETED_PENDING_INPUT][0]
    assert ev.payload["lanes"] == {"done": 8, "waiting": 3}

    # what the sweep saw in REST: one contact per email with the routed owner, one task each
    async with admin_client() as a:
        for lane in (v for v in lanes.values() if v["status"] == "done"):
            params = {"where[0][type]": "equals", "where[0][attribute]": "emailAddress", "where[0][value]": lane["email"]}
            hits = (await a.get("/Contact", params=params)).json()["list"]
            assert len(hits) == 1, lane
            tparams = {"where[0][type]": "equals", "where[0][attribute]": "parentId", "where[0][value]": hits[0]["id"]}
            tasks_ = [t for t in (await a.get("/Task", params=tparams)).json()["list"] if t["name"] == SUBJECT]
            assert len(tasks_) == 1, lane
