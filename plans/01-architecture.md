# Architecture

## 1. Service map (docker-compose)

| Service        | Image / build        | Role                                                        |
|----------------|----------------------|-------------------------------------------------------------|
| `redis`        | redis:7 (AOF on)     | Ledger: events, steps, facts, leases, queues                |
| `api`          | build ./services/api | FastAPI: goals, ledger, escalations, SSE, chaos, agent config, shell bridge |
| `orchestrator` | build ./services/core| Understand, plan, route, replan, reap expired leases        |
| `verifier`     | build ./services/core| Check postconditions against the world, commit facts        |
| `meta-reviewer`| build ./services/core| Resolve ambiguity + approval steps; escalate below threshold |
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
│   │       ├── meta_reviewer.py  # decide review steps or escalate
│   │       ├── worker_base.py    # lease loop, heartbeat, claim
│   │       ├── workers/{parser,drafter,mailer}.py
│   │       ├── faults.py         # fault injection switches
│   │       └── agent_config.py   # versioned prompt / tools / determinism per agent
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
| `queue:review`              | Stream  | Steps for the meta-reviewer (ambiguity, approvals)           |
| `agent:{agent_id}:config`   | Hash    | Prompt version, injection layers on/off, tools on/off        |
| `agent:{agent_id}:prompt:{v}`| String | Versioned system prompt text                                 |
| `run:{run_id}:determinism`  | Hash    | level 0..1, seed, derived temperature per role               |
| `run:{run_id}:config`       | Hash    | Run controls (§6a) + `config_hash`; versioned via events     |
| `llm:cache:{hash}`          | String  | Cached structured LLM response (§6b)                         |
| `queue:verify`              | Stream  | Steps in `claimed_done` awaiting the verifier                |
| `escalation:{id}` / `escalations:open` | Hash / Set | Human escalations (replaces v1 `approval:*`)      |
| `config:crm`                | Hash    | Written by `make seed`: scoped CRM API keys, user/account ids |

All keys are built through `ledger_core.keys.Keys(ns)`; a namespace prefix
isolates tests and parallel stacks. Contracts live in `ledger_core/protocol.py`.
| `llm:budget:{yyyy-mm-dd}`   | Counter | Free-model requests used today (§6b)                         |

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
                      +-> review_required -> ready (meta-reviewer, confidence >= threshold)
                      |                   -> input_required -> ready (after human answer)
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
`input.answered`, `review.requested`, `review.resolved`, `review.escalated`,
`approval.auto`, `config.updated`, `prompt.updated`, `tool.toggled`,
`shell.opened`, `model.fallback`, `agent.registered`, `agent.lost`,
`fault.injected`, `run.completed`, `run.completed_pending_input`, `run.failed`,
`run.config_updated`, `llm.cache_hit`, `llm.budget_exhausted`, `spend.cap_reached`,
`step.replanned`, `step.stale_fence` (A4 logging), `llm.call` (tokens/cost per call).

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

### Review decision (meta-reviewer)

Review steps carry skill `review` and arrive on `queue:review`. The
meta-reviewer answers with a `task.artifact` whose data part is:

```json
{
  "decision": "link_account | match_existing | skip | approve | reject",
  "value": "acc/12",
  "confidence": 0.91,
  "threshold": 0.80,
  "evidence": ["REST: 2 accounts named Lumen", "domain lumen.io shared"],
  "options": [{"label": "Lumen Inc", "value": "acc/12"}, {"label": "Lumen Health", "value": "acc/31"}]
}
```

- `confidence >= threshold`: the decision is committed as a fact
  (`review.resolved` / `approval.auto`) and the step goes to `ready`.
- Below threshold: the step moves to `input_required` on `queue:human` with
  `options` and `evidence` (`review.escalated`). The human's answer is
  committed the same way.
- The meta-reviewer reads facts, the playbook and the CRM REST API read-only.
  It never acts on external systems.

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
MODEL_META_REVIEWER=<strong model>,<fallback>
REVIEW_AUTO_THRESHOLD=0.80        # ambiguity
APPROVAL_AUTO_THRESHOLD=0.90      # email judge score
DETERMINISM=0.8                   # 0 = exploratory, 1 = fully deterministic
LLM_DAILY_REQUEST_BUDGET=45       # free tier is 50/day; keep headroom
LLM_CACHE=on                      # on | off | replay-only
```

**Build-phase defaults: free models only.** Until the OpenRouter account is
topped up, every role uses `:free` models. Checked against the live model
list on 2026-10-04 (structured output smoke-tested where marked ✓):

```
MODEL_ORCHESTRATOR=nvidia/nemotron-3-super-120b-a12b:free,qwen/qwen3.8-27b:free
MODEL_META_REVIEWER=nvidia/nemotron-3-super-120b-a12b:free,qwen/qwen3.8-27b:free
MODEL_WORKER=qwen/qwen3.8-27b:free,google/gemma-4-31b-it:free
MODEL_VERIFIER=nvidia/nemotron-3-super-120b-a12b:free,dots-studio/dots-3-note-preview:free
```

- ✓ `nvidia/nemotron-3-super-120b-a12b:free`: json_schema + seed, ~2s.
- ✓ `qwen/qwen3.8-27b:free`: json_schema, no seed param.
- `google/gemma-4-26b-a4b-it:free` returned an upstream 429 (shared pool)
  on first call. Free models are rate-limited and flaky, which exercises the
  fallback path (F4) for real.
- Worker (Qwen/Gemma) and verifier (NVIDIA/dots) stay in different families.
- Only pick models whose `supported_parameters` include `response_format` or
  `structured_outputs`. For a model without it, `llm.py` falls back to
  "JSON in a fenced block + pydantic validate + one re-ask".
- Switching to paid models later is an `.env` change only; the design's mock
  model names (sonnet / haiku / gemini) must come from this config, never
  from hard-coded UI strings.

Determinism is one run-level setting (`POST /runs/{id}/determinism`) that maps
to temperature per role: orchestrator `0.8 × (1 − d)`, workers `1.0 × (1 − d)`,
meta-reviewer `0.5 × (1 − d)`, verifier always `0`. A pinned seed is passed
where the provider supports it. Changes emit `config.updated` and apply from the
next attempt.

`llm.py` exposes `complete(role, messages, schema)` with structured (JSON
schema) output, retries, fallback down the list, and token/cost logging to the
ledger. Pick current model ids from openrouter.ai/models when building.

## 6a. Run configuration (run controls)

The Dashboard's Run controls panel is one versioned config object per run.
Defaults come from the playbook (`Reset to playbook defaults`), then `.env`.
`GET/PUT /runs/{id}/config`; every change emits `run.config_updated` with the
diff and applies from the next attempt (never mid-attempt). `config_hash`
(sha256 of the canonical JSON) is shown in the report's "Reproduce this run".

| Group      | Key                        | Default   | Effect                                                     |
|------------|----------------------------|-----------|------------------------------------------------------------|
| Autonomy   | `review_auto_threshold`    | 0.80      | Meta-reviewer auto-decides ambiguity at or above this      |
| Autonomy   | `approval_auto_threshold`  | 0.90      | Email judge score needed to auto-approve                   |
| Autonomy   | `always_ask_human_email`   | false     | true: every external email escalates (meta-reviewer can't approve) |
| Autonomy   | `llm_judge_enabled`        | true      | D4 soft checks; false = deterministic checks only          |
| Reliability| `lease_ttl_s`              | 15        | Lease TTL; heartbeat = TTL/3                               |
| Reliability| `max_attempts`             | 3         | Per step, before `dead`                                    |
| Reliability| `check_then_act`           | true      | Off only to demonstrate duplicates on takeover             |
| Reliability| `replan_after_rejections`  | 2         | B5; 0 disables replanning                                  |
| Reliability| `model_fallback`           | true      | Walk the role's model list on error                        |
| Execution  | `determinism`, `seed`, `seed_pinned` | 0.80, 42, true | §6 temperature mapping                      |
| Execution  | `browser_concurrency`      | 2         | Number of browser operators consuming `queue:browser.espocrm` (scale via compose) |
| Execution  | `crm_write_path`           | `browser` | `browser` / `auto` (UI, API on blocked) / `api`. Verifier always reads REST |
| Safety     | `spend_cap_usd`            | 2.00      | Hard stop: no new LLM calls once reached, emit `spend.cap_reached`, run → `failed` with reason. Free models count $0 but still count requests |
| Safety     | `fuzzy_match_threshold`    | 0.85      | Name+company similarity needed to *propose* a match to the meta-reviewer |
| Safety     | `dry_run`                  | false     | Mailer verifies and logs but does not SMTP-send            |

Replay: `make replay RUN=<id> [SEED=] [DETERMINISM=]` creates a new run with
the same goal, input file hash, playbook version, prompt versions and config.
With `LLM_CACHE=replay-only` every LLM call must hit the cache (§6b), so a
replay costs zero requests and produces the identical plan.

## 6b. LLM request budget and cache

The free tier gives 50 free-model requests per day, and a full demo run needs
roughly 25-30 LLM calls (understand, plan, 3 ambiguity reviews, 1 email
approval, 8 drafts, 8 judge calls, a few browser decisions). So:

- **Cache.** Key = sha256(role, model, messages, schema, temperature, seed).
  Stored as files under `LLM_CACHE_DIR` (bind mount `./.cache/llm`, survives
  `make clean`). Prompts never contain run/step ids or timestamps, so the same
  inputs produce the same key across runs.
  At determinism 1.0 the cache makes "same inputs → same plan" true even when
  the provider ignores `seed`. Cache hits emit `llm.cache_hit` and count no
  budget. Tests and `make demo` reuse the cache; `LLM_CACHE=off` forces live
  calls.
- **Budget.** `llm.py` increments `llm:budget:{date}` before each live call
  and refuses (`llm.budget_exhausted`, treated like a model outage, no
  fallback) once `LLM_DAILY_REQUEST_BUDGET` is used.
- **Fewer calls.** Fan-out and dependency release are deterministic code, not
  LLM calls. The email judge scores all drafts of a run in one call. Unit
  tests use the stub LLM; only the env-flagged smoke test calls OpenRouter.
- 429s from free models are retried once with backoff, then fall back.
- **Implementation notes (Track D).** `LLM_BACKEND=openrouter|scripted|stub`
  picks the backend (`llm.complete()` installs it lazily); `scripted` answers
  from `tests/fixtures/llm/*.json` for offline runs (convention in that
  folder's README). Every HTTP request (including the one retry and the one
  re-ask) increments the budget and emits `llm.call` (`ok`, tokens, cost,
  latency); a cache hit emits `llm.cache_hit` instead. Per-run spend and
  request counts (also per role) live in the hash `llm:spend:{run_id}`.
  `LLM_CACHE=off` skips cache reads but still writes, so a forced live run
  refreshes the cache for later replays. The `model_outage` fault (F4)
  replaces the primary model of the **worker** role only, matching
  `make chaos-model-outage`; a numeric value is a shot count. `llm.last_call()`
  exposes the model that answered (for `Verdict.model` / `ReviewDecision.model`).

## 7. Prompt assembly (dynamic prompting)

A worker prompt is rebuilt on every attempt from ledger state only:

1. Role + skill instructions (static).
2. Relevant playbook sections (selected by step kind).
3. The step: kind, inputs resolved from committed facts, postcondition.
4. Prior attempts on this step: observations and verifier rejection reasons.
5. Output schema.

Never included: other agents' messages, uncommitted claims, full run history.

Layers are configurable per agent (`agent:{id}:config`): the system prompt is
editable and versioned (`prompt.updated`), and layers 2 and 5 can be switched
off for experiments. Layer 4 (prior attempts + rejection reasons) is locked on,
because D3 depends on it.
Layer ids (agent config, GUI): `role`, `playbook`, `step`, `history`, `schema`.
`prompts.assemble()` reports approximate tokens per layer (chars / 4) and
accepts a stable `fixture_key` hint for the scripted backend.

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
| `crm.lookup_matches`       | Search step: the claimed match (or no match) agrees with a REST query |
| `review.decided`           | A decision fact exists: meta-reviewer at/above threshold, or a human answer |
| `run.criteria_met`         | Final sweep of all success criteria                  |

CRM credentials are scoped at the CRM itself (seeded by `make seed`): the
verifier and meta-reviewer use the `ledger-verifier` API user with a
**read-only** role; only the `api.espocrm` fallback skill gets the
`ledger-writer` key; the browser operator logs in as `ledger.operator`.

Key property: the operator acts through the **browser**; the verifier reads
through the **REST API**. Action and verification use different channels, so
one broken channel cannot both act wrongly and report success.

Deterministic checks always run first. The LLM judge is used only where no
hard check exists, and its verdict is recorded with its reasoning.

## 9. Review and human in the loop (by exception)

- Policy comes from the playbook: e.g. "external emails require approval",
  "never merge contacts without confirmation". Policy now creates a **review
  step**, not a human task.
- The meta-reviewer decides review steps automatically when confidence clears
  the threshold (`REVIEW_AUTO_THRESHOLD`, `APPROVAL_AUTO_THRESHOLD`). Email
  approval uses the LLM judge score plus deterministic policy checks.
- Only below threshold does a step escalate to `queue:human`. Escalations are
  batched per run and include the options and what the reviewer tried.
- Humans are an agent with skill `human`. The GUI is the human's agent
  client. An answer can be saved as a playbook rule so the same case resolves
  automatically next time.
- Open escalations do not block unrelated steps; the run finishes as
  `completed_pending_input` with the report listing what waits. (This is the
  only name for that status; v1's `completed_pending_approval` is retired.)

## 9a. Agent operations

- **Config:** `GET/PUT /agents/{id}/config` for prompt text (versioned),
  injection layers and tool switches. Every change is an event.
- **Tools:** each agent card lists its tools by type: REST API, MCP server
  (stdio or HTTP), local function, browser, SMTP, LLM. Workers never get the
  CRM REST tool; that channel belongs to the verifier and meta-reviewer.
- **Shell:** `WS /agents/{id}/exec` bridges to `docker exec` in that agent's
  container. Sandboxed: container-scoped user, no host mounts, no Docker socket,
  egress limited to the compose network. Opening a shell emits `shell.opened`.

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
| POST   | `/chaos/{fault}`                   | Inject a fault                  |
| GET    | `/evidence/{path}`                 | Screenshots                     |
| GET    | `/escalations`                     | Open human escalations          |
| POST   | `/escalations/{id}`                | Answer (+ optional save_as_rule)|
| GET    | `/runs/{id}/steps/{step_id}`       | Step detail with attempt history|
| POST   | `/runs/{id}/determinism`           | Set level 0..1, seed            |
| GET/PUT| `/runs/{id}/config`                | Run controls (§6a)              |
| POST   | `/runs/{id}/replay`                | New run from the same inputs + config |
| GET    | `/llm/budget`                      | Requests used / remaining today |
| GET/PUT| `/agents/{id}/config`              | Prompt, injections, tools       |
| POST   | `/agents/{id}/restart`             | Restart an agent's container    |
| WS     | `/agents/{id}/exec`                | Sandboxed shell into container  |
