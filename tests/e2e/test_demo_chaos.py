"""e2e: the chaos run (plans/02 F1-F4, G2; plans/05 chaos commands).

During ONE run from clean: false_claim (next browser step claims done without
acting), expire_session (CRM cookie cleared), model_outage (primary worker
model invalid) and a docker kill of the browser operator holding a lease
mid-step. The run still completes (after the one human answer), the CRM has
zero duplicate contacts (REST), and the evidence report pairs every fault with
its recovery.
"""

from __future__ import annotations

import pytest
from e2e_helpers import EXPECTED, Api, Crm, Docker, Mailpit, log, outcomes, wait_for

from test_demo_clean import answer_sam, assert_sent_only_with_approval

pytestmark = pytest.mark.e2e

BROWSERS = ("worker-browser-1", "worker-browser-2")
CRM_WRITES = {"crm.create_contact", "crm.update_contact", "crm.create_task"}


def _lease_holder(api: Api, run_id: str) -> tuple[str, str] | None:
    """(agent, step) for a browser operator holding a CRM write step of this run
    right now (preferring create_contact: the duplicate-prone case)."""
    steps = {s["id"]: s for s in api.steps(run_id)}
    held = [(a["id"], a["current_step"]) for a in api.agents()
            if a["id"] in BROWSERS and a.get("alive") and a.get("current_step") in steps
            and steps[a["current_step"]]["status"] == "leased"]
    held.sort(key=lambda x: steps[x[1]]["kind"] != "crm.create_contact")
    for agent, sid in held:
        if steps[sid]["kind"] in CRM_WRITES:
            return agent, sid
    return None


def test_chaos_run(api: Api, crm: Crm, mail: Mailpit, docker: Docker, clean):
    run_id = api.submit()
    for fault in ("false_claim", "expire_session", "model_outage"):
        api.inject(fault, run_id=run_id)

    agent, step_id = wait_for(lambda: _lease_holder(api, run_id), 240, "a browser operator holding a CRM write",
                              every=0.5)
    log(f"killing {agent} while it holds {step_id}")
    out = api.inject("kill_worker", run_id=run_id, agent_id=agent, kill=True)
    assert "error" not in (out.get("kill") or {}), out
    try:
        api.wait_settled(run_id, timeout=480)
        run = api.run(run_id)
        assert run["status"] == "completed_pending_input", run
        answer_sam(api, run_id)
        run = api.wait_settled(run_id, timeout=300, statuses=("completed", "failed"))
        assert run["status"] == "completed", run
    finally:
        if not docker.running(agent):  # bring the killed replica back for the next scenario
            docker.start(agent)
        api.wait_agents(timeout=180)

    # zero duplicate contacts, by REST: one contact per lead email, nothing extra
    assert crm.duplicates() == {}
    emails = {v[2] for v in EXPECTED.values() if v[2] and v[0] != "skipped"} - {"ben.ortiz@gmail.com"}
    for email in sorted(emails):
        assert len(crm.contacts_with(email)) == 1, email
    seeded = len(crm.seed["contacts"])
    assert len(crm.contacts()) == seeded + 7  # 6 created + Sam after the answer; updates add none
    got = outcomes(api.report(run_id))
    assert {n: o for n, o in got.items() if n != 9} == {n: v[0] for n, v in EXPECTED.items() if n != 9}
    assert got[9] == "created"
    assert_sent_only_with_approval(api, mail, run_id)

    # the report pairs every fault with its recovery (G2)
    faults = {f["fault"]: f for f in api.report(run_id)["faults"]}
    for f in faults.values():
        log(f"fault {f['fault']}: {f['what']} -> {f['recovery']} (lost {f['lost_s']}s)")
    assert set(faults) >= {"false_claim", "expire_session", "model_outage", "kill_worker"}, list(faults)
    assert all(f["recovered"] and f["recovery_events"] for f in faults.values()), faults
    assert "verifier rejected" in faults["false_claim"]["recovery"]
    assert "retry committed" in faults["false_claim"]["recovery"]
    assert "re-authenticated" in faults["expire_session"]["recovery"]
    assert faults["model_outage"]["recovery"].startswith("model.fallback")
    kill = faults["kill_worker"]
    assert agent in kill["what"] and "took over" in kill["recovery"] and "committed" in kill["recovery"], kill
    other = next(b for b in BROWSERS if b != agent)
    assert other in kill["recovery"], kill
    # the takeover is fenced: the dead holder never committed the step it held
    evs = api.events(run_id, "step.leased", "step.committed", "step.lease_expired")
    held = [e for e in evs if e["step_id"] == kill["step_id"]]
    assert any(e["type"] == "step.lease_expired" for e in held)
    assert [e["actor"] for e in held if e["type"] == "step.leased"][-1] == other
