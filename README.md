# Ledger: an AI operator built on a shared, verified work ledger

> **Autonomy is a state-management problem, not a prompting problem.**

Ledger turns a one-line request (*"Add the leads from yesterday's event to the CRM
and set up follow-ups."*) into verified work in a real company system. Agents parse
a messy attendee file, drive a self-hosted CRM through a browser, draft and send
follow-up emails, and return an evidence report. A human is asked only about the
one decision the system cannot make confidently.

The innovation is in the backend coordination layer, not the UI:

- **One source of committed truth.** Agents never read each other's chat. They read
  committed facts from a Redis ledger.
- **A claim is not a fact.** A worker can only *claim* a step is done. An independent
  verifier checks the real system through a **different channel** (CRM REST API,
  while workers act through the browser) before anything is committed.
- **Leases, heartbeats and fencing tokens.** Kill any worker mid-step; another takes
  over with a higher token and no duplicated side effects.
- **Humans by exception.** A meta-reviewer resolves ambiguity and approvals when its
  confidence clears a threshold; only the rest escalates to a person, as a task state.

**Submission write-up** (architecture, design decisions, limitations, next steps,
assumptions, models and services used): [`submission/SUBMISSION.md`](submission/SUBMISSION.md).
**Demo video and screenshots:** [`submission/demo/`](submission/demo/) and
[`submission/screenshots/`](submission/screenshots/).

---

## Requirements

| What | Needed | Notes |
|---|---|---|
| Docker | Docker Desktop 4.x or Docker Engine + Compose v2.20+ | Everything runs in containers; nothing else is installed on the host |
| Docker resources | 4 CPUs, ~8 GB RAM, ~10 GB disk | 16 containers incl. EspoCRM, MariaDB and two headless Chromium workers |
| CPU architecture | arm64 or amd64 | All images are multi-arch (built and tested on Apple Silicon) |
| `make`, `git` | any recent version | `make help` lists every target |
| Free host ports | 3000, 8000, 8080, 8025, 6379 | Change them in `.env` if taken |
| Internet | only for image pulls and (optionally) OpenRouter | The CRM and email are local; no email leaves the machine |

## Keys and credentials

| Key | Required? | Purpose |
|---|---|---|
| `OPENROUTER_API_KEY` | **Optional** | Live LLM calls through [OpenRouter](https://openrouter.ai) (OpenAI-compatible). Without it, set `LLM_BACKEND=scripted` and every agent uses recorded model answers from `tests/fixtures/llm/`: the whole demo runs offline and deterministically. |
| EspoCRM / MariaDB passwords | Pre-filled | Local sandbox defaults in `.env.example`. Change them if you expose the stack. |
| CRM API keys | Generated | `make seed` creates two scoped EspoCRM API users (read-only for the verifier, read/write only for the API fallback skill) and stores their keys in Redis. |

No other accounts, tokens or external services are needed. All CRM data is
synthetic and seeded from `data/crm_seed.json`.

**About the OpenRouter key.** Models are configured per role in `.env`
(`MODEL_ORCHESTRATOR`, `MODEL_WORKER`, `MODEL_VERIFIER`, `MODEL_META_REVIEWER`) as a
primary plus fallbacks. The defaults are free `:free` models. OpenRouter's free tier
allows 50 free-model requests per day (1000 per day once the account has bought at
least $10 of credit); a full live demo run uses roughly 25-30 requests. Ledger reads
the remaining budget from OpenRouter's `GET /api/v1/key`, keeps a reserve, caches
responses on disk (`.cache/llm/`) and refuses live calls rather than overspend.
Switching to paid models is an `.env` change only.

## Run it

```bash
git clone git@github.com:jash-maester/centralign-ai-role-submission.git ledger && cd ledger
cp .env.example .env          # add OPENROUTER_API_KEY, or set LLM_BACKEND=scripted for offline
make up                       # build + start all services, waits until healthy (first build ~5-10 min)
make seed                     # seed EspoCRM: users, scoped API keys, accounts, contacts, an open deal
```

Then open the GUI and press **Run** in the Builder, or run the same workflow from the CLI:

```bash
make demo                     # submits the one-line goal, waits, prints the evidence report
```

| Open | URL | Login |
|---|---|---|
| Ledger GUI (Dashboard · Report · Builder) | http://localhost:3000 | none |
| EspoCRM, the "company system" being operated | http://localhost:8080 | `admin` / `ledger-admin-pw` |
| Mailpit inbox (all outgoing email lands here) | http://localhost:8025 | none |
| API + OpenAPI docs | http://localhost:8000/docs | none |

**Reset to a clean world** (empty ledger, freshly seeded CRM, empty inbox):

```bash
make clean && make up && make seed
```

### What a run does

The demo input `data/event_attendees.csv` has 12 deliberately messy rows. With the
playbook `playbooks/event-leads.md`, a run ends with **6 contacts created, 3 updated
(2 exact duplicates, 1 fuzzy match), 2 skipped with a reason (an in-file duplicate and a
phone-only row), 1 escalated to you** (Sam Ito: "Lumen" matches two CRM accounts),
**8 follow-up emails** in Mailpit (none to Ben Ortiz, who has an open deal), and a
follow-up task for every contact. Answer the escalation in *Needs your attention* and
Sam's lane completes (7 created, 9 emails).

### Break it on purpose

Use the fault buttons in the GUI (Builder or Dashboard → Run controls), or:

```bash
make chaos-false-claim        # next browser step claims "done" without acting -> verifier rejects, retry succeeds
make chaos-kill-browser       # docker kill the browser operator holding a lease -> takeover, no duplicate
make chaos-expire-session     # invalidate the CRM login -> operator re-authenticates
make chaos-model-outage       # primary model fails -> model.fallback to the next model in the role list
make determinism LEVEL=1.0 RUN=<run_id>   # temperatures to 0 + pinned seed
make replay RUN=<run_id>      # re-run from the same inputs and config with zero live LLM calls
```

## Tests

All tests run in containers against a real Redis, real EspoCRM and real Mailpit.

| Command | What | Last result |
|---|---|---|
| `make lint` | ruff | clean |
| `make test` | unit + CRM integration (needs `make up && make seed`) | 537 passed |
| `make test-browser` | Playwright skills and the browser worker against EspoCRM | 42 passed |
| `make test-e2e FRESH=1` | full stack: clean demo, chaos run with four faults and **zero duplicates**, determinism 1.0 gives identical plans, replay with zero live calls, restart mid-run | 5 passed |
| `make test-gui` | drives the whole demo from the GUI with Playwright, saves screenshots | passed (~100 s) |
| `make web-check` | web typecheck, lint, vitest | 34 passed |
| `make test-live` | smoke test against OpenRouter (uses the daily budget) | passed |
| `make demo LLM_BACKEND=openrouter` with `LLM_CACHE=off` | the full demo on live free models | completed: 28 live calls, same outcome ([details](submission/SUBMISSION.md#live-run-on-real-models)) |

## Repository layout

```
plans/                 the spec: thesis, architecture, features, build phases, GUI, demo
playbooks/             company context (event-leads.md): dedupe, routing, follow-up, approval, escalation rules
data/                  messy attendee CSV, CRM seed data
services/core/         ledger_core: ledger, leases, bus, orchestrator, verifier, meta-reviewer, workers, LLM layer
services/browser/      Playwright operator for EspoCRM (two replicas)
services/api/          FastAPI: runs, steps, SSE stream, agents, chaos, escalations, report
web/                   React + Vite + TypeScript + Tailwind GUI (designs in web/design/)
tests/                 unit, CRM, browser, e2e and GUI tests; scripted LLM fixtures
submission/            write-up, demo video, screenshots
```

See [`submission/SUBMISSION.md`](submission/SUBMISSION.md) for the architecture and
design decisions, and [`HANDOFF.md`](HANDOFF.md) for redeploying or extending it.
