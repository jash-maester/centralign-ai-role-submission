# Ledger — handoff

## Redeploying (status: built, all phases integrated and verified)

**Requirements:** Docker Desktop 4.x or Docker Engine + Compose v2.20+ with 4 CPUs,
~8 GB RAM and ~10 GB disk for Docker; `make` and `git`; free host ports 3000, 8000,
8080, 8025, 6379 (all changeable in `.env`). arm64 and amd64 both work.

**Keys:**

| Variable | Required | Notes |
|---|---|---|
| `OPENROUTER_API_KEY` | optional | Live LLM calls. Without it set `LLM_BACKEND=scripted` (offline, recorded answers). Free tier: 50 free-model requests/day, 1000/day once the account has bought ≥ $10 of credit. A full live run uses ~25-30 requests. |
| `MODEL_ORCHESTRATOR` / `MODEL_WORKER` / `MODEL_VERIFIER` / `MODEL_META_REVIEWER` | pre-filled | Comma-separated primary + fallbacks (free models by default). Keep the verifier on a different model family from the workers. |
| `ESPO_*` passwords | pre-filled | Local sandbox defaults; change them if the stack is exposed. |
| CRM API keys | generated | `make seed` creates a read-only verifier key and a writer key (fallback skill only) and stores them in Redis. |

**Steps:** `cp .env.example .env` → add the key (or `LLM_BACKEND=scripted`) →
`make up && make seed` → http://localhost:3000. Reset with `make clean && make up && make seed`.
Never commit `.env`; it is git-ignored, and so are `.cache/` (LLM response cache) and evidence.

**Before going further than a demo:** add auth in front of the API and GUI (the agent
shell is sandboxed but unauthenticated), rotate the sandbox passwords, and point
`CRM_*` at a sandbox CRM, never a production one.

The rest of this file is the original build handoff (plans, designs and the
design-to-endpoint map), kept for anyone extending the system.

## Build handoff (original)

### What's in here

```
plans/                 Spec. Read in order; these are the source of truth.
  00-README.md         Thesis, demo workflow, decisions
  01-architecture.md   Services, ledger schema, protocol, meta-reviewer, API
  02-features.md       Features with priorities + acceptance criteria
  03-build-phases.md   Phase-by-phase prompts (paste the session primer first)
  04-gui-design-prompts.md   Original GUI prompts + status note
  05-demo-and-submission.md  Video script, chaos commands, checklist
web/design/            Visual source of truth for Phase 7
  Ledger Dashboard.dc.html   Main app: Dashboard · Report · Builder (embeds Canvas)
  Ledger Canvas.dc.html      Builder: node-graph of agents around the ledger
  support.js                 Runtime needed to open the designs in a browser
```

Open `web/design/Ledger Dashboard.dc.html` in a browser (serve the folder,
e.g. `npx serve web/design`) to click through everything. All data is mock.

### How to use with Claude Code

1. Put `plans/` at the repo root as `plans/` and `web/design/` under `web/`.
2. Paste the **Session primer** from `plans/03-build-phases.md`.
3. Run phases 0–8 in order; don't start a phase until the previous one passes.

### Design → implementation map (Phase 7)

The designs are single-file prototypes. Re-implement in React + Vite +
TypeScript + Tailwind as specified; do not ship the .dc.html files.

| Design element | Data source (01-architecture §10) |
|---|---|
| Needs your attention (escalations) | `GET/POST /escalations` |
| Handled automatically | `review.resolved`, `approval.auto` events via SSE |
| Run strip / progress | `GET /runs/{id}` + SSE |
| Workflow → Agents table | `GET /agents` |
| Workflow → Steps table / Graph | `GET /runs/{id}/steps` |
| Step drawer | `GET /runs/{id}/steps/{step_id}` |
| Agent sheet: Overview / Prompt / Tools | `GET/PUT /agents/{id}/config` |
| Agent sheet: Shell | `WS /agents/{id}/exec` (sandboxed) |
| Run controls | `POST /runs/{id}/determinism`, run config, `POST /chaos/{fault}` |
| Live ledger | `GET /stream?run_id=` (SSE) |
| Report | `GET /runs/{id}/report` (JSON + md) |
| Builder canvas | `GET /agents` + wiring config; packets from SSE |

In the design source, every `MOCK_*` / constant block notes the endpoint that
replaces it. Theme tokens are CSS variables in the `<helmet><style>` of each
file — extract them as Tailwind theme colours named after the step states.

### Spec gaps to close while building

> **Closed 2026-10-04:** run controls are now in `plans/01-architecture.md` §6a
> (+ §6b free-model budget/cache) and `02-features` F7-F9, H13-H14. Kept below
> for history.

- **Run controls not yet in the plan docs:** spend cap, fuzzy-match threshold,
  dry run, browser concurrency, CRM write path (browser / auto / API),
  check-then-act, replan and fallback toggles, lease TTL, max attempts.
  Add them to `01-architecture` (run config hash + endpoint) and
  `02-features` when implementing.
- **Determinism:** temperature mapping is in `01-architecture §6`; the
  "two runs at 1.0 produce identical plans" test is in `05` checklist.
- **Shell sandbox:** non-root, no host mounts, no Docker socket, compose
  network only. Demo-only; there is no auth.
