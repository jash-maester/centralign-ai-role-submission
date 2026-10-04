# GUI design prompts (for Claude Design)

Use the prompts in order. Prompt 0 sets the design language; the rest are one
screen each. After export, place the files in `web/design/` and run Phase 7.

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
- Left nav rail: Runs, Approvals (with count badge), Agents, Chaos.
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

## Prompt 4: approvals inbox

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
```

## Handoff note for Claude Code (Phase 7)

```
The designs in web/design/ are the visual source of truth. Implement them
with React + Tailwind. Extract the state colours into CSS variables named
after the step states. Build the step graph with React Flow (dagre layout).
Timeline rows must be virtualised. All data comes from the API; SSE drives
live updates through a single store.
```
