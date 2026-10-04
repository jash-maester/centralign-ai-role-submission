# Feature list

Priority: **P0** = the argument (must ship), **P1** = makes the demo
convincing, **P2** = polish, cut first.

## A. Ledger and coordination

| #   | Feature                                   | P  | Acceptance                                                              |
|-----|-------------------------------------------|----|-------------------------------------------------------------------------|
| A1  | Append-only event stream                  | P0 | Every state change has an event; replaying events rebuilds step status  |
| A2  | Step records with state machine           | P0 | Illegal transitions raise; unit tests cover every edge                  |
| A3  | Leases with TTL + heartbeat               | P0 | Killing a worker returns its step to `ready` within 20s                 |
| A4  | Fencing tokens                            | P0 | A write with a stale token is rejected and logged                       |
| A5  | Committed-facts store                     | P0 | Facts appear only after `verified`; workers read only facts             |
| A6  | Skill queues + consumer groups            | P0 | Two browser workers never hold the same step                            |
| A7  | Agent cards + liveness                    | P1 | `/agents` shows cards, alive/lost, current step                         |
| A8  | A2A-shaped envelopes                      | P1 | All bus messages validate against the pydantic envelope                 |
| A9  | Ledger persistence across restart         | P1 | `docker compose restart` mid-run resumes without loss                   |

## B. Understand and plan

| #   | Feature                                   | P  | Acceptance                                                              |
|-----|-------------------------------------------|----|-------------------------------------------------------------------------|
| B1  | Goal + playbook -> success criteria       | P0 | One-line goal yields explicit, checkable criteria shown in the GUI      |
| B2  | Plan as step graph with postconditions    | P0 | Every step has a postcondition from the registry                        |
| B3  | Routing by agent card                     | P0 | Orchestrator never names a worker, only a skill                         |
| B4  | Dynamic fan-out                           | P0 | Per-lead steps are created after the parse step commits, not before     |
| B5  | Replanning on failure                     | P1 | After 2 rejections the remaining plan is revised and `plan.revised` emitted |
| B6  | Playbook sectioning                       | P1 | Workers receive only the sections relevant to their step kind           |

## C. Execution

| #   | Feature                                   | P  | Acceptance                                                              |
|-----|-------------------------------------------|----|-------------------------------------------------------------------------|
| C1  | Parser worker (CSV, messy)                | P0 | Normalises names, emails, phones, company; flags unusable rows          |
| C2  | Browser operator: login + session reuse   | P0 | Recovers from expired session by re-login                               |
| C3  | Browser: search contact                   | P0 | Finds existing contacts by email and by fuzzy name+company              |
| C4  | Browser: create contact                   | P0 | Check-then-act; no duplicate on retry or takeover                       |
| C5  | Browser: update existing (dedupe path)    | P0 | Existing contact enriched, not duplicated                               |
| C6  | Browser: create follow-up task            | P0 | Task linked to contact, due date and owner per playbook                 |
| C7  | Screenshot per browser step               | P0 | Saved to evidence volume, referenced in claim                           |
| C8  | Drafter worker                            | P1 | Email per lead, personalised from committed facts only                  |
| C9  | Mailer worker                             | P1 | Sends only steps with a committed approval fact                         |
| C10 | PDF attendee list parsing                 | P2 | Same output shape as CSV                                                |
| C11 | API fallback skill for CRM writes         | P2 | Used by replanner when UI path is blocked                               |

## D. Verification

| #   | Feature                                   | P  | Acceptance                                                              |
|-----|-------------------------------------------|----|-------------------------------------------------------------------------|
| D1  | Postcondition registry                    | P0 | All checks in `01-architecture.md` section 8 implemented                |
| D2  | Verifier reads world via REST, not claims | P0 | A fabricated claim is rejected                                          |
| D3  | Rejection feeds next attempt              | P0 | Rejection reason appears in the next prompt                             |
| D4  | LLM judge for soft checks                 | P1 | Runs only after deterministic checks pass; different model family       |
| D5  | Final run-level criteria sweep            | P0 | Run cannot be `completed` unless every criterion is verified or waived  |

## E. Human in the loop

| #   | Feature                                   | P  | Acceptance                                                              |
|-----|-------------------------------------------|----|-------------------------------------------------------------------------|
| E1  | Approval policy from playbook             | P0 | External email send always creates an approval                          |
| E2  | Batched clarification                     | P0 | All ambiguous rows -> one question, not one per row                     |
| E3  | Non-blocking approvals                    | P1 | Unrelated steps proceed while approval is open                          |
| E4  | Approve / edit / reject in GUI            | P1 | Answer becomes a committed fact; dependent steps become `ready`         |

## F. Self-healing and chaos

| #   | Feature                                   | P  | Acceptance                                                              |
|-----|-------------------------------------------|----|-------------------------------------------------------------------------|
| F1  | Kill worker -> takeover                   | P0 | `make chaos-kill-browser` mid-run; run completes; zero duplicates       |
| F2  | False-claim fault                         | P0 | Worker claims done without acting; verifier rejects; retry succeeds     |
| F3  | Expired CRM session fault                 | P1 | Operator observes login page, re-authenticates, continues               |
| F4  | Model outage fault                        | P1 | Bad primary model id -> `model.fallback` -> step completes              |
| F5  | UI-changed fault                          | P2 | Selector broken -> replan to API skill                                  |

## G. Evidence and reporting

| #   | Feature                                   | P  | Acceptance                                                              |
|-----|-------------------------------------------|----|-------------------------------------------------------------------------|
| G1  | Evidence report                           | P0 | Created / updated / skipped (with reason) / awaiting approval, with CRM links and screenshots |
| G2  | Recovery summary in report                | P1 | Lists every retry, takeover, fallback that occurred                     |
| G3  | Cost and token accounting                 | P2 | Per run and per role                                                    |

## H. Interface

| #   | Feature                                   | P  | Acceptance                                                              |
|-----|-------------------------------------------|----|-------------------------------------------------------------------------|
| H1  | CLI: submit run, tail ledger              | P0 | `make demo` runs the full workflow and prints the report                |
| H2  | Web: run console + live timeline          | P1 | Events appear within 1s via SSE                                         |
| H3  | Web: step graph with states               | P1 | Colours match state machine; click shows history                        |
| H4  | Web: agents panel                         | P1 | Shows liveness and which step each agent holds                          |
| H5  | Web: approvals inbox                      | P1 | Approve/edit/reject                                                     |
| H6  | Web: evidence report view                 | P1 | Screenshots inline                                                      |
| H7  | Web: chaos panel                          | P2 | Buttons for F1-F4                                                       |

## I. Packaging

| #   | Feature                                   | P  | Acceptance                                                              |
|-----|-------------------------------------------|----|-------------------------------------------------------------------------|
| I1  | `docker compose up` brings up everything  | P0 | Fresh clone + `.env` + `make up && make seed && make demo` works        |
| I2  | Healthchecks and ordered startup          | P0 | No manual waiting or restarts                                           |
| I3  | Seed script for CRM                       | P0 | Idempotent; creates users, API key, existing contacts                   |
| I4  | Test suite in container                   | P1 | `make test` green                                                       |
| I5  | README with one-command run + GIF         | P1 |                                                                         |

## Demo data

`data/event_attendees.csv`, 12 rows, deliberately messy:
- 6 clean new leads
- 2 exact duplicates of seeded CRM contacts (same email, different casing)
- 1 fuzzy duplicate (same person, personal email, company matches)
- 1 row with no email (phone only) -> skip with reason per playbook
- 1 row whose company matches two CRM accounts -> ambiguous, ask human
- 1 row duplicated within the file itself
- Mixed header names, stray whitespace, inconsistent phone formats

`playbooks/event-leads.md` sections: Dedupe rules, Owner routing (by region or
company size), Follow-up policy (task due in 2 business days; email template
and tone rules), Approval policy, Escalation rules, Definitions of done.
