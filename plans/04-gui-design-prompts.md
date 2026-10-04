# GUI design prompts (for Claude Design)

Use the prompts in order. Prompt 0 sets the design language; the rest are one
screen each. After export, place the files in `web/design/` and run Phase 7.

> **Status (Oct 2026):** designed as `Ledger Dashboard` and `Ledger Canvas`.
> Where the delivered design differs from the prompts below, the design wins:
> light theme default with dark variant, top-bar views (Dashboard · Report ·
> Builder) instead of a left rail, approvals replaced by meta-reviewer
> escalations, and per-agent prompt / tools / shell sheets.

## Prompt 0: design language

```
Design a web app called "Ledger": the control room for an autonomous AI
operator that completes company tasks. The audience is engineers evaluating
whether the system is trustworthy, so the interface must feel like an
instrument, not a chatbot: dense, calm, precise. Think flight recorder or
CI pipeline view, not a marketing dashboard.

Design language:
- Dark theme first, with a light variant. Near-black neutral background,
  one accent colour, and a strict semantic palette for step states:
  planned (grey), ready (blue-grey), leased (blue, animated pulse),
  claimed (amber: "says it's done, not yet proven"), verified/committed
  (green), rejected (red), input required (violet), dead (dark red).
  The amber "claimed" vs green "committed" distinction is the most
  important visual idea in the product: a claim is not a fact.
- Monospace for ids, timestamps, events; a clean sans for everything else.
- No chat bubbles, no gradients, no illustrations, no emoji.
- Top bar: Dashboard · Report · Builder, plus a "Needs you" badge that counts
  escalations only.
- Desktop first at 1440px; must still work at 1024px.

Produce a small design system frame: colours, type scale, state chips,
buttons, event row, step node, agent card, evidence thumbnail.
```

## Prompt 1: run console

```
Using the Ledger design system, design the "New run" screen.

- One large input for a plain-language goal, placeholder: "Add the leads
  from yesterday's event to the CRM and set up follow-ups."
- Attach-file control (shows event_attendees.csv attached).
- Playbook selector showing "Event leads SOP" with a "view" link that opens
  the playbook in a side sheet.
- Start button.
- Below: table of previous runs with goal, status chip, duration, steps
  committed / total, recoveries count, pending approvals.
```

## Prompt 2: live run (the main screen)

```
Design the "Live run" screen for Ledger. This is the centrepiece.

Header: goal text, run status chip, elapsed time, counters (steps committed
/ total, retries, takeovers, model fallbacks), link to evidence report.

Three columns:

1. Left (narrow): "Success criteria" — the checkable criteria the system
   derived from the one-line goal and the playbook, each with a verdict
   icon (pending, verified, waived, failed). Under it, "Committed facts" —
   a compact key/value list that grows as steps commit.

2. Centre (wide): the step graph. A DAG that starts with
   understand -> plan -> parse file, then fans out into one lane per lead
   (search contact -> create or update -> create follow-up task -> draft
   email), then converges on approval -> send -> final verification.
   Each node shows kind, state colour, attempt count, and the agent
   currently holding its lease with a small TTL ring that drains and
   refills on heartbeat. A node in "claimed" state is amber with a
   "verifying" indicator; it turns green only on commit.
   Show these in the mock: one node rejected then retried (attempt 2), one
   node whose lease expired and was taken over by a second worker (show
   both worker names, first struck through), one node in input-required.

3. Right: the ledger timeline. Append-only event rows, newest at the
   bottom, auto-scroll with a pause control. Each row: time, actor chip,
   event type, one-line payload. Filter chips by actor and by event type.
   Fault-injected events are visually marked so cause and recovery read as
   a pair.

Clicking a step opens a drawer: inputs, postcondition (as readable text and
JSON), every attempt with the worker, model, observation, claim, verifier
verdict and reason, and before/after screenshots.
```

## Prompt 3: agents panel

```
Design the "Agents" screen for Ledger.

A grid of agent cards: orchestrator, verifier, parser, browser operator 1,
browser operator 2, drafter, mailer, and "Human (you)". Each card: name,
skills as chips, model in use (and fallback), liveness dot with last
heartbeat age, the step it currently holds with lease TTL ring, steps
completed, rejection rate.

Show one browser operator as "lost" (heartbeat stale, greyed, with the step
it dropped and who took it over). The Human card looks like the others on
purpose: humans are just another agent on the bus.
```

## Prompt 4: needs your attention (replaces approvals inbox)

Everything is automatic. Only items the meta-reviewer could not resolve
(confidence below threshold) appear, each with what it tried, the options and
its confidence, plus a "save as playbook rule" option. Beside it, a "handled
automatically" list shows each auto decision with its reason and who made it.

The original prompt is kept below for reference.

```
Design the "Approvals" screen for Ledger.

List on the left, detail on the right. Two items in the mock:

1. Clarification (batched): "3 rows need a decision." A table with one row
   per ambiguous lead: the source row, what the system found in the CRM,
   why it is ambiguous, and a choice control (e.g. link to Account A /
   Account B / skip). One "Submit decisions" button for the whole batch.

2. Approval: "8 follow-up emails ready to send." Each email is expandable
   with recipient, subject, body, an inline edit control, and the playbook
   rule that requires approval. Actions: approve all, approve selected,
   reject with note.

Show which playbook rule triggered each request, and what will happen
after approval.
```

## Prompt 5: evidence report

```
Design the "Evidence report" screen for Ledger: what a manager reads to
trust the work without watching it.

- Summary sentence at the top, then the goal and each success criterion
  with its verdict and how it was verified.
- Four sections with counts: Created, Updated (matched existing), Skipped
  (with reason per row), Awaiting approval. Each record row: name,
  company, owner, link to the CRM record, screenshot thumbnail, and the
  check that verified it.
- "What went wrong and how it recovered": a short list pairing each
  failure (false claim, worker died, session expired, model outage) with
  the recovery taken and the time lost.
- Footer: duration, models used by role, token cost.
- Export as markdown / PDF buttons.
Layout should print cleanly.
```

## Prompt 6: chaos panel

```
Design a "Chaos" screen for Ledger used during a live demo.

Four large, clearly labelled controls, each with a one-line description of
what it breaks and what recovery is expected:
- False claim: next browser step reports success without acting.
- Kill worker: terminate the browser operator currently holding a lease.
- Expire session: invalidate the CRM login.
- Model outage: make the primary worker model fail.

Beside the controls, a small live strip of the last 10 ledger events so
the effect is visible immediately. Each control shows the last time it was
fired and the recovery event it led to.

Add a determinism slider (exploratory ↔ deterministic) that shows the derived
temperature per role (verifier always 0) and a "pin seed" toggle.
```

## Prompt 7: agent sheet

```
Clicking any agent opens a side sheet with tabs: Overview (properties,
metrics), Prompt (editable versioned system prompt + the five injection
layers with token counts; history layer locked on), Tools (REST / MCP /
function / browser / SMTP / LLM, toggle + add, recent calls), Shell (live
sandboxed terminal into the agent's container, follow logs). Ledger adds
Streams, Verifier adds Checks, Playbook adds Content, Human adds Inbox.
```

## Handoff note for Claude Code (Phase 7)

```
The designs in web/design/ are the visual source of truth. Implement them
with React + Tailwind. Extract the state colours into CSS variables named
after the step states. Build the step graph with React Flow (dagre layout).
Timeline rows must be virtualised. All data comes from the API; SSE drives
live updates through a single store.
```

## Design inconsistencies to fix in Phase 7

Found when clicking through `web/design/` on 2026-10-04. The spec wins:

- Live ledger mock shows `input.requested 3 rows need a decision` and
  `input.requested 8 emails await approval`: v1 approval wording. Real events
  are `review.requested` / `review.resolved` / `approval.auto` /
  `review.escalated`; only Sam Ito produces `input.requested`.
- Mock events use `step.heartbeat`; the event list only has
  `step.heartbeat_lost`. Heartbeats renew the lease key and are not ledger
  events (too noisy). Show the TTL ring from `/agents` instead.
- Model names (claude-sonnet-4.5, claude-haiku-4.5, gemini-2.5-flash,
  gpt-4.1-mini) are mock. Render from role config; during the free-model
  phase they are the `:free` ids in 01 §6.
- Costs ($0.83 of $2.00) will read $0.00 on free models. Show the request
  budget (`GET /llm/budget`) next to spend.
- Builder simulates a run locally. In the build it submits `POST /runs` and
  animates from SSE.
- The Steps tab count (8) vs. graph (46) is a filtered view; keep both but
  label the filter.

## Phase 7 implementation notes (Track E)

All six inconsistencies above are fixed in `web/`: the live ledger shows real
event types only (no `step.heartbeat`; TTL rings come from `GET /agents`),
model names are rendered from `/agents` / `/llm/budget` role config, the run
strip and Spend cap show the free-request budget next to `$` spend, the
Builder submits `POST /runs` and animates from SSE, and the Steps tab is
labelled "N lanes" next to the Graph tab's "N steps". Deliberate deviations
from the designs:

- Builder: the design's local simulator controls (Step, Reset, speed) are gone
  because the canvas follows the real run; ADD creates local sketch nodes and
  the wiring is a per-viewer view (localStorage), since routing is by skill on
  the server and scaling operators is a compose change.
- Agent Shell: "follow logs" is replaced by "reconnect"; the shell is a real
  xterm.js terminal over `WS /agents/{id}/exec`, so `tail -f` works there.
- Playbook Content tab reads a proposed additive `GET /playbooks/{name}` and
  says so when the API does not have it.
- The step graph is React Flow + dagre over the real dependency graph rather
  than the design's fixed 5-column lane grid; the Steps table keeps the lane view.

## Phase 7 acceptance (Track L, wave W4)

`make test-gui` (`tests/e2e/gui_demo.py`, Playwright in the browser image
against `http://web`, scripted LLM backend) performs the plans/05 demo from
the browser: Builder run (`POST /runs`), Dashboard progress, false claim from
Run controls (verifier rejects), Kill worker on the browser lease holder (lease
expires, the other operator commits the step), the agent sheet's Restart
container, Determinism slider to 1.0 (`POST /runs/{id}/determinism`), the Sam
Ito escalation answered with save-as-rule, any email approval that escalates
(per email since Track N; the clean demo auto-approves all 8), Report and markdown
export. Screenshots land in `web/screenshots/real/` (git-ignored). Deviations:

- `GET /playbooks/{name}` now exists (additive), so the Content tab reads it.
- The approval card does not offer save-as-rule; rules apply to row decisions
  only (an email approval is per batch, not a reusable policy).
- Before each `make test-gui`: scrub the CRM, empty Mailpit and
  `git checkout playbooks/event-leads.md`, because the saved rule is written
  through the bind mount and would decide Sam on the next run.

## Collapsible long text (Track O, wave W4-extra)

User request: long vertical text takes too much space. `Collapsible` (title,
count, chevron, collapsed by default) and `ClampText` (N lines, more/less only
when it overflows) in `web/src/components/ui.tsx`; both print in full. Applied
to the escalation card's "What the meta-reviewer tried" (collapsed,
de-duplicated; the confidence line stays visible; context clamped to 2 lines),
"Handled automatically" reasons (2 lines), the step drawer (observation/reason
3 lines, postcondition JSON and earlier attempts collapsed, latest attempt
open) and Report decision/criteria evidence (2 lines). Deviation: the design
showed these expanded; `make test-gui` now asserts the trail starts collapsed
and expands it (`data-testid=esc-tried`).
