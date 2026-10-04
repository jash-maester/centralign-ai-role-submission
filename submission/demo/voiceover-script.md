# Ledger demo: voiceover script

Read it straight through as one take, in your own voice. Pause for a breath (about one
second) between paragraphs: each paragraph matches one section of the video, and the
video is fitted to your timing at those pauses. Record as `voiceover.m4a` (any format is fine).

**01 · Hook**  _(on screen: Title card: "Agents can't mark their own homework." then the Ledger title)_

Hi, I'm Jash. This is Ledger, a prototype I built for the CentrAlign founding engineer problem. It's really about one question: how should AI agents communicate and coordinate, so they can finish real work without a human watching every step?

**02 · One line in**  _(on screen: Builder: the one-line goal typed, Run pressed, agents animating around the ledger)_

Let me show you. I give it one line: add the leads from yesterday's event to the CRM and set up follow-ups. That's it. Everything I didn't say, like how to dedupe, who owns which lead, and what needs approval, comes from a playbook, which is basically our company's SOP.

**03 · One shared ledger**  _(on screen: Dashboard: success criteria, steps progressing, live ledger events streaming)_

Here's the core design. My agents never talk to each other directly. There's no chat between them at all. They coordinate through one shared ledger in Redis. The orchestrator writes steps to it, workers pick them up by skill, and every single change is recorded as an event.

**04 · A claim is not a fact**  _(on screen: Animated insert: step goes leased, amber claimed, verifier checks, green committed)_

And the most important rule on that ledger is that a claim is not a fact. A worker can only say "I think I'm done." That stays amber until a separate verifier checks the real CRM through its API, while the worker used the browser. Only the verifier can turn it green.

**05 · False claim, caught**  _(on screen: Real footage: False claim fired; zoom on step.rejected in the live ledger; step drawer with both attempts)_

So let me break it on purpose. I'll inject a fault, so the next browser step says it's done without actually doing anything. The verifier checks the CRM, finds no contact, and rejects the claim with a reason. The worker retries with that reason, and this time it really commits.

**06 · Kill a worker**  _(on screen: Animated lease and fencing insert, then real footage: worker lost, lease_expired, worker-browser-2 takes over with token 2)_

Now something harsher. I'll kill one of the browser workers right in the middle of a step. Every step is held under a lease, with a heartbeat and a fencing token. When the heartbeat stops, the lease expires, the step goes back on the queue, and the other worker picks it up with a higher token. No duplicates, because every worker checks before it writes.

**07 · Humans by exception**  _(on screen: Needs your attention: Handled automatically list, Sam Ito escalation, 0.52 < 0.80, Lumen Inc picked, Save as playbook rule, Commit)_

So where do I come in? Only where the system genuinely isn't sure. A meta-reviewer agent handles the judgment calls. It matched Ben Ortiz to an existing contact and skipped Jo Park, who had no email, because both were above its confidence threshold. Sam Ito is different: "Lumen" matches two accounts in the CRM, and it's only fifty-two percent sure. So that one comes to me. I pick the account, save it as a rule, and next time it won't need to ask.

**08 · Outcome**  _(on screen: Mailpit inbox, then outcome tiles: 6 created, 3 updated, 2 skipped, 1 for a human)_

Emails only go out once there's an approval on record, and the verifier confirms each one actually arrived. So from twelve messy rows, I end up with six contacts created, three updated, two skipped with a reason, and exactly one question for me.

**09 · Evidence report**  _(on screen: Report: headline, every row, decisions, what went wrong and how it recovered, reproduce block)_

At the end I get an evidence report: what happened to every row, who decided it, how it was checked, and every failure next to how it recovered. I also ran this end to end on live free models; this recording replays recorded answers so it's exactly reproducible.

**10 · Same substrate**  _(on screen: End card: new workflow = new playbook + connectors; thesis)_

And if I want a new workflow, I write a new playbook and connectors. The coordination layer stays the same. That's Ledger. Thanks for watching.

_About 504 words, roughly 3 to 3.5 minutes at a relaxed pace._