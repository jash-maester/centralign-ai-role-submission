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

## E. Review and human in the loop (by exception)

| #   | Feature                                   | P  | Acceptance                                                              |
|-----|-------------------------------------------|----|-------------------------------------------------------------------------|
| E1  | Approval policy from playbook -> review   | P0 | External email send always creates a review step; meta-reviewer auto-approves when judge >= 0.90 and no policy flags |
| E2  | Automatic ambiguity resolution            | P0 | Ambiguous rows go to the meta-reviewer; resolved automatically when confidence >= 0.80 |
| E3  | Escalation only below threshold           | P0 | Only unresolved items reach `queue:human`, batched, with options and evidence |
| E4  | Non-blocking escalations                  | P1 | Unrelated steps proceed while an escalation is open                     |
| E5  | Answer in GUI, optional save as rule      | P1 | Answer becomes a committed fact; "save as rule" appends to the playbook |
| E6  | Auto-decision audit trail                 | P1 | Every auto decision lists confidence, evidence and model in the report  |

## F. Self-healing and chaos

| #   | Feature                                   | P  | Acceptance                                                              |
|-----|-------------------------------------------|----|-------------------------------------------------------------------------|
| F1  | Kill worker -> takeover                   | P0 | `make chaos-kill-browser` mid-run; run completes; zero duplicates       |
| F2  | False-claim fault                         | P0 | Worker claims done without acting; verifier rejects; retry succeeds     |
| F3  | Expired CRM session fault                 | P1 | Operator observes login page, re-authenticates, continues               |
| F4  | Model outage fault                        | P1 | Bad primary model id -> `model.fallback` -> step completes              |
| F5  | UI-changed fault                          | P2 | Selector broken -> replan to API skill                                  |
| F6  | Determinism control                       | P1 | One slider sets per-role temperature + seed; at 1.0 two runs produce the same plan |
| F7  | Run config (01 §6a)                       | P1 | Every key is honoured by the service that owns it; changes emit `run.config_updated` and apply from the next attempt |
| F8  | Spend cap + free-model budget             | P0 | Cap reached or daily budget used → no new LLM calls, clear event, run fails with reason; cache hits cost nothing |
| F9  | Replay                                    | P2 | `make replay RUN=` reproduces the plan from cache with zero live LLM calls |

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
| H5  | Web: "Needs your attention"               | P1 | Shows escalations only, inline answer, plus "handled automatically" log |
| H6  | Web: evidence report view                 | P1 | Screenshots inline                                                      |
| H7  | Web: run controls                         | P1 | Fault buttons F1-F4 + determinism slider                                |
| H8  | Web: step detail drawer                   | P0 | Inputs, postcondition, every attempt with verdict + reason, screenshots |
| H9  | Web: agent prompt + injections            | P1 | Edit/version system prompt; toggle layers (history layer locked)        |
| H10 | Web: agent tools (API / MCP / function)   | P1 | List, toggle and add tools per agent; changes emit events               |
| H11 | Web: live sandboxed shell per agent       | P1 | WS exec into the agent container; shows dead containers honestly        |
| H12 | Web: builder canvas                       | P2 | Node graph of agents + ledger; wiring edits; animated message flow      |
| H13 | Web: run controls panel                   | P1 | All 01 §6a keys editable with live hints (e.g. derived temperatures, $ spent of cap); "Reset to playbook defaults" |
| H14 | Web: goal entry                           | P1 | Builder goal field + Run submits `POST /runs` with the attached file (video 0:15) |

## I. Packaging

| #   | Feature                                   | P  | Acceptance                                                              |
|-----|-------------------------------------------|----|-------------------------------------------------------------------------|
| I1  | `docker compose up` brings up everything  | P0 | Fresh clone + `.env` + `make up && make seed && make demo` works        |
| I2  | Healthchecks and ordered startup          | P0 | No manual waiting or restarts                                           |
| I3  | Seed script for CRM                       | P0 | Idempotent; creates users, API key, existing contacts                   |
| I4  | Test suite in container                   | P1 | `make test` green                                                       |
| I5  | README with one-command run + GIF         | P1 |                                                                         |

## Demo data

The Report design fixes the exact dataset. Event: **Signal Summit**. Owners
(seeded EspoCRM users): `a.chen`, `r.silva`. Expected outcome per row:

| #  | Lead          | Company / email                          | Expected outcome                          | Owner   |
|----|---------------|------------------------------------------|-------------------------------------------|---------|
| 1  | Priya Raman   | Northwind · priya@northwind.com          | created                                   | a.chen  |
| 2  | Marcus Lee    | Acme Corp · MARCUS.LEE@ACME.COM          | updated (exact dup, email casing)         | r.silva |
| 3  | Lena Fischer  | Kestrel Labs · lena@kestrel-labs.io      | created                                   | r.silva |
| 4  | Dana Okafor   | Helix Bio · dana@helixbio.com            | created (false-claim demo, 2 tries)       | a.chen  |
| 5  | Tom Becker    | Orbital Freight · tom.becker@orbitalfreight.com | created (kill-worker demo, takeover) | r.silva |
| 6  | Lena Fischer  | duplicate of row 3 in the file           | skipped by parser                         | —       |
| 7  | Ben Ortiz     | Quarry Data · ben.ortiz@gmail.com        | updated (fuzzy → seeded Benjamin Ortiz, phone +1 415 555 0119, 0.91); **no email: open deal** | a.chen |
| 8  | Hannah Cole   | Fieldstone · hannah@fieldstone.dev       | updated (exact dup, casing)               | a.chen  |
| 9  | Sam Ito       | Lumen · sam@lumen.io                     | escalated: Lumen Inc (EMEA, 40 contacts) vs Lumen Health (US, 6), 0.52 | — |
| 10 | Omar Haddad   | Brightline · omar@brightline.co          | created                                   | a.chen  |
| 11 | Jo Park       | no email · +1 415 555 0182               | skipped by meta-reviewer (0.88)           | —       |
| 12 | Grace Wu      | Tallgrass · grace.wu@tallgrass.com       | created                                   | r.silva |

Totals: 6 created, 3 updated, 2 skipped, 1 waiting; 8 emails (rows
1,2,3,4,5,8,10,12). `crm_seed.json` must contain Marcus Lee, Hannah Cole,
Benjamin Ortiz (with an open deal), and the two Lumen accounts. The playbook's
owner-routing rule must reproduce the owner column above.

The same dataset described by category, deliberately messy:
- 6 clean new leads
- 2 exact duplicates of seeded CRM contacts (same email, different casing)
- 1 fuzzy duplicate (same person, personal email, company matches) -> meta-reviewer matches (0.91)
- 1 row with no email (phone only) -> meta-reviewer skips with reason per playbook (0.88)
- 1 row whose company matches two CRM accounts -> meta-reviewer unsure (0.52) -> escalated to human
- 1 row duplicated within the file itself
- Mixed header names, stray whitespace, inconsistent phone formats

`playbooks/event-leads.md` sections: Dedupe rules, Owner routing (by region or
company size), Follow-up policy (task due in 2 business days; email template
and tone rules; **no automated email to a contact with an open deal**, the
owner follows up), Approval policy (auto-approve thresholds), Escalation rules
(what the meta-reviewer may decide), Definitions of done. Playbook front
matter holds the run-config defaults (01 §6a).
