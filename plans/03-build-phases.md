# Build phases (prompts for Claude Code)

Run in order. Each phase ends with acceptance checks; do not continue until
they pass. Commit after each phase.

## Session primer (paste first, once per session)

```
You are building "Ledger", a proof of concept for an autonomous AI operator.
Read plans/00-README.md, plans/01-architecture.md and plans/02-features.md
fully before writing code. They are the spec; if you need to deviate, say why
and update the plan file in the same change.

Ground rules:
- Python 3.12, type hints, pydantic v2, redis-py asyncio, FastAPI, pytest.
- Everything runs in docker compose. Never ask me to install anything on the
  host except Docker.
- No agent framework (no LangChain, CrewAI, AutoGen). The coordination layer
  is the point of this project and must be our own code.
- LLM calls go through services/core/ledger_core/llm.py only, via OpenRouter's
  OpenAI-compatible endpoint, with model names read from env by role.
- Use only OpenRouter `:free` models for now (defaults in
  plans/01-architecture.md section 6). The free tier allows 50 requests/day:
  never call the live API from unit tests, and respect the cache and daily
  budget in section 6b.
- Workers may only move a step to claimed_done. Only the verifier commits.
- Review steps are decided by the meta-reviewer; humans only get escalations.
- Every state change appends an event to ledger:events.
- Write tests alongside code. Tests run with `make test` inside a container
  against a real Redis, not a mock.
- Keep modules small. Prefer boring, readable code over cleverness.
- Work one phase at a time. At the end of a phase, run its acceptance checks
  and show me the output.
```

---

## Phase 0: Skeleton and infrastructure

```
Phase 0. Create the repository skeleton from plans/01-architecture.md section 2.

Deliver:
- docker-compose.yml with redis (AOF), espocrm + mariadb, mailpit, and a
  placeholder `api` service returning /health. Healthchecks and
  depends_on: service_healthy throughout. Named volumes.
- services/core Dockerfile + pyproject (package ledger_core), services/api
  Dockerfile.
- .env.example with every variable, .gitignore, Makefile targets:
  up, down, logs, test, seed, demo, clean.
- services/core seed entrypoint that, idempotently: waits for EspoCRM,
  creates an API user + API key, two sales users (owners), and the contacts
  and accounts in data/crm_seed.json.
- data/event_attendees.csv and data/crm_seed.json per the "Demo data" section
  of plans/02-features.md (the 12-row table is exact; the seed includes the
  users a.chen and r.silva, an open deal on Benjamin Ortiz, and both Lumen
  accounts).
- playbooks/event-leads.md with the sections listed there, written as a
  realistic internal SOP.

Acceptance:
- `make up` from clean brings all services healthy with no manual steps.
- `make seed` twice in a row produces the same CRM state.
- EspoCRM UI reachable on localhost, Mailpit UI reachable, /health returns ok.
```

## Phase 1: Ledger, leases, one worker end to end (P0 core)

```
Phase 1. Implement the coordination substrate. No LLM calls yet.

Deliver in ledger_core:
- protocol.py: pydantic models for Envelope, AgentCard, Step, Event, Fact.
- ledger.py: append_event, create_run, create_step, transition(step, to,
  fence) enforcing the state machine in plans/01-architecture.md section 3,
  commit_fact, get_facts. Illegal transitions and stale fences raise.
- leases.py: acquire (SET NX PX + INCR fence), heartbeat and release as Lua
  scripts with owner check.
- bus.py: enqueue step to queue:{skill}, consume via consumer group.
- worker_base.py: async loop = consume -> acquire lease -> heartbeat task ->
  run handler -> write claim with fence -> release. Registers agent card and
  liveness key. Graceful on SIGTERM, but must also be safe under SIGKILL.
- A reaper coroutine that requeues steps whose lease vanished.
- workers/parser.py as the first real worker: deterministic CSV parsing and
  normalisation (no LLM), flags unusable rows.
- A minimal verifier with the file.parsed_rows check that commits facts.
- A CLI: `python -m ledger_core.cli submit|tail|steps|facts`.

Tests (real Redis):
- every legal and illegal state transition
- two workers racing for one step: exactly one gets the lease
- stale fence write is rejected
- kill a worker task mid-step: reaper requeues, second worker completes
- facts absent before verification, present after

Acceptance: `make test` green; from the CLI I can submit a hand-written
one-step run, watch it go planned -> committed with `tail`, and see facts.
```

## Phase 2: Verifier and postconditions (P0 core)

```
Phase 2. Make "a claim is not a fact" real.

Deliver:
- crm_api.py: EspoCRM REST client using the seeded API key (read-only use).
- postconditions.py: registry with crm.contact_exists, crm.no_duplicate,
  crm.task_exists, file.parsed_rows, email.draft_valid (deterministic part),
  email.sent (Mailpit API), run.criteria_met. Each returns
  {ok, reason, observed}.
- verifier.py as its own service: consumes claimed_done steps, runs the
  check, then verified -> committed with facts, or rejected with reason and
  requeue (attempt + 1), or dead at max_attempts.
- faults.py + the `false_claim` switch: when set, the next handler returns a
  success claim without doing the work.
- A temporary API-based "crm.create_contact" handler (REST) so the pipeline
  can be exercised before the browser worker exists. Check-then-act.

Tests:
- false claim is rejected, retry succeeds, exactly one contact exists
- rejection reason is stored in step.history
- create_contact run twice (simulated takeover) yields one contact

Acceptance: scripted run of parse -> create 3 contacts -> verify completes;
with false_claim on, the timeline shows claimed -> rejected -> claimed ->
verified -> committed.
```

## Phase 3: Orchestrator: understand, plan, replan (P0 core)

```
Phase 3. Add the LLM-driven orchestrator.

Deliver:
- llm.py: complete(role, messages, schema) using OpenRouter with JSON-schema
  structured output, retry, fallback down the role's model list, and a
  model.fallback event. Log tokens and cost to the ledger. Response cache,
  daily request budget and spend cap per plans/01-architecture.md 6b; a
  JSON-in-text fallback for models without response_format.
- Run config (plans/01-architecture.md 6a): pydantic model with playbook
  defaults, stored in run:{id}:config with config_hash.
- playbook.py: load markdown, split by heading, select sections by step kind.
- prompts.py: assemble prompts strictly from ledger state as described in
  plans/01-architecture.md section 7.
- orchestrator.py:
  1. understand: goal + playbook -> success criteria (run.understood)
  2. plan: criteria -> initial steps with postconditions chosen from the
     registry (plan.created). Validate every step against the registry and
     the known skills; re-ask the model on invalid output.
  3. fan-out: when the parse step commits, create per-lead steps
     (search -> create or update -> create task -> draft email).
  4. dependency release: planned -> ready when depends_on are committed.
  5. replan: on dead steps or repeated rejection, revise remaining steps
     (plan.revised).
  6. finish: run.criteria_met sweep, then run.completed or
     run.completed_pending_input.
- The orchestrator never executes work and never names a worker, only skills.

Tests: plan validation rejects unknown checks/skills; fan-out creates the
right number of steps for the demo CSV; replan triggers after N rejections
(use a stubbed LLM for determinism, plus one live smoke test behind an env
flag).

Acceptance: `make demo` with only the one-line goal produces criteria, a
plan, executes it against the CRM through the temporary API handler, and
ends with all criteria verified.
```

## Phase 4: Browser operator

```
Phase 4. Replace the temporary API handler with a real browser operator.

Deliver in services/browser:
- Playwright (Chromium, headless, with optional headed mode + noVNC or
  traces for recording) against EspoCRM.
- Skills as functions: login (session reuse, detect login page and
  re-authenticate), search_contact, create_contact, update_contact,
  create_task. Use roles/labels over CSS where possible.
- Each skill: check-then-act, screenshot before/after into the evidence
  volume, structured observation events (what page, what was seen).
- LLM use is limited to deciding between observed candidates (e.g. fuzzy
  duplicate match) and recovering from unexpected page states by choosing
  among a small set of recovery actions. Navigation itself is coded.
- Run two replicas (worker-browser-1, -2) with separate browser contexts.
- Fault: expire_session clears cookies mid-run.
- Makefile: chaos-kill-browser (docker kill the replica currently holding a
  lease, looked up via the API), chaos-expire-session.

Tests: end-to-end against the compose stack: after the full demo run plus a
kill mid-run, the CRM has the exact expected contact set with no duplicates
and each new contact has one follow-up task.

Acceptance: the demo run completes via the browser; killing the lease holder
mid-run results in takeover and a correct final state.
```

## Phase 5: Humans on the bus, drafter, mailer

```
Phase 5. Meta-reviewer, escalations and email.

Deliver:
- Review steps with skill `review` on queue:review. meta_reviewer.py decides
  them using facts, playbook and read-only CRM REST, returning decision,
  confidence, evidence and options (see 01-architecture section 4).
- Auto-commit when confidence >= REVIEW_AUTO_THRESHOLD (ambiguity) or the
  email judge >= APPROVAL_AUTO_THRESHOLD with no policy flags.
- Below threshold: escalate to queue:human (skill `human`), batched per run.
  Endpoints GET /escalations and POST /escalations/{id} (answer, optional
  save_as_rule that appends to the playbook).
- Escalations do not block unrelated steps. Run status
  completed_pending_input when only escalations remain.
- workers/drafter.py: LLM drafts from committed facts + playbook template
  and tone rules. Verifier: deterministic email.draft_valid, then LLM judge
  on a different model family.
- workers/mailer.py: sends via SMTP to Mailpit only when the approval fact
  is committed. Verified by email.sent.
- CLI commands to list and answer approvals.

Acceptance: the demo run auto-resolves 2 of 3 ambiguous rows and auto-approves
the emails; exactly one escalation (Sam Ito) reaches queue:human; answering it
from the CLI releases that lane; emails appear in Mailpit; nothing is sent
without a committed approval fact (test asserts this).
```

## Phase 6: API, SSE, evidence report

```
Phase 6. The read/write API for the GUI and the final report.

Deliver:
- All endpoints in plans/01-architecture.md section 10, with CORS for the
  web service. SSE endpoint tails ledger:events with Last-Event-ID resume.
- Report builder: JSON + markdown. Sections: goal and criteria with
  verdicts; created, updated, skipped (with reason), awaiting approval;
  per-record CRM link and screenshots; recoveries (retries, takeovers,
  fallbacks, replans); cost by role.
- /chaos endpoints for false_claim, expire_session, model_outage.
- POST /runs/{id}/determinism; GET/PUT /runs/{id}/config (all run controls);
  POST /runs/{id}/replay; GET /llm/budget; GET/PUT /agents/{id}/config
  (prompt versions, injection layers, tools); POST /agents/{id}/restart;
  GET step detail.
- WS /agents/{id}/exec: sandboxed docker exec bridge (non-root user, no host
  mounts, no Docker socket in agent containers).
- OpenAPI schema exported to web/src/api/schema.json.

Acceptance: `curl` can drive a full run; the report for the chaos run lists
each injected fault next to its recovery.
```

## Phase 7: Web GUI

```
Phase 7. Build the GUI in web/ (React + Vite + TypeScript, Tailwind),
served by nginx in its own container, using the designs exported from
Claude Design (see plans/04-gui-design-prompts.md; I will add the exported
files under web/design/).

Screens (see web/design/: Ledger Dashboard, Ledger Canvas):
- Dashboard: Needs your attention (escalations only + handled automatically),
  run strip, Workflow (Agents table, Steps table, Step graph), run controls
  (faults + determinism), criteria, live ledger, recent runs.
- Step detail drawer; agent sheet with Overview / Prompt / Tools / Shell
  (+ Streams, Checks, Content, Inbox where relevant).
- Report: printable evidence report with markdown export.
- Builder: node canvas of agents around the ledger with animated messages.
Dashboard and Builder stay mounted; switching views must not reload.
Data via the generated API client and one SSE connection per run. No mock data
in the final build. Fix the design inconsistencies listed at the end of
plans/04-gui-design-prompts.md. The Builder's Run button submits a real run
(POST /runs); its canvas animation is driven by SSE, not the design's local
simulator.

Acceptance: the whole demo script in plans/05-demo-and-submission.md can be
performed from the browser with the CRM open in a second window.
```

## Phase 8: Hardening and submission

```
Phase 8. Finish.

- Run the full demo five times from clean; fix any flakiness.
- Add the model_outage fault path and verify fallback.
- README with thesis, architecture diagram (mermaid), run instructions, GIF.
- docs/design-note.md following the outline in
  plans/05-demo-and-submission.md, including the honest limits section.
- Final pass: remove dead code, check .env.example, make sure a fresh clone
  works with `make up && make seed && make demo`.
```

## If time is short

| Days available | Build                                                       |
|----------------|-------------------------------------------------------------|
| 2-3            | Phases 0-3, CLI only, API-based CRM handler, false-claim + kill demos |
| 4-5            | Add Phase 5 and 6; browser for create_contact only          |
| 7+             | Everything                                                  |
