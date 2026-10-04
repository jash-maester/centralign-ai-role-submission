# Demo and submission

## Deliverables

1. Repository with one-command run.
2. Video, 3-4 minutes, including failure injections.
3. `docs/design-note.md`, two pages.

## Video script

| Time  | On screen                              | Say                                                              |
|-------|----------------------------------------|------------------------------------------------------------------|
| 0:00  | Thesis slide, one sentence             | "Autonomy is a state-management problem."                        |
| 0:15  | GUI: type the one-line goal            | The request leaves almost everything unstated.                   |
| 0:30  | Criteria + plan appear                 | The playbook turned one line into checkable criteria.            |
| 0:50  | Timeline + CRM side by side            | Operator works the real CRM UI; each step is leased.             |
| 1:20  | Click chaos: false claim               | Worker says done. Verifier checks the CRM API. Rejected. Retry.  |
| 1:50  | `make chaos-kill-browser`              | Lease expires, second worker takes over, no duplicate created.   |
| 2:20  | Approvals inbox                        | One batched question; emails held until approved.                |
| 2:45  | Approve -> Mailpit inbox               | Emails actually sent.                                            |
| 3:00  | Evidence report                        | What was created, updated, skipped and why; recoveries listed.   |
| 3:20  | Architecture diagram                   | New workflow = new playbook + connectors, same substrate.        |

Record the clean run and the chaos run in one take if possible. Unedited
failure recovery is more convincing than a cut.

## Chaos commands

```
make chaos-false-claim     # next browser step claims done without acting
make chaos-kill-browser    # docker kill worker-browser-1 while it holds a lease
make chaos-expire-session  # invalidate the CRM session cookie
make chaos-model-outage    # set primary worker model to an invalid id
```

Each calls `POST /chaos/{fault}` (or docker directly for kill) and emits a
`fault.injected` event so the timeline shows cause next to recovery.

## Design note outline (`docs/design-note.md`)

1. **Interpretation of the problem.** Why humans are in the loop today:
   unreliability, no proof of completion, irreversible actions on ambiguous
   requests.
2. **The bet.** A shared, verified ledger with leases; claims vs. facts;
   action and verification through different channels.
3. **Architecture.** One diagram, one paragraph per component.
4. **Relationship to existing work.** Maps onto A2A (tasks, artifacts, agent
   cards, input-required) and is compatible with MCP for tools. What is
   different: commit-on-verification and fencing, which those protocols leave
   to the implementer.
5. **Generalisation.** A new workflow needs a playbook, postcondition checks,
   and connector skills. No orchestrator changes. Sketch a second workflow
   (e.g. vendor invoice reconciliation) in five lines.
6. **What breaks, honestly.**
   - Postconditions must be writable; fuzzy outcomes fall back to an LLM judge.
   - Selector-based browser control is brittle against UI change; vision-based
     computer use is the next step.
   - The planner can produce a wrong but verifiable plan; criteria come from
     the playbook, so playbook quality is the ceiling.
   - Redis is a single point of failure here.
   - Check-then-act is not atomic against an external system; a narrow race
     remains without CRM-side idempotency keys.
7. **What I would build next.** Company memory learned from approvals and
   rejections; playbook induction from watched human runs; desktop apps.

## Pre-submission checklist

- [ ] Fresh clone on another machine runs with only `.env` filled in
- [ ] `make test` green
- [ ] Zero duplicate contacts after the chaos run (assert in a test)
- [ ] No API keys in git history
- [ ] README: thesis, GIF, run instructions, link to video and design note
- [ ] Design note states limits plainly
