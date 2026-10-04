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
4. **Humans are just another agent on the bus.** Approval and clarification are
   task states (`input_required`), not a separate system.

## The one workflow we demo

> "Add the leads from yesterday's event to the CRM and set up follow-ups."

The request is one line. The playbook (company context) supplies everything
unstated: dedupe rules, owner routing, follow-up policy, what needs approval.

The operator parses a messy attendee file, drives a real self-hosted CRM through
a browser, drafts follow-up emails, asks one batched question about ambiguous
rows, holds emails for approval, verifies every outcome against the CRM API, and
returns an evidence report.

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
| Ask for input / approval           | `input_required` state, approvals inbox             |
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
| Protocol                    | A2A-shaped subset over Streams | Recognisable, not a reinvention, small to build    |
| CRM                         | EspoCRM (self-hosted)          | One container + DB, real UI, real REST API         |
| Email                       | Mailpit                        | Real SMTP send, nothing leaves the laptop          |
| Browser                     | Playwright (Chromium)          | Deterministic selectors + screenshots as evidence  |
| GUI                         | React + Vite, served by nginx  | Static build, talks to API over REST + SSE         |
| Packaging                   | docker-compose, one `make up`  | Reviewer can run it in one command                 |

## Non-goals

- More than one workflow.
- Full A2A spec compliance.
- Vision-based computer use. The browser operator uses the accessibility tree
  and selectors; vision is listed as future work.
- Auth, multi-tenancy, production hardening.
