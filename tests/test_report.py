"""Track H: evidence report from a synthetic chaos run (G1-G3).

The run is driven through the real ledger (leases, transitions, verifier
commits) so the report sees the same steps, facts and events a live run
produces. Four faults are injected; each must be listed next to its recovery.
"""

from __future__ import annotations

import json

import pytest

from api_helpers import api_client
from ledger_core import agents, leases, ledger, report, run_config
from ledger_core.events import append_event
from ledger_core.protocol import (
    AgentCard,
    Claim,
    Criterion,
    Event,
    EventType,
    Postcondition,
    Skill,
    Step,
    StepKind,
    StepStatus,
    Verdict,
)
from ledger_helpers import lease

S = StepStatus

LEADS = {
    1: {"row": 1, "name": "Priya Raman", "company": "Northwind", "email": "priya@northwind.com"},
    2: {"row": 2, "name": "Marcus Lee", "company": "Acme Corp", "email": "marcus.lee@acme.com"},
    3: {"row": 3, "name": "Sam Ito", "company": "Lumen", "email": "sam@lumen.io"},
    4: {"row": 4, "name": "Ben Ortiz", "company": "Quarry Data", "email": "ben.ortiz@gmail.com"},
}
FLAGGED = [{"row": 5, "reason": "duplicate_in_file", "duplicate_of": 1,
            "detail": "same email as row 1; keeping the first row", "record": {"row": 5, "name": "Priya Raman"}}]


def _step(run_id: str, kind: StepKind, skill: Skill, lane: str, check: str, **kw) -> Step:
    return Step(run_id=run_id, kind=kind, skill=skill, lane=lane, title=kw.pop("title", f"{kind.value} {lane}"),
                postcondition=Postcondition(check=check, expect=kw.pop("expect", {})), **kw)


async def _ev(r, keys, run_id, type_, payload, *, step_id=None, actor="chaos"):
    await append_event(r, keys, Event(run_id=run_id, step_id=step_id, actor=actor, type=type_, payload=payload))


async def _work(r, keys, step, worker, data, *, evidence=(), ok=True, reason="verified via REST", observed=None,
                facts=None, fence=None, then=S.READY):
    """lease -> claim -> verifier verdict. Returns the fence used."""
    if fence is None:
        fence = await lease(r, keys, step.id, worker)
    await ledger.claim(r, keys, step.id, Claim(worker=worker, fence=fence, summary="done", data=data,
                                                evidence=list(evidence)))
    await leases.release(r, keys, step.id, worker)  # as worker_base does after claiming
    verdict = Verdict(ok=ok, check=step.postcondition.check, reason=reason, observed=observed or {})
    if ok:
        await ledger.commit(r, keys, step.id, verdict, facts=facts or {})
    else:
        await ledger.reject(r, keys, step.id, verdict, then=then)
    return fence


async def _ready(r, keys, step):
    await ledger.create_step(r, keys, step, actor="orchestrator")
    await ledger.transition(r, keys, step.id, S.READY, actor="orchestrator", actor_role="orchestrator")


@pytest.fixture
async def chaos_run(r, keys):
    run = await ledger.create_run(r, keys, goal="Process yesterday's Signal Summit leads", actor="api",
                                  input_file="event_attendees.csv", input_sha256="ab" * 32)
    await run_config.init(r, keys, run.id, actor="api")
    await ledger.update_run(r, keys, run.id, actor="orchestrator", event_type=EventType.RUN_UNDERSTOOD, criteria=[
        Criterion(id="c1", text="Every usable lead exists in the CRM once", check="crm.no_duplicate",
                  status="verified", evidence="REST: 1 contact per email"),
        Criterion(id="c2", text="Each lead has a follow-up task", check="crm.task_exists", status="pending"),
    ])
    for agent_id, role in (("worker-browser-1", "worker"), ("worker-browser-2", "worker"),
                           ("meta-reviewer", "meta_reviewer"), ("verifier", "verifier")):
        await agents.register_agent(r, keys, AgentCard(id=agent_id, name=agent_id, role=role, model_role=role,
                                                       container=agent_id))
    for n, rec in LEADS.items():
        await ledger.commit_fact(r, keys, run.id, f"lead:{n}", rec, source_step="stp_parse", actor="verifier")
    await ledger.commit_fact(r, keys, run.id, "parse.summary", {
        "source": "event_attendees.csv", "sha256": "ab" * 32, "total_rows": 5, "usable_rows": [1, 2, 3, 4],
        "flagged": FLAGGED, "stats": {"flagged_by_reason": {"duplicate_in_file": 1}, "emails_lowercased": 2},
    }, source_step="stp_parse", actor="verifier")

    # lead 1: F2 false claim -> verifier rejects -> retry commits; then task + email
    create1 = _step(run.id, StepKind.CRM_CREATE_CONTACT, Skill.BROWSER_ESPOCRM, "lead:1", "crm.contact_exists",
                    expect={"owner": "a.chen"})
    await _ready(r, keys, create1)
    await _ev(r, keys, run.id, EventType.FAULT_INJECTED, {"fault": "false_claim", "phase": "injected",
                                                          "switch": "false_claim:browser.espocrm"})
    fence = await lease(r, keys, create1.id, "worker-browser-1")
    await _ev(r, keys, run.id, EventType.FAULT_INJECTED, {"fault": "false_claim", "phase": "consumed"},
              step_id=create1.id, actor="worker-browser-1")
    await _work(r, keys, create1, "worker-browser-1", {}, ok=False, fence=fence,
                reason="no contact with email priya@northwind.com via REST")
    await _work(r, keys, create1, "worker-browser-1", {"contact_id": "c1", "owner": "a.chen"},
                evidence=["stp_c1_a2_after.png"], observed={"id": "c1", "owner": "a.chen"},
                facts={"lead:1.contact_id": "c1"})
    task1 = _step(run.id, StepKind.CRM_CREATE_TASK, Skill.BROWSER_ESPOCRM, "lead:1", "crm.task_exists",
                  expect={"due": "2026-10-06", "owner": "a.chen"})
    await _ready(r, keys, task1)
    await _work(r, keys, task1, "worker-browser-2", {"task_id": "t1"})
    draft1 = _step(run.id, StepKind.EMAIL_DRAFT, Skill.EMAIL_DRAFT, "lead:1", "email.draft_valid")
    await _ready(r, keys, draft1)
    await _work(r, keys, draft1, "worker-drafter", {"to": "priya@northwind.com", "subject": "Great to meet you"},
                observed={"judge_score": 0.94})
    send1 = _step(run.id, StepKind.EMAIL_SEND, Skill.EMAIL_SEND, "lead:1", "email.sent")
    await _ready(r, keys, send1)
    await _work(r, keys, send1, "worker-mailer", {"message_id": "m1"})

    # lead 2: F1 kill the lease holder -> lease expires -> other operator takes over
    upd2 = _step(run.id, StepKind.CRM_UPDATE_CONTACT, Skill.BROWSER_ESPOCRM, "lead:2", "crm.contact_exists")
    await _ready(r, keys, upd2)
    await lease(r, keys, upd2.id, "worker-browser-1")
    held = json.dumps({"ts": 1, "current_step": upd2.id, "run_id": run.id, "fence": 1})
    await _ev(r, keys, run.id, EventType.FAULT_INJECTED, {"fault": "kill_worker", "phase": "injected",
                                                          "agent_id": "worker-browser-1", "held": held})
    await _ev(r, keys, run.id, EventType.AGENT_LOST, {"agent_id": "worker-browser-1"}, actor="reaper")
    await r.delete(keys.lease(upd2.id))
    await ledger.expire_lease(r, keys, upd2.id)
    # F3 during the takeover attempt: session expired, operator re-logs in
    await _ev(r, keys, run.id, EventType.FAULT_INJECTED, {"fault": "expire_session", "phase": "injected",
                                                          "switch": "expire_session"})
    fence = await lease(r, keys, upd2.id, "worker-browser-2")
    await ledger.observe(r, keys, upd2.id, {"kind": "page", "text": "login page shown; re-authenticated"},
                         actor="worker-browser-2", fence=fence)
    await _work(r, keys, upd2, "worker-browser-2", {"contact_id": "c2", "contact_url": "http://crm/#Contact/view/c2",
                                                    "owner": "r.silva"},
                fence=fence, evidence=["stp_u2_a2_after.png"])

    # lead 3: ambiguous company -> below threshold -> escalated, waiting on a human
    rev3 = _step(run.id, StepKind.REVIEW_AMBIGUITY, Skill.REVIEW, "lead:3", "review.decided",
                 title="Which Lumen is sam@lumen.io?")
    await _ready(r, keys, rev3)
    await lease(r, keys, rev3.id, "meta-reviewer")
    await ledger.transition(r, keys, rev3.id, S.REVIEW_REQUIRED, actor="meta-reviewer", actor_role="worker",
                            fence=1)
    await ledger.transition(r, keys, rev3.id, S.INPUT_REQUIRED, actor="meta-reviewer",
                            actor_role="meta_reviewer")
    await _ev(r, keys, run.id, EventType.REVIEW_ESCALATED, {"confidence": 0.55, "threshold": 0.8},
              step_id=rev3.id, actor="meta-reviewer")

    # lead 4: F4 model outage -> fallback; reviewer decides "skip" above threshold
    rev4 = _step(run.id, StepKind.REVIEW_AMBIGUITY, Skill.REVIEW, "lead:4", "review.decided",
                 title="Personal email: Ben Ortiz")
    await _ready(r, keys, rev4)
    await _ev(r, keys, run.id, EventType.FAULT_INJECTED, {"fault": "model_outage", "phase": "injected"})
    fence = await lease(r, keys, rev4.id, "meta-reviewer")
    await _ev(r, keys, run.id, EventType.MODEL_FALLBACK, {"role": "meta_reviewer", "from": "bad/model",
                                                          "to": "good/model:free", "reason": "404"},
              step_id=rev4.id, actor="meta-reviewer")
    await _ev(r, keys, run.id, EventType.LLM_CALL, {"role": "meta_reviewer", "model": "good/model:free", "ok": True,
                                                    "total_tokens": 1200, "cost_usd": 0.0},
              step_id=rev4.id, actor="meta-reviewer")
    await r.hset(keys.llm_spend(run.id), mapping={"requests": "1", "requests:meta_reviewer": "1",
                                                  "tokens:meta_reviewer": "1200", "prompt_tokens": "1000",
                                                  "completion_tokens": "200"})
    await _work(r, keys, rev4, "meta-reviewer", {
        "decision": "skip", "confidence": 0.91, "threshold": 0.8, "decided_by": "meta-reviewer",
        "model": "good/model:free", "evidence": ["gmail.com is a personal domain", "Quarry Data has no account"],
        "reason": "personal email; playbook says skip"}, fence=fence)
    return run


async def test_report_sections_from_chaos_run(r, keys, chaos_run):
    rep = await report.build_report(r, keys, chaos_run.id)

    # faults, each next to its recovery, in the order they happened
    faults = rep["faults"]
    assert [f["fault"] for f in faults] == ["false_claim", "kill_worker", "expire_session", "model_outage"]
    assert all(f["recovered"] and f["lost_s"] is not None for f in faults), faults
    fc, kill, sess, outage = faults
    assert "verifier rejected: no contact with email priya@northwind.com" in fc["recovery"]
    assert "retry committed (attempt 2)" in fc["recovery"]
    assert "worker-browser-2 took over" in kill["recovery"] and kill["recovery"].endswith("committed")
    assert "re-authenticated" in sess["recovery"]
    assert "bad/model -> good/model:free" in outage["recovery"]
    assert {x["type"] for x in rep["recoveries"]} >= {"retry", "takeover", "model fallback", "agent lost"}

    leads = {x["n"]: x for x in rep["leads"]}
    assert {n: x["outcome"] for n, x in leads.items()} == {
        1: "created", 2: "updated", 3: "waiting", 4: "skipped", 5: "skipped"}
    l1 = leads[1]
    assert l1["owner"] == "a.chen" and l1["task_due"] == "2026-10-06" and l1["email_status"] == "sent"
    assert l1["crm_url"].endswith("/#Contact/view/c1") and l1["screenshot"] == "stp_c1_a2_after.png"
    assert l1["check"] == "crm.contact_exists" and l1["tries"] == 5 and l1["decided_by"] == "worker-browser-1"
    assert leads[2]["crm_url"] == "http://crm/#Contact/view/c2" and leads[2]["owner"] == "r.silva"
    assert leads[3]["outcome_detail"] == "Which Lumen is sam@lumen.io?"
    assert leads[4]["outcome_detail"] == "personal email; playbook says skip"
    assert leads[5]["outcome_detail"].startswith("same email as row 1") and leads[5]["flag"] == "duplicate_in_file"

    decisions = {d["title"]: d for d in rep["decisions"]}
    assert decisions["Which Lumen is sam@lumen.io?"]["escalated"] is True
    assert decisions["Which Lumen is sam@lumen.io?"]["result"] == "waiting on you"
    ben = decisions["Personal email: Ben Ortiz"]
    assert ben["confidence"] == 0.91 and ben["threshold"] == 0.8 and ben["result"] == "skip"
    assert not ben["escalated"] and ben["model"] == "good/model:free"  # W3: model on every auto decision

    cov = {c["check"]: c for c in rep["coverage"]}
    assert (cov["crm.contact_exists"]["runs"], cov["crm.contact_exists"]["pass"],
            cov["crm.contact_exists"]["reject"]) == (3, 2, 1)
    assert cov["crm.contact_exists"]["channel"].startswith("EspoCRM REST")
    assert cov["email.sent"]["channel"] == "Mailpit API"

    assert rep["emails"] == [{"lane": "lead:1", "to": "priya@northwind.com", "subject": "Great to meet you",
                              "judge": 0.94, "approval": "-", "delivery": "sent"}]
    agents_rows = {a["agent"]: a for a in rep["agents"]}
    assert agents_rows["worker-browser-1"]["rejections"] == 1 and agents_rows["worker-browser-1"]["retries"] == 1
    assert agents_rows["meta-reviewer"]["model"] == "good/model:free"
    assert agents_rows["meta-reviewer"]["tokens"] == 1200
    assert rep["cost_by_role"] == [{"role": "meta_reviewer", "models": ["good/model:free"], "requests": 1,
                                    "tokens": 1200, "cost_usd": 0.0, "cache_hits": 0}]
    assert rep["totals"]["tokens"] == 1200 and rep["totals"]["requests"] == 1

    assert rep["criteria"][0]["how"] == "crm.no_duplicate" and rep["criteria"][0]["status"] == "verified"
    assert rep["input"]["rows"] == 5 and rep["input"]["quality"][0]["label"] == "duplicate in file"
    rp = rep["reproduce"]
    assert rp["command"] == f"make replay RUN={chaos_run.id}" and rp["input_sha256"] == "ab" * 32
    assert rp["config_hash"] and rp["determinism"] == 0.8 and rp["seed"] == 42
    assert rp["faults"] == ["false_claim", "kill_worker", "expire_session", "model_outage"]
    assert rp["prompts"] == {"meta-reviewer": 0, "verifier": 0, "worker-browser-1": 0, "worker-browser-2": 0}
    assert rep["summary"].startswith("5 rows processed: 1 created, 1 updated, 2 skipped with a reason, 1 waiting.")
    assert "4 injected faults, 4 recovered." in rep["summary"]
    assert [p["label"] for p in rep["phases"]][0] == "crm.create_contact"

    md = rep["markdown"]
    for heading in ("## Success criteria", "## Leads", "## Decisions", "## Faults and recoveries",
                    "## Verification coverage", "## Emails", "## Agents and cost", "## Input file",
                    "## Reproduce this run"):
        assert heading in md
    fault_rows = [line for line in md.splitlines() if line.startswith("| ") and "verifier rejected" in line]
    assert fault_rows and "false_claim" in fault_rows[0]
    assert f"make replay RUN={chaos_run.id}" in md


async def test_report_endpoint_json_and_markdown(ns, r, keys, chaos_run):
    with api_client(ns) as client:
        body = client.get(f"/runs/{chaos_run.id}/report").json()
        assert body["run_id"] == chaos_run.id and len(body["faults"]) == 4
        md = client.get(f"/runs/{chaos_run.id}/report", params={"format": "md"})
        assert md.status_code == 200 and md.headers["content-type"].startswith("text/markdown")
        assert md.text.startswith(f"# Evidence report · {chaos_run.id}")
        accept = client.get(f"/runs/{chaos_run.id}/report", headers={"Accept": "text/markdown"})
        assert accept.text == md.text
        assert client.get("/runs/run_nope/report").status_code == 404


async def test_report_for_empty_run(r, keys):
    run = await ledger.create_run(r, keys, goal="nothing yet", actor="api")
    rep = await report.build_report(r, keys, run.id)
    assert rep["leads"] == [] and rep["faults"] == [] and rep["summary"] == "Run created; no steps planned yet."
    assert "## Leads" in rep["markdown"]


async def test_unrecovered_fault_is_reported_honestly(r, keys):
    run = await ledger.create_run(r, keys, goal="g", actor="api")
    await _ev(r, keys, run.id, EventType.FAULT_INJECTED, {"fault": "false_claim", "phase": "injected"})
    rep = await report.build_report(r, keys, run.id)
    assert rep["faults"][0]["recovery"] == "not recovered yet" and rep["faults"][0]["lost_s"] is None


async def test_summary_counts_only_resolved_decisions(r, keys, chaos_run):
    """Track N: a review that is still open (ready, leased or waiting on you) is never
    counted as made automatically, in JSON, in the summary and in markdown."""
    rep = await report.build_report(r, keys, chaos_run.id)
    assert rep["decision_counts"] == {"total": 2, "auto": 1, "human": 0, "open": 1}
    assert "2 decisions: 1 made automatically, 1 still open." in rep["summary"]
    states = {d["title"]: d["state"] for d in rep["decisions"]}
    assert states == {"Which Lumen is sam@lumen.io?": "open", "Personal email: Ben Ortiz": "auto"}

    # a review the reviewer has not even picked up yet (ready, not escalated) is open too
    pending = _step(chaos_run.id, StepKind.REVIEW_AMBIGUITY, Skill.REVIEW, "lead:2", "review.decided",
                    title="Second look at Marcus Lee")
    await _ready(r, keys, pending)
    rep = await report.build_report(r, keys, chaos_run.id)
    assert rep["decision_counts"] == {"total": 3, "auto": 1, "human": 0, "open": 2}
    assert "3 decisions: 1 made automatically, 2 still open." in rep["summary"]
    assert "made automatically, 2 still open" in rep["markdown"]
    stat = next(s for s in rep["stats"] if s["label"] == "decided automatically")
    assert stat["n"] == "1/3" and stat["sub"] == "0 by you · 2 open"

    # a human answer counts as "by you", not as automatic
    await _work(r, keys, pending, "meta-reviewer", {"decision": "skip", "decided_by": "human",
                                                    "escalation_id": "esc_1", "confidence": 1.0})
    rep = await report.build_report(r, keys, chaos_run.id)
    assert rep["decision_counts"] == {"total": 3, "auto": 1, "human": 1, "open": 1}
    assert "3 decisions: 1 made automatically, 1 by you, 1 still open." in rep["summary"]
