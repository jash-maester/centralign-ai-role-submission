"""e2e: A9, ledger persistence across a restart (plans/02 A9).

Mid-run, restart Redis and every ledger service (api, orchestrator, verifier,
meta-reviewer, all workers) — what `docker compose restart` does to the ledger.
EspoCRM, its database and Mailpit stay up: they are the external systems of
record, not the ledger (restarting them tests EspoCRM, not A9). After the
restart the run resumes and finishes with exactly the clean-run outcome: no
event, step or fact from before the restart is lost, committed steps stay
committed, and there are no duplicates.
"""

from __future__ import annotations

import pytest
from e2e_helpers import Api, Crm, Docker, Mailpit, log, wait_for

from test_demo_clean import assert_demo_outcome

pytestmark = pytest.mark.e2e

LEDGER_SERVICES = ("redis", "api", "orchestrator", "verifier", "meta-reviewer", "worker-api", "worker-parser",
                   "worker-drafter", "worker-mailer", "worker-browser-1", "worker-browser-2")


def test_restart_mid_run_resumes_without_loss(api: Api, crm: Crm, mail: Mailpit, docker: Docker, clean):
    run_id = api.submit()

    def mid_run():
        run = api.run(run_id)
        c = run.get("step_counts") or {}
        return run if c.get("committed", 0) >= 12 and c.get("leased", 0) + c.get("ready", 0) > 0 else None

    before = wait_for(mid_run, 300, "the run to be mid-way (>= 12 committed, work in flight)", every=0.5)
    events_before = api.events(run_id)
    steps_before = {s["id"]: s["status"] for s in api.steps(run_id)}
    facts_before = api.facts(run_id)
    log(f"restarting {len(LEDGER_SERVICES)} services at {before['step_counts']}")
    docker.restart(*LEDGER_SERVICES)
    wait_for(api.healthy, 120, "the API to come back")
    api.wait_agents(timeout=180)

    run = api.wait_settled(run_id, timeout=480)
    assert run["status"] == "completed_pending_input", run

    # nothing from before the restart was lost
    events_after = api.events(run_id)
    ids_after = {e["id"] for e in events_after}
    assert all(e["id"] in ids_after for e in events_before), "events lost across the restart"
    assert [e["id"] for e in events_after[:len(events_before)]] == [e["id"] for e in events_before]
    steps_after = {s["id"]: s["status"] for s in api.steps(run_id)}
    assert set(steps_before) <= set(steps_after)
    assert all(steps_after[sid] == "committed" for sid, st in steps_before.items() if st == "committed")
    facts_after = api.facts(run_id)
    assert all(facts_after.get(k) == v for k, v in facts_before.items() if not k.startswith("approval:")), \
        [k for k, v in facts_before.items() if facts_after.get(k) != v]
    # and the run finished exactly like a clean run
    assert_demo_outcome(api, crm, mail, run_id)
    assert len(mail.messages()) == 8  # no email sent twice by a restarted mailer
