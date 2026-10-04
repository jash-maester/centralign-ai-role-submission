# Ledger: an AI operator built on a shared, verified work ledger

Submission for the CentrAlign AI Founding Engineer problem statement.
Working name: **Ledger** (rename freely).

## Thesis

Autonomy is a state-management problem, not a prompting problem.

Humans stay in the loop today because agents are unreliable, cannot prove they
finished, and act irreversibly on ambiguous requests. Ledger removes those
reasons with a coordination substrate borrowed from pre-LLM distributed systems:

1. **One source of committed truth.** Agents never read each other's chat
   history. They read committed facts from a ledger.
2. **A claim is not a fact.** A worker's "done" callback is only a claim. A
   separate verifier checks the real system through a different channel before
   anything is committed.
3. **Leases, heartbeats, fencing tokens.** Any worker can die at any point; the
   step is picked up by another worker with no duplicated side effects.
4. **Humans are the last agent on the bus.** A meta-reviewer resolves ambiguity
   and approvals automatically. Only decisions it cannot make with enough
   confidence escalate to a human, as a task state (`input_required`), not a
   separate system.

## The one workflow we demo

> "Add the leads from yesterday's event to the CRM and set up follow-ups."

The request is one line. The playbook (company context) supplies everything
unstated: dedupe rules, owner routing, follow-up policy, what needs approval.

The operator parses a messy attendee file, drives a real self-hosted CRM through
a browser, drafts follow-up emails, has the meta-reviewer resolve ambiguous rows and
approve the emails automatically, escalates the one decision it cannot make
confidently, verifies every outcome against the CRM API, and returns an
evidence report.

## Mapping to the brief

| Brief asks for                     | Where it shows up                                   |
|------------------------------------|-----------------------------------------------------|
| Understand intended outcome        | Orchestrator expands goal + playbook into criteria  |
| Determine / plan actions           | Step graph with postconditions written to ledger    |
| Select tools                       | Routing by agent card (skills)                      |
| Execute                            | Parser, browser operator, drafter workers           |
| Observe                            | Every action emits an observation event             |
| Adapt / recover                    | Retry, replan, lease takeover, model fallback       |
| Maintain state                     | Redis ledger: runs, steps, facts, events            |
| Ask for input / approval           | Meta-reviewer decides; escalates below threshold    |
| Verify completion                  | Independent verifier, deterministic checks first    |
| Return evidence                    | Evidence report: records, screenshots, skips        |

## Files in this directory

| File                          | Purpose                                              |
|-------------------------------|------------------------------------------------------|
| `01-architecture.md`          | Services, compose layout, ledger schema, protocol    |
| `02-features.md`              | Feature list with priorities and acceptance criteria |
| `03-build-phases.md`          | Phase-by-phase prompts to hand to Claude Code        |
| `04-gui-design-prompts.md`    | Prompts for Claude Design to produce the web GUI     |
| `05-demo-and-submission.md`   | Demo script, chaos commands, design-note outline     |

## How to use these plans with Claude Code

1. Open this folder in Claude Code.
2. Paste the "Session primer" from `03-build-phases.md` first.
3. Run the phases in order. Do not start a phase until the previous phase's
   acceptance checks pass.
4. Phases 0-3 are the argument. Everything after is polish. If time runs out,
   cut from the bottom.

## Decisions already made (and why)

| Decision                    | Choice                         | Reason                                             |
|-----------------------------|--------------------------------|----------------------------------------------------|
| Language                    | Python 3.12                    | Playwright, FastAPI, fastest path with Claude Code |
| Ledger store                | Redis 7 (Streams + hashes, AOF)| Leases/TTL native; one container                   |
| LLM access                  | OpenRouter (OpenAI-compatible) | Multi-model, fallbacks, one key                    |
| Models (build phase)        | OpenRouter `:free` models only | No spend until the account is topped up; see 01 §6 |
| Protocol                    | A2A-shaped subset over Streams | Recognisable, not a reinvention, small to build    |
| CRM                         | EspoCRM (self-hosted)          | One container + DB, real UI, real REST API         |
| Email                       | Mailpit                        | Real SMTP send, nothing leaves the laptop          |
| Browser                     | Playwright (Chromium)          | Deterministic selectors + screenshots as evidence  |
| GUI                         | React + Vite, served by nginx  | Static build, talks to API over REST + SSE         |
| Packaging                   | docker-compose, one `make up`  | Reviewer can run it in one command                 |
| Human in the loop           | By exception (meta-reviewer)   | Approvals don't stall the run; humans see only real ambiguity |
| Determinism                 | One slider → per-role temp + seed | Reproducible demo runs; verifier always at 0    |
| Agent ops                   | Per-agent prompt, tools, shell | Inspect and change any agent live, all versioned in the ledger |

## Merge notes (2026-10-04)

These files are the v2 plans from the design handoff, merged with:
- the v1 plans (archived in `plans/archive/2026-10-03-v1/`). v2 supersedes v1
  everywhere; the only v1 idea dropped is per-email *edit* in an approvals
  inbox, which no longer exists (emails are auto-approved or escalated).
- the "spec gaps" listed in `HANDOFF.md`: run controls are now specified in
  01 §6a, F7-F8 and H13; replay in 01 §6a and 05.
- the free-model constraint: build and demo on OpenRouter `:free` models
  first (01 §6). The free tier allows **50 free-model requests per day**, so
  the LLM layer has a response cache and a daily request budget (01 §6b).
- details the design fixes that the text left open: the exact 12-row demo
  dataset, owner names, event name, and the "open deal → no email" rule
  (02 Demo data).
- design mock inconsistencies to correct in Phase 7 (04, end of file).

## Non-goals

- More than one workflow.
- Full A2A spec compliance.
- Vision-based computer use. The browser operator uses the accessibility tree
  and selectors; vision is listed as future work.
- Auth, multi-tenancy, production hardening. The per-agent shell is sandboxed
  (container-scoped, no host mounts) but unauthenticated: demo only.
