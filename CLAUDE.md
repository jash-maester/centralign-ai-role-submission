# Ledger: build rules

Proof of concept for an autonomous AI operator on a shared, verified work
ledger. **`plans/` is the spec** (read 00 → 03 before coding; 01 is the
architecture). `web/design/*.dc.html` is the visual source of truth for the
GUI. If you deviate from a plan, say why and update the plan file in the same
commit.

## Ground rules (from plans/03 session primer)

- Python 3.12, type hints, pydantic v2, redis-py asyncio, FastAPI, pytest.
- Everything runs in docker compose. Never install anything on the host;
  run Python, pytest and npm inside containers (`make test`, `docker compose run ...`).
- No agent framework (no LangChain, CrewAI, AutoGen). The coordination layer
  is our own code.
- LLM calls go only through `ledger_core/llm.py` (`complete(role, messages, schema)`),
  OpenRouter, model ids from env by role. **Free `:free` models only** for now.
- Workers may only move a step to `claimed_done`. Only the verifier commits.
- Review steps are decided by the meta-reviewer; humans only get escalations.
- Every state change appends an event (`ledger_core/events.py`).
- Tests run in a container against a real Redis, never a mock. Write them
  alongside the code. Keep modules small; boring, readable code.

## Shared contracts (do not break)

| Module | What it fixes |
|---|---|
| `ledger_core/protocol.py` | Step/Run/Event/Fact/AgentCard/Envelope/Escalation models, `StepStatus` + `LEGAL_TRANSITIONS`, `Skill`, `StepKind`, `DEFAULT_CHECK`, `EventType`, `FaultName` |
| `ledger_core/keys.py` | Every Redis key name. Always use `Keys(ns)`; tests use a unique ns |
| `ledger_core/events.py` | `append_event`, `read_events`, `tail_events` |
| `ledger_core/config.py` | `RunConfig` (run controls, plans/01 §6a), temperatures, `config_hash` |
| `ledger_core/llm.py` | `complete()` signature, `set_backend()`, LLM error types |
| `ledger_core/postconditions.py` | `CHECK_NAMES`, `CheckResult`, `CheckContext`, `register`, `run_check`, `load_all` |
| `ledger_core/settings.py` | Env settings |

Extend contracts additively (new optional fields, new enum members) and note
it in your report. Never rename or remove. Plug-in points so tracks don't edit
shared files: checks go in `ledger_core/checks/<channel>.py` (auto-loaded),
API routes in `services/api/app/routes/<area>.py` exposing `router` (auto-included),
service entrypoints in `ledger_core/services/<service>.py`.

## Docker

- `make up` (build + wait healthy) · `make seed` (idempotent) · `make test`
  (unit + CRM; needs up + seed) · `make test-unit` · `make test-crm` ·
  `make test-browser` · `make logs SVC=x` · `make down` · `make clean` (wipes volumes).
- Host has 4 CPUs / ~7.7 GB for Docker. Start only the services you need
  (`docker compose up -d --wait redis espocrm mailpit`), and `docker compose down -v`
  when you finish.
- **Parallel stacks (git worktrees):** `.env` is git-ignored, so copy it from
  `/Users/jash/Work/CentrAlignAI/.env` and set a unique `COMPOSE_PROJECT_NAME`,
  `IMAGE_TAG` and host ports (`REDIS_PORT ESPO_PORT MAILPIT_PORT API_PORT WEB_PORT`,
  plus `CRM_PUBLIC_URL=http://localhost:<ESPO_PORT>`). Otherwise stacks and
  images collide.
- CRM: EspoCRM 10 at `http://espocrm` inside the network. `make seed` stores
  scoped API keys in Redis `config:crm`: `verifier_api_key` (read-only role;
  verifier + meta-reviewer only), `writer_api_key` (read/write; only the
  `api.espocrm` fallback skill). The browser logs in as `ESPO_OPERATOR_USER`.
  Workers never get the verifier's REST key.
- Mailpit: SMTP `mailpit:1025`, API `http://mailpit:8025/api/v1`.

## LLM budget (OpenRouter free tier: 50 requests/day across everything)

- Unit and integration tests use a stub backend (`llm.set_backend`). Never
  call OpenRouter from normal tests; live tests are marked `live_llm` and run
  only via `make test-live`.
- Check what's left before any live call:
  `curl -s https://openrouter.ai/api/v1/key -H "Authorization: Bearer $OPENROUTER_API_KEY"`
  (`free_model_daily_requests.remaining`). Never print the key.
- Prompts must not contain volatile values (run/step ids, timestamps) so the
  response cache (`LLM_CACHE_DIR`, survives `make clean`) hits across runs.

## Git

- Commit after each completed unit of work. Messages end with
  `Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>`.
- Never commit `.env`, `.cache/`, evidence or secrets.
