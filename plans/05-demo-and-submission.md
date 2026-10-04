# Demo and submission

## Deliverables

1. Repository with one-command run.
2. Video, 3-4 minutes, including failure injections.
3. `docs/design-note.md`, two pages.

## Video script

| Time  | On screen                                        | Say                                                                 |
|-------|--------------------------------------------------|---------------------------------------------------------------------|
| 0:00  | Thesis slide, one sentence                       | "Autonomy is a state-management problem."                           |
| 0:15  | Builder: type the one-line goal, Run             | The request leaves almost everything unstated.                      |
| 0:30  | Dashboard: criteria + Step graph                 | The playbook turned one line into checkable criteria and a plan.    |
| 0:45  | Orchestrator sheet → Prompt: layers, edit, save  | Prompts are rebuilt from committed facts each attempt; edits are versioned in the ledger. |
| 1:00  | Step graph + CRM side by side                    | The operator works the real CRM UI; each step is leased.            |
| 1:20  | Inject false claim → step drawer                 | Worker says done. Verifier checks the CRM API. Rejected, retried with the reason. |
| 1:45  | Kill worker → Browser op 1 shell (container down) | Lease expires, the second worker takes over with a higher fencing token. No duplicate. |
| 2:05  | Browser op 2 → Tools, then Shell: `ps`, `tail`    | Tools are APIs, MCP servers and functions per agent; the shell is a sandboxed exec into its container. |
| 2:20  | Determinism slider to 1.0                        | Same inputs, same plan: temperatures go to 0 and the seed is pinned. |
| 2:35  | Needs your attention: 1 escalation               | The meta-reviewer resolved the rest and auto-approved 8 emails. It only asks what it can't decide. |
| 2:55  | Commit decision (save as rule) → Mailpit inbox   | The answer becomes a fact, the lane resumes, and emails are actually sent. |
| 3:10  | Report view                                      | Created, updated, skipped and why; who decided each item; recoveries listed. |
| 3:30  | Architecture diagram                             | New workflow = new playbook + connectors, same substrate.           |

Record the clean run and the chaos run in one take if possible. Unedited
failure recovery is more convincing than a cut.

## Chaos commands

```
make chaos-false-claim     # next browser step claims done without acting
make chaos-kill-browser    # docker kill worker-browser-1 while it holds a lease
make chaos-expire-session  # invalidate the CRM session cookie
make chaos-model-outage    # set primary worker model to an invalid id
make determinism LEVEL=1.0 # POST /runs/{id}/determinism
make replay RUN=run_7f3a   # POST /runs/{id}/replay; cache-only, zero live LLM calls
```

Each calls `POST /chaos/{fault}` (or docker directly for kill) and emits a
`fault.injected` event so the timeline shows cause next to recovery. Add
`RUN=<id>` to pin the fault to a run; without it the API picks the most recent
run in flight, or (none in flight) holds it as pending for the next run that
consumes it (Track N).

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
7. **Humans by exception.** Why auto-decide with a meta-reviewer: thresholds,
   evidence, escalation, and "save as rule" turning each answer into policy.
   The risk: a confident wrong decision. Mitigation: the verifier still checks
   outcomes, and every auto decision is in the report.
8. **What I would build next.** Company memory learned from approvals and
   rejections; playbook induction from watched human runs; desktop apps.

## Pre-submission checklist

- [ ] Fresh clone on another machine runs with only `.env` filled in
- [ ] `make test` green
- [ ] Zero duplicate contacts after the chaos run (assert in a test)
- [ ] No API keys in git history
- [ ] README: thesis, GIF, run instructions, link to video and design note
- [ ] Design note states limits plainly
- [ ] Agent shell: non-root, no host mounts, no Docker socket, compose network only
- [ ] Determinism 1.0: two runs from clean produce identical plans (assert in a test)
- [ ] Rehearse on free models with a warm cache; a full live run uses ~25-30 of the 50 daily requests
- [ ] Rotate the OpenRouter key (it was shared in chat) and confirm `.env` is git-ignored
