# Architecture

## 1. Service map (docker-compose)

| Service        | Image / build        | Role                                                        |
|----------------|----------------------|-------------------------------------------------------------|
| `redis`        | redis:7 (AOF on)     | Ledger: events, steps, facts, leases, queues                |
| `api`          | build ./services/api | FastAPI: submit goals, read ledger, approvals, SSE, chaos   |
| `orchestrator` | build ./services/core| Understand, plan, route, replan, reap expired leases        |
| `verifier`     | build ./services/core| Check postconditions against the world, commit facts        |
| `worker-parser`| build ./services/core| Parse CSV/PDF attendee files into structured rows           |
| `worker-browser`| build ./services/browser | Playwright operator against the CRM UI (scale to 2)    |
| `worker-drafter`| build ./services/core| Draft follow-up emails per playbook                         |
| `worker-mailer`| build ./services/core| Send approved emails via SMTP                               |
| `espocrm`      | espocrm/espocrm      | The "company system" being operated                         |
| `espocrm-db`   | mariadb              | CRM database                                                |
| `mailpit`      | axllent/mailpit      | SMTP sink with web inbox                                    |
| `web`          | build ./web          | React GUI                                                   |
| `seed`         | build ./services/core| One-shot: seed CRM with existing contacts, users, API key   |

All core services share one Python package (`ledger_core`) and differ only by
entrypoint. `worker-browser` has its own image because of Playwright's size.

Compose requirements:
- healthchecks on redis, espocrm, api; `depends_on: condition: service_healthy`
- named volumes: `redis-data`, `espocrm-data`, `db-data`, `evidence` (screenshots)
- `.env` for `OPENROUTER_API_KEY`, model names, CRM credentials
- `profiles: [chaos]` for nothing; chaos is driven through the API and `make`

## 2. Repository layout

```
.
├── docker-compose.yml
├── Makefile                  # up, down, seed, demo, chaos-*, test, logs
├── .env.example
├── playbooks/
│   └── event-leads.md        # company context for the demo workflow
├── data/
│   ├── event_attendees.csv   # messy input
│   └── crm_seed.json         # pre-existing contacts (to create duplicates)
├── services/
│   ├── core/
│   │   ├── Dockerfile
│   │   ├── pyproject.toml
│   │   └── ledger_core/
│   │       ├── ledger.py         # event append, step CRUD, facts
│   │       ├── leases.py         # acquire, heartbeat, release, fencing
│   │       ├── protocol.py       # envelope + agent card models (pydantic)
│   │       ├── bus.py            # stream publish/consume helpers
│   │       ├── llm.py            # OpenRouter client, role->model, fallback
│   │       ├── prompts.py        # prompt assembly from ledger state
│   │       ├── playbook.py       # load + section the playbook
│   │       ├── postconditions.py # checker registry
│   │       ├── crm_api.py        # EspoCRM REST client (verifier only)
│   │       ├── orchestrator.py
│   │       ├── verifier.py
│   │       ├── worker_base.py    # lease loop, heartbeat, claim
│   │       ├── workers/{parser,drafter,mailer}.py
│   │       └── faults.py         # fault injection switches
│   ├── browser/
│   │   ├── Dockerfile
│   │   └── browser_worker/       # Playwright skills for EspoCRM
│   └── api/
│       ├── Dockerfile
│       └── app/                  # FastAPI routes, SSE
├── web/                          # React + Vite
├── tests/
└── docs/
    └── design-note.md
```

## 3. Ledger schema (Redis)

| Key                         | Type    | Contents                                                     |
|-----------------------------|---------|--------------------------------------------------------------|
| `ledger:events`             | Stream  | Append-only log of everything. Never mutated.                |
| `run:{run_id}`              | Hash    | goal, status, criteria (JSON), playbook_hash, created_at     |
| `run:{run_id}:steps`        | List    | Ordered step ids                                             |
| `step:{step_id}`            | Hash    | See step record below                                        |
| `facts:{run_id}`            | Hash    | Committed facts only. key -> JSON value + source step        |
| `lease:{step_id}`           | String  | worker_id, set with `NX PX <ttl>`                            |
| `fence:{step_id}`           | Counter | Monotonic fencing token, `INCR` on every lease               |
| `queue:{skill}`             | Stream  | Step ids ready for a skill; consumer group per skill         |
| `agents`                    | Hash    | agent_id -> agent card JSON                                  |
| `agent:{agent_id}:alive`    | String  | TTL heartbeat for liveness in the GUI                        |
| `approval:{approval_id}`    | Hash    | run, step, question, options, status, answer                 |
| `approvals:pending`         | Set     | Open approval ids                                            |
| `faults`                    | Hash    | Fault injection switches read by workers                     |

### Step record

```json
{
  "id": "stp_...",
  "run_id": "run_...",
  "kind": "crm.create_contact",
  "skill": "browser.espocrm",
  "inputs": {"lead_ref": "fact:lead:7"},
  "postcondition": {"check": "crm.contact_exists", "args": {"email": "a@b.com"},
                    "expect": {"account": "Acme", "assigned_user": "priya"}},
  "depends_on": ["stp_..."],
  "side_effect": true,
  "idempotency_key": "create_contact:a@b.com",
  "status": "planned",
  "attempt": 0,
  "max_attempts": 3,
  "fence": 0,
  "lease_owner": null,
  "claim": null,
  "verdict": null,
  "history": []
}
```

### Step state machine

```
planned -> ready -> leased -> claimed_done -> verified -> committed
                      |            |
                      |            +-> rejected -> ready (retry, with observation)
                      |                         -> replanned (superseded by new steps)
                      +-> lease_expired -> ready
                      +-> input_required -> ready (after human answer)
any -> dead (max attempts, escalated to human)
```

Rules that make it correct:
- Only the verifier moves a step to `verified`/`committed`. Workers can only
  reach `claimed_done`.
- Facts are written only at `committed`. Workers' prompts are built from facts,
  so an unverified claim can never poison another agent's context.
- Every write from a worker carries its fencing token. The ledger rejects any
  write whose token is lower than `fence:{step_id}`. A worker that was paused
  and wakes up after its lease expired cannot corrupt state.
- Side-effecting steps are **check-then-act**: the worker first checks whether
  the postcondition already holds (e.g. contact already exists) and claims done
  without acting if so. This is what makes takeover idempotent.

### Event types (`ledger:events`)

`run.created`, `run.understood`, `plan.created`, `plan.revised`,
`step.ready`, `step.leased`, `step.heartbeat_lost`, `step.lease_expired`,
`step.observation`, `step.claimed`, `step.verified`, `step.rejected`,
`step.committed`, `step.dead`, `fact.committed`, `input.requested`,
`input.answered`, `model.fallback`, `agent.registered`, `agent.lost`,
`fault.injected`, `run.completed`, `run.failed`.

Every event: `{id, ts, run_id, step_id?, actor, type, payload}`.

## 4. Protocol (A2A-shaped subset)

Transport is Redis Streams instead of HTTP/JSON-RPC. The shapes map onto A2A so
a real A2A HTTP facade could be added without touching agents.

### Agent card

```json
{
  "id": "worker-browser-1",
  "name": "EspoCRM browser operator",
  "skills": [
    {"id": "browser.espocrm", "kinds": ["crm.search_contact", "crm.create_contact",
      "crm.update_contact", "crm.create_task"]}
  ],
  "model_role": "worker",
  "side_effects": true
}
```

### Envelope

```json
{
  "id": "msg_...",
  "run_id": "run_...",
  "task_id": "stp_...",
  "from": "orchestrator",
  "to_skill": "browser.espocrm",
  "type": "task.submit | task.status | task.artifact | task.cancel",
  "state": "submitted | working | input-required | completed | failed",
  "fence": 3,
  "parts": [{"kind": "data", "data": {}}, {"kind": "file", "uri": "evidence/..png"}],
  "ts": "..."
}
```

A2A mapping: step = Task, claim payload = Artifact, `input_required` =
`input-required`, agent card = Agent Card. Not implemented: streaming over SSE
between agents, push notifications, auth schemes, discovery over HTTP.

## 5. Leases and self-healing

- Acquire: `SET lease:{step} {worker} NX PX 15000`, then `INCR fence:{step}`.
- Heartbeat every 5s: Lua script, extend only if value == worker_id.
- Release on claim: Lua compare-and-delete.
- Reaper (in orchestrator, every 2s): any step in `leased` whose lease key is
  gone -> emit `step.lease_expired`, set `ready`, requeue.
- Four recovery layers, each visible as distinct events:
  1. **Retry**: verifier rejection -> same skill, prompt includes the rejection
     reason and last observation.
  2. **Replan**: after N rejections or a `blocked` observation, orchestrator
     rewrites the remaining plan (e.g. route `crm.create_contact` to an API
     skill when the UI path fails).
  3. **Takeover**: worker death -> lease expiry -> another worker continues.
  4. **Model fallback**: LLM error or repeated rejection -> next model in the
     role's list, emit `model.fallback`.

## 6. Models (OpenRouter)

Configured by role in `.env`, never hard-coded:

```
MODEL_ORCHESTRATOR=<strong model>,<fallback>
MODEL_WORKER=<cheap fast model>,<fallback>
MODEL_VERIFIER=<strong model from a DIFFERENT family than MODEL_WORKER>
```

`llm.py` exposes `complete(role, messages, schema)` with structured (JSON
schema) output, retries, fallback down the list, and token/cost logging to the
ledger. Pick current model ids from openrouter.ai/models when building.

## 7. Prompt assembly (dynamic prompting)

A worker prompt is rebuilt on every attempt from ledger state only:

1. Role + skill instructions (static).
2. Relevant playbook sections (selected by step kind).
3. The step: kind, inputs resolved from committed facts, postcondition.
4. Prior attempts on this step: observations and verifier rejection reasons.
5. Output schema.

Never included: other agents' messages, uncommitted claims, full run history.

## 8. Verification

`postconditions.py` is a registry of named checks:

| Check                      | How it verifies                                      |
|----------------------------|------------------------------------------------------|
| `file.parsed_rows`         | Row count and required columns vs. source file       |
| `crm.contact_exists`       | EspoCRM REST query by email; compare expected fields |
| `crm.no_duplicate`         | Exactly one contact per normalised email             |
| `crm.task_exists`          | Follow-up task linked to contact, due per playbook   |
| `email.draft_valid`        | Deterministic: recipient, merge fields, no placeholders; then LLM judge vs. playbook tone rules |
| `email.sent`               | Mailpit API shows message to recipient               |
| `run.criteria_met`         | Final sweep of all success criteria                  |

Key property: the operator acts through the **browser**; the verifier reads
through the **REST API**. Action and verification use different channels, so
one broken channel cannot both act wrongly and report success.

Deterministic checks always run first. The LLM judge is used only where no
hard check exists, and its verdict is recorded with its reasoning.

## 9. Human in the loop

- Policy comes from the playbook: e.g. "external emails require approval",
  "never merge contacts without confirmation".
- Orchestrator batches all ambiguous rows into **one** question per run.
- Approvals are steps with skill `human`. The GUI is the human's agent client.
- Unanswered approvals do not block unrelated steps; the run finishes as
  `completed_pending_approval` with the report listing what waits.

## 10. API surface (FastAPI)

| Method | Path                               | Purpose                         |
|--------|------------------------------------|---------------------------------|
| POST   | `/runs`                            | Submit goal (+ file reference)  |
| GET    | `/runs`, `/runs/{id}`              | Run summary, criteria, status   |
| GET    | `/runs/{id}/steps`                 | Step graph with statuses        |
| GET    | `/runs/{id}/events`                | Event history (paginated)       |
| GET    | `/runs/{id}/facts`                 | Committed facts                 |
| GET    | `/runs/{id}/report`                | Evidence report (JSON + md)     |
| GET    | `/stream?run_id=`                  | SSE of ledger events            |
| GET    | `/agents`                          | Cards, liveness, current lease  |
| GET    | `/approvals`                       | Pending approvals               |
| POST   | `/approvals/{id}`                  | Answer / approve / reject       |
| POST   | `/chaos/{fault}`                   | Inject a fault                  |
| GET    | `/evidence/{path}`                 | Screenshots                     |
