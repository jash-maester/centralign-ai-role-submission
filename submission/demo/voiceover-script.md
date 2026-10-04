# Ledger demo: voiceover script

Read one block per segment. Pace: calm and clear (about 2.3 words per second). Each block's
duration is fixed; the video is cut to these timings. Record either one continuous take or one
file per segment named `01.m4a`, `02.m4a`, ... (per-segment gives the tightest sync).

| # | Time | Segment | On screen |
|---|---|---|---|
| 01 | 0:00–0:16 (16s) | Hook | Title card: "Agents can't mark their own homework." then the Ledger title |
| 02 | 0:16–0:33 (17s) | One line in | Builder: the one-line goal typed, Run pressed, agents animating around the ledger |
| 03 | 0:33–0:52 (19s) | One shared ledger | Dashboard: success criteria, steps progressing, live ledger events streaming |
| 04 | 0:52–1:13 (21s) | A claim is not a fact | Animated insert: step goes leased, amber claimed, verifier checks, green committed |
| 05 | 1:13–1:33 (20s) | False claim, caught | Real footage: False claim fired; zoom on step.rejected in the live ledger; step drawer with both attempts |
| 06 | 1:33–1:58 (25s) | Kill a worker | Animated lease and fencing insert, then real footage: worker lost, lease_expired, worker-browser-2 takes over with token 2 |
| 07 | 1:58–2:30 (32s) | Humans by exception | Needs your attention: Handled automatically list, Sam Ito escalation, 0.52 < 0.80, Lumen Inc picked, Save as playbook rule, Commit |
| 08 | 2:30–2:47 (17s) | Outcome | Mailpit inbox, then outcome tiles: 6 created, 3 updated, 2 skipped, 1 for a human |
| 09 | 2:47–3:08 (21s) | Evidence report | Report: headline, every row, decisions, what went wrong and how it recovered, reproduce block |
| 10 | 3:08–3:20 (12s) | Same substrate | End card: new workflow = new playbook + connectors; thesis |

Total: 3:20

## 01 · Hook · 16 s (33 words)

_On screen: Title card: "Agents can't mark their own homework." then the Ledger title_

> Today's AI agents can't be trusted to finish real work on their own. They're unreliable, they can't prove they're done, and they act on ambiguous requests. Ledger fixes that with state, not prompts.

## 02 · One line in · 17 s (36 words)

_On screen: Builder: the one-line goal typed, Run pressed, agents animating around the ledger_

> Here's the job: add the leads from yesterday's event to the CRM and set up follow-ups. One line. A playbook, our company's SOP, supplies everything unstated: dedupe rules, owner routing, follow-up policy, and what needs approval.

## 03 · One shared ledger · 19 s (41 words)

_On screen: Dashboard: success criteria, steps progressing, live ledger events streaming_

> The orchestrator turns that line into checkable success criteria and a plan of steps. Every agent works off one shared ledger in Redis. Agents never read each other's chat, only facts that have been committed. Every state change is an event.

## 04 · A claim is not a fact · 21 s (46 words)

_On screen: Animated insert: step goes leased, amber claimed, verifier checks, green committed_

> Here's the core idea: a claim is not a fact. A worker can only say it's done. That claim stays amber until an independent verifier checks the real CRM, through its REST API while the workers act through the browser, and only then commits it green.

## 05 · False claim, caught · 20 s (43 words)

_On screen: Real footage: False claim fired; zoom on step.rejected in the live ledger; step drawer with both attempts_

> Let's prove it. I inject a fault: the next browser step claims success without doing anything. The verifier queries the CRM, finds no contact, and rejects it with a reason. The retry gets that reason in its prompt, and this time it commits.

## 06 · Kill a worker · 25 s (53 words)

_On screen: Animated lease and fencing insert, then real footage: worker lost, lease_expired, worker-browser-2 takes over with token 2_

> Now I kill a browser worker in the middle of a step. Every step is held under a lease, with a heartbeat and a fencing token. The lease expires, the reaper puts the step back, and the second worker takes over with token two. Zero duplicates, because every write checks before it acts.

## 07 · Humans by exception · 32 s (70 words)

_On screen: Needs your attention: Handled automatically list, Sam Ito escalation, 0.52 < 0.80, Lumen Inc picked, Save as playbook rule, Commit_

> Humans are the last agent on the bus. A meta-reviewer decides ambiguous cases on its own when its confidence clears a threshold: Ben Ortiz is a match, Jo Park is skipped. Sam Ito is different. Lumen matches two CRM accounts, confidence point five two, below point eight. So only that one comes to me. I pick the account and save it as a playbook rule, so next time it's automatic.

## 08 · Outcome · 17 s (36 words)

_On screen: Mailpit inbox, then outcome tiles: 6 created, 3 updated, 2 skipped, 1 for a human_

> Emails go out only with an approval on record, and the verifier confirms each one in the inbox. Twelve messy rows in: six created, three updated, two skipped with reasons, and one question for a human.

## 09 · Evidence report · 21 s (45 words)

_On screen: Report: headline, every row, decisions, what went wrong and how it recovered, reproduce block_

> Everything ends in an evidence report: every row, who decided it, how it was verified, and every failure paired with its recovery. This same run also completed on live free models, twenty-eight live calls, same outcome. The recording uses recorded answers so it's exactly reproducible.

## 10 · Same substrate · 12 s (25 words)

_On screen: End card: new workflow = new playbook + connectors; thesis_

> New workflow? Write a new playbook and connectors; the substrate stays the same. Autonomy is a state-management problem, not a prompting problem. Thanks for watching.
