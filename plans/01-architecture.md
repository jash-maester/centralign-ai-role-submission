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
`run.status` (non-terminal run status change, payload `{from, to}`; added in W1, the GUI reducer maps `to` onto the run).

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

### Implementation notes (Track A, ledger core)

- `steps:leased` (Set, `Keys.leased_steps`) indexes steps in `leased` so the
  reaper never scans every run; `ledger.transition` maintains it atomically.
- Non-terminal run status changes emit `run.status` (`{from, to}`); terminal
  ones keep `run.completed` / `run.completed_pending_input` / `run.failed`.
- A step whose lease expired `max_attempts` times goes to `dead` (a step that
  crashes every worker cannot loop forever). Rejections count separately.
- `false_claim` may be scoped to one skill: field `false_claim:<skill>` in
  `faults` is consumed before the global `false_claim` field.
- The verifier's rejected -> ready/dead/hold choice is a pluggable policy
  (`Verifier(reject_policy=...)`) so the orchestrator can hold steps for B5.
- Bus entries the dead consumer never acked are XAUTOCLAIMed after 30s idle;
  harmless because the lease + state machine decide who works on a step.

### W1 integration notes

- `web/src/api/protocol.schema.json` is regenerated with `make schema` whenever `protocol.py` changes; `web/src/api/types.ts` `EVENT_TYPES` must list the same members (checked by `make web-check`).
- `make test-browser` collects the whole `tests/` tree inside the browser image, so test modules that need test-image-only libraries (e.g. `respx`) use `pytest.importorskip` instead of a bare import.
- Contact `title` is stored by EspoCRM on the account link (`AccountContact.role`); contacts created without an account have no title, so planner expects must not include `title` for account-less leads (both the REST and browser skills behave this way).

### Implementation notes (Track G, orchestrator)

- `orchestrator.py` reconciles each run idempotently from ledger state (events
  trigger a pass; every 5 s all active runs are swept; the reaper runs inside).
  `submit_goal()` creates the run and its RunConfig (playbook + overrides).
- Understand/plan are the only LLM calls (`orchestrator_llm.py`, schemas
  `Understanding` / `Plan`, re-asked up to 3 times on invalid output). The
  initial plan may contain only run-level steps (file.parse); per-lead lanes
  come from deterministic fan-out (`orchestrator_lanes.py`).
- Lanes are `lead:<row>`; stages: search -> (matched: update | none + owner:
  create | ambiguous / no owner: review.ambiguity) -> task -> tail stages
  registered with `register_tail()` (W3 email). Phone-only rows get a
  review.ambiguity (reason `phone_only`) at fan-out.
- A planned step may hold `bind:<selector>.<field>` values (e.g.
  `bind:contact.contact_id`), resolved at release from the lane's committed
  step (verdict.observed first, then claim data). Workers never see them.
- Review decisions continue a lane when the review step is committed and a
  decision is readable from fact `review:<lane>` (or the review step's claim
  data): `{decision: skip | match_existing | create_new | link_account, value}`.
- Replan (B5): `orchestrator_replan.reject_policy` holds a CRM step in
  `rejected` once `replan_after_rejections` is reached and the route
  (`crm_write_path=auto`) has another skill; the lane's remaining steps are
  replanned onto it (plan.revised with `replaces`). Verifier services should
  pass `reject_policy=` this function.
- Finish: when nothing moves without a review/human, `checks/run.py` sweeps
  every criterion per lane (REST via the verifier's read-only key, facts);
  terminal events carry `criteria`, `lanes` and `steps` counts. Email
  criteria stay `pending` until email lanes exist.
- The run hash `run:{id}` gains a field `event` (`{name, date, due,
  task_subject}`), from the input's `<name>.meta.json` (null date = the day
  before the run).

### Implementation notes (Track I, browser worker)

- `browser_worker/worker.py`: `BrowserHandler` (worker_base handler for `browser.espocrm`) + `browser_card`. Inputs accept the planner / api.espocrm shape (`lead{...}`, `owner`, `contact_id`, `subject|event_name`, `due|event_date`); claim data uses the api.espocrm keys (`contact_id`, `action` created|exists, `task_id`, `result` matched|ambiguous|none, `candidates`) plus `channel: "browser"`. Screenshots go to `claim.evidence`; every skill observation becomes a `step.observation`.
- Browser `crm.create_contact` needs `owner` in the inputs (the browser does not route; take it from the search step) and only links an Account that already exists (exact name), like the REST path.
- A skill that gives up files a claim with `data.blocked=true, acted=false, fallback_skill="api.espocrm"`; the verifier decides (reject -> retry / replan).
- Faults: `expire_session` and `ui_changed` (scoped `<fault>:browser.espocrm` first) are one-shot per step even when set to "on". `RunConfig.check_then_act=false` skips the existence check in create_contact / create_task.
- LLM: only the recovery chooser (`BROWSER_LLM_RECOVERY=on`, role worker, schema-bounded to reload|relogin|home|give_up, falls back to the default policy on any error). Off by default to save the shared budget.
- Demo knob `LEDGER_BROWSER_PAUSE="<checkpoint>:<seconds>[:<kind>]"` (e.g. `after_save:45:crm.create_contact`) holds a step so `make chaos-kill-browser` lands mid-step. `chaos-kill-browser` / `chaos-expire-session` fall back to Redis when the API is not up, and kill only the replica container (`docker kill $(compose ps -q ...)`), never one-off `run` containers.

### Integration notes (wave W2 gate)

- Hand-written plans (CLI `submit` spec, `POST /runs` with `steps`) are created
  with status `running` and no criteria. The orchestrator treats "running
  without criteria" as hand-planned: no understand/plan (LLM), no fan-out or
  replan; it only closes the run (`completed` when every step is committed,
  `failed` on a dead step). Before this, the live orchestrator spent an LLM
  request understanding every hand-written run.
- Run pinning: `submit_goal(..., orchestrator=<agent_id>)` writes hash field
  `run:{id}.orchestrator` before `run.created`; any other orchestrator skips
  the run, and one built with `owned_only=True` handles only its pinned runs.
  `make demo LLM_BACKEND=...` (explicit backend) starts its own in-process
  orchestrator and pins the run to it, so a scripted demo no longer reaches
  the live service's OpenRouter backend.
- Lane lookup (`orchestrator_lanes.lookup_for`) = the search step's claim data
  overlaid with the verifier-committed lane facts (`<lane>.routing`,
  `<lane>.lookup`, `<lane>.owner`, `<lane>.open_deal`). Track F's `lookup`
  fact holds only `{result, match_type, contact_id, candidates}`.
- `crm.lookup_matches` also observes the playbook routing from REST
  (`observed.routing` = owner, owner_reason, region, account_id, account_name,
  account_candidates; `observed.open_deal` for a matched contact).
  `checks/crm_facts.py` prefers it over the claim. The browser search claim
  carries no routing, so without this every unmatched browser lane went to
  review as `unknown_region` and follow-up tasks were owned by the operator.

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
  **Track N:** the source of truth is now OpenRouter's own count
  (`llm_budget.py`: `GET /api/v1/key` → `free_model_daily_requests`
  {used, limit, remaining}, `limit_remaining` for paid models; cached 60 s,
  plus the requests made since the fetch). A live call is refused once
  `remaining <= LLM_BUDGET_RESERVE` (default 3). Without a key, or when the
  check fails, a persistent per-UTC-day file counter under `LLM_CACHE_DIR/budget/`
  (survives `make clean`) is enforced against `LLM_DAILY_REQUEST_BUDGET`. Every
  live request increments that file; the Redis `llm:budget:{date}` key is now
  only the stack's informational count. `GET /llm/budget` returns the same
  numbers (`source`, `used`, `limit`, `remaining`, `reserve`, `usable`,
  `local`, `ledger_used`, `live`), never the key.
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
Both API roles may read the user list (owner lookup by userName); the writer
role also has `assignmentPermission: all` so it can assign contacts and tasks
to their routed owner. A search step's claimed result is `matched` (exact
normalised email), `ambiguous` (one or more probable fuzzy matches, which go to
review) or `none`.

Key property: the operator acts through the **browser**; the verifier reads
through the **REST API**. Action and verification use different channels, so
one broken channel cannot both act wrongly and report success.

Deterministic checks always run first. The LLM judge is used only where no
hard check exists, and its verdict is recorded with its reasoning.

### Implementation notes (Track F, verifier + faults + API worker)

- `verifier.make_context_factory()` gives checks `ctx.crm` (CrmReader, read-only
  `ledger-verifier` key from `config:crm`) and `ctx.mailpit`; the default context
  also carries `extra` r/keys/config/playbook/rejections.
- `judge.make_judge()` (role `verifier`) runs only for `email.draft_valid` and only
  after the deterministic check passed, when `llm_judge_enabled`. Pass = score
  >= 0.6 and no flags; score/flags/reasoning go to `observed.judge`, the model to
  `Verdict.model`. LLM unavailable -> deterministic verdict stands, `judge.skipped`.
  `judge.judge_drafts()` scores a batch in one call (approval).
- `faults.py` is the only reader/writer of `faults`: set/consume/clear for every
  FaultName, each set/clear emits `fault.injected` (phase set|cleared, by, what),
  consumers emit phase=consumed. Scope lookup: `<fault>:<agent_id>`,
  `<fault>:<skill>`, then `<fault>`. `python -m ledger_core.faults set ...` arms one.
- `worker-api` (skill `api.espocrm`) runs Track B's REST handlers; SkillInputError
  becomes a `blocked` observation (retryable=false) and a not-acted claim.
- CRM facts per lead (`lead:<n>` from step.lane): `.lookup`, `.contact_id`,
  `.action`, `.owner`, `.crm_url`, `.routing`, `.open_deal`, `.task_id`,
  `.task_due`, `.task_owner`, `.task_url` (REST-observed values win over claims).

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

### Implementation notes (Track J, meta-reviewer + escalations)

- `meta_reviewer.py` (`MetaReviewer`, a worker_base.Worker on skill `review`;
  service `python -m ledger_core.services.meta_reviewer`). review.ambiguity:
  deterministic read-only evidence (CrmReader verifier key: candidates' phones,
  emails, shared domains, account websites/countries, open deals, task owners;
  playbook escalation rules) -> a saved playbook rule for the same case decides
  at confidence 1.0 without an LLM call -> else `llm.complete("meta_reviewer",
  ..., ReviewDecision)` with `fixture_key=<lane>`. Guards: the answer must map to
  one of the step's options (by value, id or label); `ambiguous_account` with
  nothing distinguishing the accounts always escalates (playbook rule). LLM
  errors escalate (never guessed).
- At/above `review_auto_threshold`: fact `review:<lane>` (decision, value,
  confidence, threshold, evidence, decided_by, model, option, label, source),
  `review.resolved`, then a claim (`acted=false`) that the verifier commits via
  `review.decided` (checks/review.py: fact exists and agrees with the claim,
  option is valid, confidence >= the run's threshold and no forced reason, or a
  human answer recorded on an answered escalation of this step).
- Below: `escalations.escalate()` moves the step leased -> review_required ->
  input_required in one transaction (role `meta_reviewer`, fenced), stores
  `escalation:{id}` (+ `escalations:open`), puts a task envelope on
  `queue:human` and emits `review.escalated` (payload: question, options,
  confidence, threshold, tried/evidence, proposed decision, forced reason).
- Answers (`POST /escalations/{id}` `{answer, save_as_rule, by, note}`; CLI
  `approvals list|answer ESC ANSWER [--save-as-rule] [--wait|--local]`):
  answer = an option value or label; commits the decision fact (decided_by
  human), emits `input.answered`, moves the step input_required -> ready with
  `inputs.human_decision`; the meta-reviewer claims it without an LLM call and
  the verifier commits it, which releases only that lane. `save_as_rule`
  appends `- <text> [rule reason=<r> company=<c> domain=<d> -> <option>]` to
  "Escalation rules" (playbook.append_rule, version bump, `config.updated`
  scope=playbook).
- review.approval (Track K): `inputs.drafts=[{id, to, subject, body,
  checks_ok?, check_reason?}]` (or `draft`), decision key from
  `postcondition.args.decision_key` (default `approval:<lane>`). Auto-approve
  only if every deterministic check passed, `judge.judge_drafts` (one call) min
  score >= `approval_auto_threshold` with no flags, `llm_judge_enabled` and not
  `always_ask_human_email` -> fact + `approval.auto` (record has `scores`,
  `drafts`); else escalate with options approve / reject.
- `orchestrator_lanes.lookup_for` merges the search claim data, Track F's
  `<lane>.lookup` and `<lane>.routing` facts (the lookup fact alone has no
  owner, which sent every routed lead to an unknown_region review).
- `cli demo` / `LocalAgents` start an in-process meta-reviewer when none is alive.

### Implementation notes (Track K, drafter + approval batch + mailer)

- Email lanes are a run-level orchestrator stage (`orchestrator_email.py`,
  registered with the additive `orchestrator_lanes.register_stage()` hook; the
  orchestrator calls every stage each progress round as
  `await stage(orch, run, steps, facts, cfg) -> bool`). No LLM in the stage.
- Draft: when a lane's contact + task are committed, one `email.draft` step
  (skill email.draft, depends on the task). Excluded per playbook: no email,
  **open deal** (from the search when matched by email, else one read-only REST
  call `open_opportunities_for_contact` for contacts matched via review /
  check-then-act; a contact this run created has none), skipped/unresolved rows
  (never reach a committed contact). Exclusions: run hash field `email`
  (`{"excluded": {lane: reason}}`) + a `plan.revised` event with `excluded`.
  Owner full name from the playbook's `` `user` (Full Name) `` routing lines
  (fallback CRM user list); sender `<owner>@ledger-demo.test`.
- Verifier: `email.draft_valid` (deterministic, then the LLM judge, Track F).
  Facts: `lead:<n>.draft` (to, subject, body, owner, owner_name, from_email,
  draft_step, judge) and, after a send, `lead:<n>.email` (`checks/email_lanes.py`).
- Approval: ONE `review.approval` step per run (skill review, lane None) once
  drafts are committed and no upstream step (parse, CRM, drafts, reviews a live
  reviewer will take) is moving; a lane resolved later gets a second, smaller
  batch. Inputs: `items` [{lane, draft_step, draft: "fact:lead:<n>.draft", to,
  subject, owner, approval_key}], `decision_key` "approval:emails", `threshold`
  (approval_auto_threshold), `always_ask_human`, `question`, `options`
  (approve | reject), `policy`, `context.not_emailed`. Postcondition
  `review.decided` args `{decision_key: "approval:emails", lanes, kind: "approval"}`.
- Approval facts (`approval.py`, the contract with the meta-reviewer, Track J):
  `approval:<lane>` = {decision approve|reject, draft_step, to, subject, score,
  flags, decided_by, ...} and the batch `approval:emails` = {decision
  approve|reject|partial, items {lane: ...}, approved [..], rejected [..],
  decided_by, model, threshold, step}. Helpers: `approval.policy_decisions()`
  (judge >= approval_auto_threshold, no flags, always_ask_human_email ->
  escalate) and `approval.commit_decisions()`. The judge scores the batch in
  ONE `judge.judge_drafts()` call (ids = lanes).
- Send: `email.send` steps (depend on the draft and the approval step) are
  created only for lanes whose committed approval fact says approve and matches
  the draft. The mailer re-checks the fact itself (no fact / reject / different
  draft -> `blocked`, acted=false, nothing sent), sends the committed draft
  unchanged via aiosmtplib with a deterministic Message-ID
  (`ledger-<sha>@ledger.local`, from run + lane + draft step), and does
  check-then-act through the Mailpit API on that Message-ID (a takeover never
  double-sends). `email.sent` expects `exactly_once`.
- `RunConfig.dry_run`: the mailer sends nothing and claims `dry_run`;
  `email.sent` (additive) then verifies that nothing was sent, trusting the
  claim only when the run config is dry_run.
- Run criteria: `email.sent` is swept per lane (`checks/email_lanes.py`):
  verified when every lane that should get an email has a committed send
  (approval rejections count as handled; excluded lanes are listed).

### Integration notes (wave W3 gate)

- J reads K's approval step as built: `decide_approval` takes `inputs.items`
  (draft fact refs resolved) when there is no `inputs.drafts`. The batch is
  decided all-or-nothing (one judge call; the min score must clear
  `approval_auto_threshold`, else the whole batch escalates). K's per-lane
  `approval.policy_decisions` (partial batches) is not used by the reviewer.
- Whoever decides an approval (meta-reviewer or a human answer, re-claimed by
  the reviewer) commits `approval:<lane>` per email (pinned to, subject,
  draft_step) and adds `items` / `approved` / `rejected` / `lanes` to
  `approval:emails`. The mailer and the send stage read those.
- `lookup_for`: J and K each carried the F/G lookup fix; main keeps the W2
  gate's version (one fix).
- A run pinned to an in-process orchestrator (`cli demo`) is adopted by the
  service orchestrator once the pinned one is no longer alive and the run is
  `completed_pending_input`, so answering an escalation after the demo exits
  still releases the lane. Runs still being planned stay pinned.
- After Sam Ito's escalation is answered, his lane is drafted, approved in a
  second, one-email batch and sent: 8 emails at `completed_pending_input`, 9
  once the run completes (plans/02 lists the 8 at the escalation point). The
  scripted fixtures include `lead:9` for the drafter and the batch judge.
- Report `decisions[]` also carry `model` and `source` (llm | rule | human | judge).

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
| GET    | `/escalations/{id}`                | One escalation (Track J, additive; GET /escalations takes ?run_id=&status=open\|answered\|all) |
| GET    | `/runs/{id}/steps/{step_id}`       | Step detail with attempt history|
| POST   | `/runs/{id}/determinism`           | Set level 0..1, seed            |
| GET/PUT| `/runs/{id}/config`                | Run controls (§6a)              |
| POST   | `/runs/{id}/replay`                | New run from the same inputs + config |
| GET    | `/llm/budget`                      | Requests used / remaining today |
| GET/PUT| `/agents/{id}/config`              | Prompt, injections, tools       |
| POST   | `/agents/{id}/restart`             | Restart an agent's container    |
| WS     | `/agents/{id}/exec`                | Sandboxed shell into container  |

**Implementation notes (Track H, API).** Additive endpoints beyond the table:
`POST /runs/{id}/config/reset` (H13 reset to playbook defaults), `GET /chaos`
(armed switches) and `DELETE /chaos/{fault}`. `POST /runs` also accepts an
optional hand-written `steps` plan (the CLI run-spec format) so `curl` can drive
a run without the planner; without it the run is only created (`run.created`)
for the orchestrator. `GET /stream`: `id` = stream id, `event` = type,
`Last-Event-ID` (or `?last_event_id=`) resumes; with `run_id` and no resume id
the run's backlog is replayed first; `?from=now`, `?follow=false`, `?max_s=`.
`POST /chaos/{fault}` sets `Keys.faults` (default 1 shot; `false_claim` is
scoped to `browser.espocrm` unless `skill` is given) and emits
`fault.injected` (phase `injected`); **Track N:** the injection belongs to
`run_id` (query or body) if given, else to the most recent run in flight
(created … running; never a `completed_pending_input` one), else it is
`pending` (`Keys.faults_pending`) and the first run whose worker consumes the
shot gets it on its timeline (`faults.attach_pending`: phase `injected`,
`attached: true`, just before phase `consumed`); `make chaos-* RUN=<id>`; `kill_worker` kills the agent's compose
container via the docker socket unless `kill: false`; its event records
`held_steps` (the steps whose lease the agent holds, read from the lease keys;
the heartbeat's `current_step` can lag a step) and the report pairs the kill
with that step's lease expiry and takeover (W2 gate). Only the api mounts
`/var/run/docker.sock` (joined via `group_add: DOCKER_GID`, process stays uid
10001); restart emits `config.updated` `{scope: agent, action: restart}`; the
shell runs as uid 10001 and emits `shell.opened`. Run config is written through
`run_config.py` (JSON string); `ledger.get_run_config` reads both that and the
CLI's Hash format. The report (`ledger_core/report.py`) is built only from
steps, facts and events; `?format=md` returns markdown.
