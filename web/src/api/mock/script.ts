/**
 * Scripted demo run for the mock adapter: the step graph of the Signal Summit
 * leads run (plans/02 "Demo data") and a timeline of real ledger event types
 * (plans/01 §3), including the four chaos faults and their recoveries.
 * Times are seconds from run start; the mock backend replays them.
 */
import type { Escalation, LedgerEvent, Skill, Step, StepKind } from '../types';
import { CRITERIA, GOAL, LEADS, MODELS, type LeadRow } from './fixtures';

export interface Beat {
  at: number;
  type: string;
  actor: string;
  step_id?: string | null;
  payload?: Record<string, unknown>;
  /** side effect on the mock backend (e.g. open an escalation) */
  effect?: 'escalate_sam' | 'agent_lost_b1' | 'finish';
}

export const SAM_STEP = 'stp_95';
export const B1 = 'worker-browser-1';
export const B2 = 'worker-browser-2';

const COL = { search: 0, write: 1, task: 2, draft: 3, send: 4, review: 5 } as const;
export const stepId = (n: number, col: keyof typeof COL) => `stp_${n}${COL[col]}`;

function mk(runId: string, id: string, kind: StepKind, skill: Skill, lane: string | null, title: string, inputs: Record<string, unknown>, check: string, args: Record<string, unknown>, deps: string[], extra: Partial<Step> = {}): Step {
  return {
    id, run_id: runId, kind, skill, lane, title, inputs, postcondition: { check, args, expect: {} }, depends_on: deps,
    side_effect: skill === 'browser.espocrm' || kind === 'email.send', idempotency_key: `${kind}:${String(args.email ?? lane ?? 'run')}`,
    status: 'planned', attempt: 0, max_attempts: 3, fence: 0, lease_owner: null, claim: null, verdict: null, history: [], ...extra,
  };
}

const usable = (l: LeadRow) => l.special !== 'dup_in_file';

export function planSteps(runId: string): { initial: Step[]; fanout: Step[] } {
  const initial = [
    mk(runId, 'stp_01', 'file.parse', 'file.parse', null, 'Parse event_attendees.csv', { file: 'event_attendees.csv' }, 'file.parsed_rows', { file: 'event_attendees.csv' }, []),
    mk(runId, 'stp_99', 'run.verify', 'review', null, 'Final verification', {}, 'run.criteria_met', {}, []),
  ];
  const fanout: Step[] = [];
  const sends: string[] = [];
  for (const l of LEADS.filter(usable)) {
    const lane = `lead:${l.n}`;
    const who = `${l.company ? l.company + ' · ' : ''}${l.name}`;
    const inp = { lead_ref: `fact:${lane}`, name: l.name, company: l.company, email: l.email || null, phone: l.phone ?? null };
    const search = stepId(l.n, 'search');
    fanout.push(mk(runId, search, 'crm.search_contact', 'browser.espocrm', lane, `search · ${who}`, inp, 'crm.lookup_matches', { email: l.email || l.phone }, ['stp_01']));
    let prev = search;
    if (l.special === 'fuzzy' || l.special === 'escalated' || l.special === 'skip') {
      const rv = stepId(l.n, 'review');
      fanout.push(mk(runId, rv, 'review.ambiguity', 'review', lane, `review · ${who}`, inp, 'review.decided', { lane }, [search]));
      prev = rv;
    }
    const writeKind: StepKind = l.write === 'update' ? 'crm.update_contact' : 'crm.create_contact';
    const w = stepId(l.n, 'write');
    fanout.push(mk(runId, w, writeKind, 'browser.espocrm', lane, `${l.write === 'update' ? 'update' : 'create'} · ${who}`, inp, l.write === 'update' ? 'crm.no_duplicate' : 'crm.contact_exists', { email: l.email || l.phone }, [prev]));
    const t = stepId(l.n, 'task');
    fanout.push(mk(runId, t, 'crm.create_task', 'browser.espocrm', lane, `task · ${who}`, inp, 'crm.task_exists', { email: l.email, due: '+2bd' }, [w]));
    if (l.email_out) {
      const d = stepId(l.n, 'draft');
      fanout.push(mk(runId, d, 'email.draft', 'email.draft', lane, `draft · ${who}`, inp, 'email.draft_valid', { to: l.email }, [t]));
      const s = stepId(l.n, 'send');
      fanout.push(mk(runId, s, 'email.send', 'email.send', lane, `send · ${who}`, inp, 'email.sent', { to: l.email }, ['stp_90']));
      sends.push(s);
    }
  }
  const drafts = fanout.filter((s) => s.kind === 'email.draft').map((s) => s.id);
  fanout.push(mk(runId, 'stp_90', 'review.approval', 'review', null, 'Approve follow-up emails', { drafts: drafts.length }, 'review.decided', { batch: 'emails' }, drafts));
  return { initial, fanout };
}

/** Builds the beats; `samAfter` true appends nothing for lane 9 (it waits on the human). */
export class ScriptBuilder {
  beats: Beat[] = [];
  private fence: Record<string, number> = {};

  add(at: number, type: string, actor: string, step_id: string | null, payload: Record<string, unknown> = {}, effect?: Beat['effect']) {
    this.beats.push({ at, type, actor, step_id, payload, effect });
  }

  /** ready → leased → observation → claimed → committed (+ fact). Returns end time. */
  flow(step: string, worker: string, start: number, dur: number, o: { obs?: string; claim?: string; check?: string; fact?: [string, unknown]; model?: string | null; attempt?: number } = {}): number {
    const fence = (this.fence[step] = (this.fence[step] ?? 0) + 1);
    this.add(start, 'step.ready', 'orchestrator', step, {});
    this.add(start + 0.4, 'step.leased', 'ledger', step, { worker, fence, attempt: o.attempt ?? fence, model: o.model === undefined ? MODELS.worker[0] : o.model });
    if (o.obs) this.add(start + dur * 0.6, 'step.observation', worker, step, { summary: o.obs });
    this.add(start + dur, 'step.claimed', worker, step, { summary: o.claim ?? 'done', fence, evidence: worker.startsWith('worker-browser') ? [`${step}_before.png`, `${step}_after.png`] : [] });
    this.add(start + dur + 1.1, 'step.committed', 'verifier', step, { check: o.check ?? '', reason: `${o.check ?? 'check'} ✓` });
    if (o.fact) this.add(start + dur + 1.2, 'fact.committed', 'verifier', step, { key: o.fact[0], value: o.fact[1] });
    return start + dur + 1.2;
  }

  nextFence(step: string) {
    return (this.fence[step] = (this.fence[step] ?? 0) + 1);
  }
}

export const SAM_ESCALATION = (runId: string): Escalation => ({
  id: `esc_${runId.slice(-4)}9`,
  run_id: runId,
  step_id: SAM_STEP,
  lane: 'lead:9',
  question: 'Sam Ito: which Lumen account?',
  context: 'Row 9 · sam@lumen.io · "Lumen" matches two CRM accounts. Only this lane waits; the rest continues.',
  options: [
    { label: 'Lumen Inc', value: 'acc/12', detail: 'acc/12 · EMEA · 40 contacts' },
    { label: 'Lumen Health', value: 'acc/31', detail: 'acc/31 · US · 6 contacts' },
    { label: 'Skip this lead', value: 'skip', detail: 'log reason' },
  ],
  tried: ['Email domain lumen.io is used by both accounts', 'No prior contact, deal or owner history for sam@lumen.io', 'Event badge lists "Lumen", no division'],
  confidence: 0.52,
  threshold: 0.8,
  status: 'open',
  created_at: Date.now(),
});

const browser = (i: number) => (i % 2 ? B2 : B1);

export function buildRunScript(runId: string): { initial: Step[]; fanout: Step[]; beats: Beat[]; duration: number } {
  const { initial, fanout } = planSteps(runId);
  const b = new ScriptBuilder();
  const lanes = LEADS.filter(usable);

  b.add(0, 'run.created', 'orchestrator', null, { goal: GOAL, input_file: 'event_attendees.csv' });
  b.add(6, 'run.understood', 'orchestrator', null, { summary: '6 success criteria derived', criteria: CRITERIA.map((c) => ({ ...c, status: 'pending' })) });
  b.add(12, 'plan.created', 'orchestrator', null, { summary: 'understand → plan → file.parse', steps: initial });
  b.flow('stp_01', 'worker-parser', 14, 5, { obs: '12 rows read · 4 header names mapped', claim: '12 rows, 2 flagged', check: 'file.parsed_rows', fact: ['file.rows', 12] });
  LEADS.forEach((l, i) => b.add(20.3 + i * 0.05, 'fact.committed', 'verifier', 'stp_01', { key: `lead:${l.n}`, value: { row: l.n, name: l.name, company: l.company, email: l.email, phone: l.phone ?? null, flagged: l.special === 'dup_in_file' ? 'duplicate of row 3' : l.special === 'skip' ? 'no email' : null } }));
  b.add(22, 'plan.revised', 'orchestrator', null, { summary: `fan-out: +${fanout.length} steps across ${lanes.length} lead lanes`, steps: fanout });

  // ---- search: two browser operators in parallel ---------------------------
  lanes.forEach((l, i) => {
    const t0 = 24 + Math.floor(i / 2) * 6.5;
    const found = l.write === 'update' ? (l.special === 'fuzzy' ? 'fuzzy: Benjamin Ortiz (0.86)' : 'exact match (email casing)') : l.special === 'escalated' ? '2 accounts match "Lumen"' : l.special === 'skip' ? 'no email · no match' : 'no match';
    b.flow(stepId(l.n, 'search'), browser(i), t0, 4.5, { obs: found, claim: found, check: 'crm.lookup_matches', fact: [`lead:${l.n}.match`, found] });
  });

  // ---- meta-reviewer ------------------------------------------------------------
  b.add(96, 'review.requested', 'orchestrator', stepId(7, 'review'), { summary: '3 ambiguous rows → queue:review' });
  b.add(96.2, 'review.requested', 'orchestrator', stepId(9, 'review'), {});
  b.add(96.4, 'review.requested', 'orchestrator', stepId(11, 'review'), {});
  b.add(100, 'review.resolved', 'meta-reviewer', stepId(7, 'review'), { title: 'Ben Ortiz matched to existing contact', decision: 'match_existing', label: 'Same person', value: 'con/88', confidence: 0.91, threshold: 0.8, evidence: ['Name similarity 0.86', 'Phone matches +1 415 555 0119', 'Company Quarry Data matches'], reason: 'Personal email, but phone + company match Benjamin Ortiz at Quarry Data.' });
  b.add(101, 'step.committed', 'verifier', stepId(7, 'review'), { check: 'review.decided', reason: 'decision fact exists (0.91 ≥ 0.80)' });
  b.add(102, 'review.resolved', 'meta-reviewer', stepId(11, 'review'), { title: 'Jo Park skipped', decision: 'skip', label: 'Skip', confidence: 0.88, threshold: 0.8, evidence: ['No email in the row', 'No company match in the CRM', 'Playbook § Dedupe: skip phone-only unless strategic'], reason: 'No email; playbook skips phone-only rows unless the account is strategic.' });
  b.add(103, 'step.committed', 'verifier', stepId(11, 'review'), { check: 'review.decided', reason: 'decision fact exists (0.88 ≥ 0.80)' });
  b.add(103.5, 'step.replanned', 'orchestrator', stepId(11, 'write'), { reason: 'lead skipped by meta-reviewer' });
  b.add(103.6, 'step.replanned', 'orchestrator', stepId(11, 'task'), { reason: 'lead skipped by meta-reviewer' });
  b.add(104, 'review.escalated', 'meta-reviewer', SAM_STEP, { title: 'Sam Ito: which Lumen account?', confidence: 0.52, threshold: 0.8, evidence: ['Email domain lumen.io is used by both accounts', 'No prior contact, deal or owner history', 'Event badge lists "Lumen", no division'], summary: 'lead:9 Sam Ito · confidence 0.52 < 0.80 → queue:human' }, 'escalate_sam');

  // ---- create / update, with the false-claim and kill-worker faults ----------
  const writers = lanes.filter((l) => l.write && l.special !== 'escalated');
  let tA = 40, tB = 44;
  const taskQueue: { n: number; ready: number }[] = [];
  for (const l of writers) {
    const step = stepId(l.n, 'write');
    const useA = tA <= tB;
    const start = Math.max(useA ? tA : tB, l.special === 'fuzzy' ? 104.5 : 0);
    const w = useA ? B1 : B2;
    let end: number;
    if (l.special === 'false_claim') {
      b.add(start - 0.6, 'fault.injected', 'chaos', null, { fault: 'false_claim', summary: 'false_claim armed on browser.espocrm' });
      const f1 = b.nextFence(step);
      b.add(start, 'step.ready', 'orchestrator', step, {});
      b.add(start + 0.4, 'step.leased', 'ledger', step, { worker: B2, fence: f1, attempt: 1, model: MODELS.worker[0] });
      b.add(start + 2, 'step.observation', B2, step, { summary: 'No form submit observed (fault: false claim injected).' });
      b.add(start + 3, 'step.claimed', B2, step, { summary: 'contact created', fence: f1, fault: true });
      b.add(start + 5, 'step.rejected', 'verifier', step, { check: 'crm.contact_exists', reason: `REST: 0 contacts with ${l.email}` });
      end = b.flow(step, B2, start + 8, 9, { obs: 'Prompt included the rejection reason · form saved', claim: 'contact created', check: 'crm.contact_exists', fact: [`lead:${l.n}.contact`, `crm/Contact/${(0x5f20 + l.n).toString(16)}`], attempt: 2 });
    } else if (l.special === 'takeover') {
      const f1 = b.nextFence(step);
      b.add(start, 'step.ready', 'orchestrator', step, {});
      b.add(start + 0.4, 'step.leased', 'ledger', step, { worker: B1, fence: f1, attempt: 1, model: MODELS.worker[0] });
      b.add(start + 3, 'step.observation', B1, step, { summary: 'Filled the Create Contact form' });
      b.add(start + 5, 'fault.injected', 'chaos', null, { fault: 'kill_worker', agent_id: B1, summary: `SIGKILL ${B1} (holding ${step})` }, 'agent_lost_b1');
      b.add(start + 14, 'step.heartbeat_lost', 'orchestrator', step, { worker: B1, summary: `${B1} · no heartbeat for 15s` });
      b.add(start + 14.2, 'agent.lost', 'orchestrator', null, { agent_id: B1, summary: `${B1} lost · container exited 137` });
      b.add(start + 20, 'step.lease_expired', 'orchestrator', step, { worker: B1, summary: `${step} → ready · fence ${f1} → ${f1 + 1}` });
      end = b.flow(step, B2, start + 20.5, 7, { obs: 'check-then-act: no existing contact → create', claim: 'contact created', check: 'crm.contact_exists', fact: [`lead:${l.n}.contact`, `crm/Contact/${(0x5f20 + l.n).toString(16)}`], attempt: 2 });
    } else {
      end = b.flow(step, w, start, 8, { obs: l.write === 'update' ? 'Existing contact enriched · screenshot' : `${l.name} saved · screenshot`, claim: l.write === 'update' ? 'contact updated' : 'contact created', check: l.write === 'update' ? 'crm.no_duplicate' : 'crm.contact_exists', fact: [`lead:${l.n}.contact`, `crm/Contact/${(0x5f20 + l.n).toString(16)}`] });
    }
    if (l.special === 'takeover' || w === B1) tA = l.special === 'takeover' ? end + 200 : end + 0.5;
    else tB = end + 0.5;
    if (l.special === 'false_claim' || l.special === 'takeover') tB = end + 0.5;
    taskQueue.push({ n: l.n, ready: end + 0.3 });
  }

  // ---- follow-up tasks (browser operator 2; session-expiry fault on Grace) ----
  let tt = 120;
  for (const q of taskQueue.sort((a, b2) => a.ready - b2.ready)) {
    const l = LEADS.find((x) => x.n === q.n)!;
    const step = stepId(l.n, 'task');
    const start = Math.max(tt, q.ready);
    if (l.special === 'session') {
      b.add(start - 0.5, 'fault.injected', 'chaos', null, { fault: 'expire_session', summary: 'CRM session invalidated' });
      b.add(start, 'step.ready', 'orchestrator', step, {});
      const f = b.nextFence(step);
      b.add(start + 0.4, 'step.leased', 'ledger', step, { worker: B2, fence: f, attempt: 1, model: MODELS.worker[0] });
      b.add(start + 2, 'step.observation', B2, step, { summary: 'redirected to /login', fault: true });
      b.add(start + 4, 'step.observation', B2, step, { summary: 're-authenticated · resuming the same step' });
      b.add(start + 8, 'step.claimed', B2, step, { summary: 'task created', fence: f });
      b.add(start + 9.1, 'step.committed', 'verifier', step, { check: 'crm.task_exists', reason: 'task linked · due +2 business days' });
      tt = start + 9.6;
    } else {
      tt = b.flow(step, B2, start, 7, { obs: 'Task saved · due in 2 business days', claim: 'task created', check: 'crm.task_exists', fact: [`lead:${l.n}.task`, `crm/Task/${(0x9d00 + l.n).toString(16)}`] }) + 0.4;
    }
  }

  // ---- drafts (model-outage fault) -------------------------------------------
  let td = 190;
  const drafted = lanes.filter((l) => l.email_out && l.special !== 'escalated');
  drafted.forEach((l) => {
    const step = stepId(l.n, 'draft');
    if (l.special === 'outage') {
      b.add(td - 0.5, 'fault.injected', 'chaos', null, { fault: 'model_outage', summary: 'primary worker model → 503' });
      const f = b.nextFence(step);
      b.add(td, 'step.ready', 'orchestrator', step, {});
      b.add(td + 0.4, 'step.leased', 'ledger', step, { worker: 'worker-drafter', fence: f, attempt: 1, model: MODELS.worker[0] });
      b.add(td + 2.4, 'model.fallback', 'worker-drafter', step, { role: 'worker', from: MODELS.worker[0], to: MODELS.worker[1], error: '503' });
      b.add(td + 6, 'step.claimed', 'worker-drafter', step, { summary: 'draft ready', fence: f });
      b.add(td + 7.1, 'step.committed', 'verifier', step, { check: 'email.draft_valid', reason: 'merge fields ✓ · tone judge 0.96' });
      td += 7.6;
    } else {
      td = b.flow(step, 'worker-drafter', td, 5, { claim: 'draft ready', check: 'email.draft_valid', fact: [`lead:${l.n}.draft`, `Good to meet you at Signal Summit, ${l.name.split(' ')[0]}`] }) + 0.3;
    }
  });

  // ---- approval + send -----------------------------------------------------------
  const ta = Math.max(td + 1, 244);
  b.add(ta, 'review.requested', 'orchestrator', 'stp_90', { summary: `${drafted.length} drafts → approval` });
  b.add(ta + 4, 'approval.auto', 'meta-reviewer', 'stp_90', { title: `${drafted.length} follow-up emails approved`, decision: 'approve', label: `Approve all ${drafted.length}`, confidence: 0.94, threshold: 0.9, evidence: ['Merge fields complete, no placeholders', `Judge min 0.94 across ${drafted.length} drafts`, 'No policy flags (pricing, promises, attachments)'], reason: 'Tone judge min 0.94, no policy flags. Playbook § Approval allows auto at ≥ 0.90.' });
  b.add(ta + 5, 'step.committed', 'verifier', 'stp_90', { check: 'review.decided', reason: 'approval fact exists (0.94 ≥ 0.90)' });
  b.add(ta + 5.1, 'fact.committed', 'verifier', 'stp_90', { key: 'approval:emails', value: `${drafted.length} of ${drafted.length} (auto)` });
  let ts = ta + 6;
  drafted.forEach((l) => {
    ts = b.flow(stepId(l.n, 'send'), 'worker-mailer', ts, 1.6, { obs: 'SMTP 250 OK', claim: 'sent', check: 'email.sent', model: null, fact: [`lead:${l.n}.email`, 'sent'] }) + 0.2;
  });
  b.add(ts + 1, 'step.ready', 'orchestrator', 'stp_99', { summary: 'final sweep waits on lane lead:9' });
  b.add(ts + 2, 'run.completed_pending_input', 'orchestrator', null, { summary: '1 escalation open (Sam Ito); every other criterion verified' }, 'finish');

  b.beats.sort((x, y) => x.at - y.at);
  return { initial, fanout, beats: b.beats, duration: ts + 2 };
}

/** Lane 9 after the human answers (relative times). */
export function samLaneBeats(answer: string, label: string): Beat[] {
  const b = new ScriptBuilder();
  b.add(0, 'input.answered', 'human', SAM_STEP, { answer, label, summary: `Sam Ito → ${label}` });
  b.add(1.2, 'step.committed', 'verifier', SAM_STEP, { check: 'review.decided', reason: 'human answer committed as a fact' });
  const sam = LEADS.find((l) => l.n === 9)!;
  if (answer === 'skip') {
    b.add(2, 'step.replanned', 'orchestrator', stepId(9, 'write'), { reason: 'skipped by you' });
    b.add(2.1, 'step.replanned', 'orchestrator', stepId(9, 'task'), { reason: 'skipped by you' });
    b.add(2.2, 'step.replanned', 'orchestrator', stepId(9, 'draft'), { reason: 'skipped by you' });
    b.add(2.3, 'step.replanned', 'orchestrator', stepId(9, 'send'), { reason: 'skipped by you' });
  } else {
    let t = b.flow(stepId(9, 'write'), B2, 2, 6, { obs: `linked to ${label}`, claim: 'contact created', check: 'crm.contact_exists', fact: ['lead:9.contact', 'crm/Contact/5f29'] });
    t = b.flow(stepId(9, 'task'), B2, t + 0.5, 5, { claim: 'task created', check: 'crm.task_exists' });
    t = b.flow(stepId(9, 'draft'), 'worker-drafter', t + 0.5, 4, { claim: 'draft ready', check: 'email.draft_valid' });
    b.add(t + 0.5, 'approval.auto', 'meta-reviewer', null, { title: `Email to ${sam.email} approved`, decision: 'approve', confidence: 0.95, threshold: 0.9, evidence: ['Judge 0.95', 'No policy flags'] });
    b.flow(stepId(9, 'send'), 'worker-mailer', t + 1.5, 1.5, { obs: 'SMTP 250 OK', claim: 'sent', check: 'email.sent', model: null });
  }
  const end = Math.max(...b.beats.map((x) => x.at)) + 1;
  b.flow('stp_99', 'verifier', end, 3, { claim: 'all criteria checked', check: 'run.criteria_met', model: MODELS.verifier[0] });
  b.add(end + 5, 'run.completed', 'verifier', null, { summary: 'run.criteria_met ✓ all 6' }, 'finish');
  b.beats.sort((x, y) => x.at - y.at);
  return b.beats;
}

export function toEvent(runId: string, beat: Beat, ts: number, seq: number): LedgerEvent {
  return { id: `${ts}-${seq}`, ts, run_id: runId, step_id: beat.step_id ?? null, actor: beat.actor, type: beat.type, payload: beat.payload ?? {} };
}
