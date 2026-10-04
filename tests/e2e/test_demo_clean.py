"""e2e: the clean demo run on the full compose stack (plans/02 "Demo data",
plans/03 Phases 4-5). Scripted LLM; browser operators write the CRM.

From clean: 6 created, 3 updated, 2 skipped with reasons, 1 escalation (Sam
Ito); 8 emails in Mailpit, each backed by a committed approval fact; every new
contact has exactly one follow-up task with the routed owner and due date; the
run ends completed_pending_input. Answering the escalation releases only that
lane and the run completes.
"""

from __future__ import annotations

import pytest
from e2e_helpers import CREATED, EMAIL_ROWS, EMAIL_TO, EXPECTED, LUMEN_INC, TASK_SUBJECT, Api, Crm, Mailpit, log, outcomes

pytestmark = pytest.mark.e2e


def assert_demo_outcome(api: Api, crm: Crm, mail: Mailpit, run_id: str) -> dict:
    """The W3 expected state at completed_pending_input (also used by the A9 test)."""
    run = api.run(run_id)
    assert run["status"] == "completed_pending_input", run["status"]
    rep = api.report(run_id)
    got = outcomes(rep)
    assert got == {n: v[0] for n, v in EXPECTED.items()}, got
    counts = {k: sum(1 for v in got.values() if v == k) for k in ("created", "updated", "skipped", "waiting")}
    assert counts == {"created": 6, "updated": 3, "skipped": 2, "waiting": 1}
    leads = {lead["n"]: lead for lead in rep["leads"]}
    for n in (6, 11):  # skipped with a reason: parser (duplicate in file) and meta-reviewer (phone only)
        assert leads[n]["outcome"] == "skipped" and leads[n]["outcome_detail"], leads[n]
    assert leads[6]["flag"] == "duplicate_in_file" and leads[11]["flag"] == "phone_only"
    assert leads[11]["decided_by"] == "meta-reviewer"
    for n, (_, owner, _) in EXPECTED.items():
        if owner:
            assert leads[n]["owner"] == owner, (n, leads[n]["owner"], owner)

    # exactly one escalation, and it is Sam Ito's account question
    esc = api.escalations(run_id)
    assert len(esc) == 1, [e["question"] for e in esc]
    assert esc[0]["lane"] == "lead:9" and "Sam Ito" in esc[0]["question"]
    assert any(LUMEN_INC in o["label"] for o in esc[0]["options"])

    # 8 emails, to rows 1,2,3,4,5,8,10,12 only (Ben: open deal; Sam: waiting; Jo: no email)
    assert sorted(mail.recipients()) == sorted(EMAIL_TO)
    assert_sent_only_with_approval(api, mail, run_id)

    # every new contact: exactly one contact, one follow-up task, routed owner, due date
    due = next(e for e in api.events(run_id, "run.understood"))["payload"]["event"]["due"]
    users = crm.users()
    for n, (_, owner, email) in CREATED.items():
        hits = crm.contacts_with(email)
        assert len(hits) == 1, (email, hits)
        tasks = [t for t in crm.tasks(hits[0]["id"]) if t["name"] == TASK_SUBJECT]
        assert len(tasks) == 1, (email, tasks)
        task_due = tasks[0].get("dateEndDate") or (tasks[0].get("dateEnd") or "")[:10]
        assert task_due == due, (email, task_due, due)
        assert tasks[0]["assignedUserId"] == users[owner], (email, tasks[0].get("assignedUserName"), owner)
        assert crm.contacts_with(email)[0]["assignedUserId"] == users[owner]
    assert crm.duplicates() == {}
    return rep


def assert_sent_only_with_approval(api: Api, mail: Mailpit, run_id: str) -> None:
    """Nothing is sent without a committed approval fact: every message in Mailpit
    has an approval:<lane> fact (decision approve, same recipient and subject)
    committed before the email.send step was even created."""
    facts = api.fact_records(run_id)
    steps = api.steps(run_id)
    sends = {s["lane"]: s for s in steps if s["kind"] == "email.send"}
    msgs = mail.messages()
    assert msgs, "no email sent"
    for m in msgs:
        to = m["To"][0]["Address"].lower()
        lane = next((s["lane"] for s in sends.values() if (s.get("inputs") or {}).get("to", "").lower() == to
                     or to in str((s.get("claim") or {}).get("data", "")).lower()), None)
        assert lane, f"email to {to} has no email.send step"
        rec = facts.get(f"approval:{lane}")
        assert rec and rec["value"]["decision"] == "approve", (lane, rec)
        assert rec["value"]["to"].lower() == to and rec["value"]["subject"] == m["Subject"], (rec, m["Subject"])
        assert rec["committed_at"] <= sends[lane]["created_at"], (lane, rec["committed_at"], sends[lane]["created_at"])
        assert sends[lane]["status"] == "committed"
    approved = {k.split(":", 1)[1] for k, v in facts.items()
                if k.startswith("approval:lead:") and v["value"].get("decision") == "approve"}
    assert set(sends) <= approved, set(sends) - approved


def answer_sam(api: Api, run_id: str) -> str:
    esc = api.escalations(run_id)
    assert len(esc) == 1 and esc[0]["lane"] == "lead:9"
    opt = next(o for o in esc[0]["options"] if LUMEN_INC in o["label"])
    log(f"answering {esc[0]['id']} with {opt['value']} ({opt['label']})")
    api.answer(esc[0]["id"], opt["value"])
    return opt["value"]


def assert_completed_after_answer(api: Api, crm: Crm, mail: Mailpit, run_id: str) -> None:
    run = api.wait_settled(run_id, timeout=300, statuses=("completed", "failed"))
    assert run["status"] == "completed", run
    assert all(c["status"] == "verified" for c in run["criteria"]), run["criteria"]
    assert api.escalations(run_id) == []
    got = outcomes(api.report(run_id))
    assert got[9] == "created" and sum(v == "created" for v in got.values()) == 7, got
    sam = crm.contacts_with("sam@lumen.io")
    assert len(sam) == 1
    full = crm.http.get(f"/Contact/{sam[0]['id']}").json()
    assert full["accountName"] == LUMEN_INC and full["assignedUserId"] == crm.users()["r.silva"], full
    assert len([t for t in crm.tasks(sam[0]["id"]) if t["name"] == TASK_SUBJECT]) == 1
    # Sam's lane got its own (second, smaller) approval batch; still nothing unapproved
    assert sorted(mail.recipients()) == sorted(EMAIL_TO | {"sam@lumen.io"})
    assert_sent_only_with_approval(api, mail, run_id)
    assert crm.duplicates() == {}


def test_clean_demo_run(api: Api, crm: Crm, mail: Mailpit, clean, shared):
    run_id = api.submit()
    api.wait_settled(run_id)
    assert_demo_outcome(api, crm, mail, run_id)
    assert len(EMAIL_ROWS) == 8

    answer_sam(api, run_id)
    assert_completed_after_answer(api, crm, mail, run_id)
    shared["finished_run"] = run_id
