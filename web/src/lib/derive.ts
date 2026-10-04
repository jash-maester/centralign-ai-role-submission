/** Pure view-model derivations from store data (unit tested). */
import type { AgentStatus, Escalation, Fact, LedgerEvent, Run, RunConfig, Step, StepKind } from '../api/types';
import { stepVisual, VISUAL_COLOR, type Visual } from './states';

// ---- leads and lanes -------------------------------------------------------

export interface LeadInfo {
  n: number | null;
  name: string;
  company: string;
  email: string;
  phone?: string;
}

type Obj = Record<string, unknown>;
const isObj = (x: unknown): x is Obj => typeof x === 'object' && x !== null && !Array.isArray(x);
const s = (x: unknown) => (typeof x === 'string' ? x : x == null ? '' : String(x));

export function laneNumber(lane: string | null | undefined): number | null {
  const m = /(\d+)\s*$/.exec(lane ?? '');
  return m ? Number(m[1]) : null;
}

/** Lead details for a lane from the committed `lead:N` fact, else step inputs/title. */
export function leadInfo(lane: string, laneSteps: Step[], facts: Fact[]): LeadInfo {
  const fact = facts.find((f) => f.key === lane);
  const v: Obj = isObj(fact?.value) ? (fact!.value as Obj) : {};
  const inp: Obj = laneSteps.map((x) => x.inputs ?? {}).find((i) => i.name || i.email || i.company) ?? {};
  const pick = (k: string) => s(v[k] ?? inp[k]);
  let name = pick('name') || [pick('first_name'), pick('last_name')].filter(Boolean).join(' ');
  let company = pick('company');
  if (!name) {
    const t = laneSteps.find((x) => x.title)?.title ?? '';
    const parts = t.split('·').map((x) => x.trim());
    name = parts.length > 1 ? parts[parts.length - 1] : '';
    if (!company && parts.length > 2) company = parts[parts.length - 2];
  }
  return { n: laneNumber(lane), name: name || lane, company, email: pick('email'), phone: pick('phone') || undefined };
}

export const LANE_COLUMNS = ['search', 'write', 'task', 'draft', 'send'] as const;
export type LaneColumn = (typeof LANE_COLUMNS)[number];
const COLUMN_KINDS: Record<LaneColumn, StepKind[]> = {
  search: ['crm.search_contact', 'review.ambiguity', 'human.decide'],
  write: ['crm.create_contact', 'crm.update_contact'],
  task: ['crm.create_task'],
  draft: ['email.draft'],
  send: ['email.send'],
};

export interface LaneCell { step: Step | null; label: string; c: string; v: Visual }
export interface Lane {
  lane: string;
  lead: LeadInfo;
  cells: Record<LaneColumn, LaneCell>;
  steps: Step[];
  tries: number;
  holder: string | null;
  category: 'active' | 'input' | 'done' | 'waiting';
}

const ACTIVE: Step['status'][] = ['leased', 'claimed_done', 'verified', 'review_required'];

function cellFor(col: LaneColumn, laneSteps: Step[]): LaneCell {
  const cands = laneSteps.filter((x) => COLUMN_KINDS[col].includes(x.kind));
  if (!cands.length) return { step: null, label: '—', c: 'var(--fg3)', v: 'idle' };
  // Search column: an open review/escalation for the lane wins over the committed search.
  const open = cands.find((x) => x.kind !== 'crm.search_contact' && x.status !== 'committed' && x.status !== 'replanned');
  const step = open ?? cands[cands.length - 1];
  const vis = stepVisual(step.status, { skipped: step.status === 'replanned' });
  return { step, label: vis.label, c: vis.c, v: vis.v };
}

export function buildLanes(steps: Record<string, Step>, order: string[], facts: Fact[]): Lane[] {
  const byLane = new Map<string, Step[]>();
  for (const id of order) {
    const st = steps[id];
    if (!st?.lane) continue;
    if (!byLane.has(st.lane)) byLane.set(st.lane, []);
    byLane.get(st.lane)!.push(st);
  }
  const lanes: Lane[] = [];
  for (const [lane, ls] of byLane) {
    const cells = Object.fromEntries(LANE_COLUMNS.map((c) => [c, cellFor(c, ls)])) as Record<LaneColumn, LaneCell>;
    const holderStep = ls.find((x) => x.lease_owner && ACTIVE.includes(x.status));
    const input = ls.some((x) => x.status === 'input_required');
    const active = ls.some((x) => ACTIVE.includes(x.status));
    const done = ls.every((x) => ['committed', 'replanned', 'dead'].includes(x.status));
    lanes.push({
      lane,
      lead: leadInfo(lane, ls, facts),
      cells,
      steps: ls,
      tries: Math.max(1, ...ls.map((x) => x.attempt || 0)),
      holder: holderStep?.lease_owner ?? (input ? 'human' : null),
      category: input ? 'input' : active ? 'active' : done ? 'done' : 'waiting',
    });
  }
  return lanes.sort((a, b) => (a.lead.n ?? 1e9) - (b.lead.n ?? 1e9));
}

// ---- progress + counters ----------------------------------------------------

export function progressCounts(steps: Record<string, Step>): { v: Visual; label: string; n: number; c: string }[] {
  const counts: Partial<Record<Visual, number>> = {};
  for (const st of Object.values(steps)) {
    const { v } = stepVisual(st.status, { skipped: st.status === 'replanned' });
    counts[v] = (counts[v] ?? 0) + 1;
  }
  const order: [Visual, string][] = [['committed', 'committed'], ['claimed', 'verifying'], ['leased', 'leased'], ['input', 'escalated'], ['rejected', 'rejected'], ['dead', 'dead'], ['ready', 'ready'], ['planned', 'planned'], ['skipped', 'skipped']];
  return order.map(([v, label]) => ({ v, label, n: counts[v] ?? 0, c: VISUAL_COLOR[v] })).filter((x) => x.n > 0);
}

export interface Counters { retries: number; takeovers: number; fallbacks: number; replans: number; faults: number }
export function counters(events: LedgerEvent[]): Counters {
  const c: Counters = { retries: 0, takeovers: 0, fallbacks: 0, replans: 0, faults: 0 };
  for (const e of events) {
    if (e.type === 'step.rejected') c.retries++;
    else if (e.type === 'step.lease_expired') c.takeovers++;
    else if (e.type === 'model.fallback') c.fallbacks++;
    else if (e.type === 'plan.revised' || e.type === 'step.replanned') c.replans++;
    else if (e.type === 'fault.injected') c.faults++;
  }
  return c;
}

export function committedCount(steps: Record<string, Step>): { done: number; total: number } {
  const all = Object.values(steps);
  return { done: all.filter((x) => x.status === 'committed' || x.status === 'replanned').length, total: all.length };
}

export function elapsedS(run: Run | null, now: number): number {
  if (!run?.created_at) return 0;
  return Math.max(0, Math.round(((run.finished_at ?? now) - run.created_at) / 1000));
}

export function fmtDuration(sec: number): string {
  const m = Math.floor(sec / 60), ss = Math.floor(sec % 60);
  return `${String(m).padStart(2, '0')}:${String(ss).padStart(2, '0')}`;
}

export function fmtClock(ts: number): string {
  const d = new Date(ts);
  return [d.getHours(), d.getMinutes(), d.getSeconds()].map((x) => String(x).padStart(2, '0')).join(':');
}

// ---- events: one-line payload ----------------------------------------------

export function eventLine(e: LedgerEvent): string {
  const p = e.payload ?? {};
  const parts: string[] = [];
  const text = p.summary ?? p.message ?? p.reason ?? p.title ?? p.question ?? p.detail;
  if (e.step_id) parts.push(e.step_id);
  if (typeof text === 'string' && text) parts.push(text);
  else {
    for (const [k, v] of Object.entries(p)) {
      if (parts.length >= 4) break;
      if (v == null || typeof v === 'object') continue;
      parts.push(`${k}=${typeof v === 'number' && !Number.isInteger(v) ? v.toFixed(2) : String(v)}`);
    }
  }
  if (e.type === 'step.leased' && (p.worker || e.actor)) parts.push(`→ ${s(p.worker ?? e.actor)}${p.fence != null ? ` · token ${s(p.fence)}` : ''}`);
  return parts.join(' · ');
}

// ---- decisions (meta-reviewer) ----------------------------------------------

export interface Decision {
  key: string;
  kind: 'ambiguity' | 'approval';
  title: string;
  confidence: number;
  threshold: number; // threshold recorded when decided
  evidence: string[];
  choice: string | null;
  by: string; // meta-reviewer | you | —
  escalated: boolean;
  forced: string | null;
  ts: number;
}

const num = (x: unknown, d = 0) => (typeof x === 'number' ? x : Number.isFinite(Number(x)) ? Number(x) : d);
const strList = (x: unknown) => (Array.isArray(x) ? x.map(s) : []);

/** Every meta-reviewer decision in the run, latest state per review step. */
export function decisions(events: LedgerEvent[], escalations: Escalation[]): Decision[] {
  const out = new Map<string, Decision>();
  for (const e of events) {
    if (!['review.resolved', 'approval.auto', 'review.escalated', 'input.answered'].includes(e.type)) continue;
    const p = e.payload ?? {};
    const key = e.step_id ?? s(p.lane) ?? `${e.type}:${e.ts}`;
    const prev = out.get(key);
    if (e.type === 'input.answered') {
      if (prev) out.set(key, { ...prev, choice: s(p.label ?? p.answer ?? p.value) || 'answered', by: 'you', escalated: false });
      continue;
    }
    const kind: Decision['kind'] = e.type === 'approval.auto' || p.kind === 'approval' || p.decision === 'approve' ? 'approval' : 'ambiguity';
    out.set(key, {
      key,
      kind,
      title: s(p.title ?? p.question ?? p.summary) || `${kind === 'approval' ? 'Approval' : 'Ambiguity'} ${e.step_id ?? ''}`.trim(),
      confidence: num(p.confidence),
      threshold: num(p.threshold, kind === 'approval' ? 0.9 : 0.8),
      evidence: strList(p.evidence ?? p.tried),
      choice: e.type === 'review.escalated' ? null : s(p.label ?? p.choice ?? p.value ?? p.decision) || 'decided',
      by: e.type === 'review.escalated' ? '—' : e.actor || 'meta-reviewer',
      escalated: e.type === 'review.escalated',
      forced: p.forced ? s(p.forced) : null,
      ts: e.ts,
    });
  }
  for (const esc of escalations) {
    if (out.has(esc.step_id)) continue;
    out.set(esc.step_id, {
      key: esc.step_id, kind: 'ambiguity', title: esc.question, confidence: esc.confidence, threshold: esc.threshold,
      evidence: esc.tried ?? [], choice: null, by: '—', escalated: true, forced: null, ts: esc.created_at ?? 0,
    });
  }
  return [...out.values()].sort((a, b) => a.ts - b.ts);
}

/** Live hint: how many decisions would auto-resolve at the given thresholds. */
export function autoAt(ds: Decision[], cfg: Pick<RunConfig, 'review_auto_threshold' | 'approval_auto_threshold' | 'always_ask_human_email' | 'llm_judge_enabled'>) {
  const amb = ds.filter((d) => d.kind === 'ambiguity');
  const appr = ds.filter((d) => d.kind === 'approval');
  const ambAuto = amb.filter((d) => d.confidence >= cfg.review_auto_threshold).length;
  const apprForced = cfg.always_ask_human_email || !cfg.llm_judge_enabled;
  const apprAuto = apprForced ? 0 : appr.filter((d) => d.confidence >= cfg.approval_auto_threshold).length;
  const minJudge = appr.length ? Math.min(...appr.map((d) => d.confidence)) : null;
  return { ambTotal: amb.length, ambAuto, apprTotal: appr.length, apprAuto, apprForced, minJudge };
}

// ---- handled automatically ---------------------------------------------------

export interface AutoItem { c: string; title: string; why: string; by: string; ts: number }

export function handledAutomatically(events: LedgerEvent[]): AutoItem[] {
  const items: AutoItem[] = [];
  for (const e of events) {
    const p = e.payload ?? {};
    const conf = p.confidence != null ? `confidence ${num(p.confidence).toFixed(2)} ≥ ${num(p.threshold, 0.8).toFixed(2)}` : '';
    const why = [s(p.reason ?? p.why), strList(p.evidence).join(' · '), conf].filter(Boolean).join(' · ');
    if (e.type === 'review.resolved') {
      items.push({ c: VISUAL_COLOR.committed, title: s(p.title ?? p.summary) || `Decided ${s(p.decision)} ${s(p.label ?? p.value)}`.trim(), why, by: e.actor, ts: e.ts });
    } else if (e.type === 'approval.auto') {
      items.push({ c: VISUAL_COLOR.committed, title: s(p.title ?? p.summary) || 'Emails approved automatically', why, by: e.actor, ts: e.ts });
    } else if (e.type === 'step.lease_expired') {
      items.push({ c: VISUAL_COLOR.leased, title: `Lease on ${e.step_id ?? 'a step'} expired, work taken over`, why: s(p.summary ?? p.reason) || `Previous holder ${s(p.worker ?? p.previous ?? '') || 'lost'}; the step went back to ready with a higher fence.`, by: e.actor || 'orchestrator', ts: e.ts });
    } else if (e.type === 'model.fallback') {
      items.push({ c: VISUAL_COLOR.claimed, title: `Model fallback for ${s(p.role ?? e.actor)}`, why: s(p.summary) || `${s(p.from)} → ${s(p.to)}${p.error ? ` after ${s(p.error)}` : ''}`, by: 'llm router', ts: e.ts });
    } else if (e.type === 'plan.revised' || e.type === 'step.replanned') {
      items.push({ c: VISUAL_COLOR.ready, title: e.type === 'plan.revised' ? 'Plan revised' : `Step ${e.step_id ?? ''} replanned`, why: s(p.reason ?? p.summary), by: e.actor, ts: e.ts });
    }
  }
  return items.reverse();
}

// ---- agents -----------------------------------------------------------------

export interface AgentView {
  id: string;
  name: string;
  roleLabel: string;
  group: 'core' | 'worker' | 'human';
  state: string;
  c: string;
  step: string;
  model: string;
  hbAge: number | null;
  stale: boolean;
  ttlFrac: number | null;
  ttlS: number | null;
}

const ROLE_LABEL: Record<string, string> = { orchestrator: 'Planner', verifier: 'Verifier', meta_reviewer: 'Reviewer', worker: 'Worker', human: 'Human' };

export function agentView(a: AgentStatus, now: number, agentsAt: number, leaseTtlS: number, escalationsOpen: number): AgentView {
  const hbAge = a.last_heartbeat_ms ? Math.max(0, Math.round((now - a.last_heartbeat_ms) / 1000)) : null;
  const human = a.role === 'human';
  const lost = !human && (a.alive === false || (hbAge != null && hbAge > Math.max(15, leaseTtlS * 2)));
  const total = a.lease_total_ms ?? leaseTtlS * 1000;
  const remaining = a.lease_ttl_ms != null ? a.lease_ttl_ms - (now - agentsAt) : null;
  const ttlFrac = a.current_step && remaining != null ? Math.max(0, Math.min(1, remaining / total)) : null;
  const primary = a.models?.[0];
  const model = a.model ?? primary ?? (a.model_role ? '—' : 'no LLM');
  let state: string, c: string;
  if (human) {
    state = escalationsOpen ? `${escalationsOpen} escalation${escalationsOpen > 1 ? 's' : ''}` : 'all clear';
    c = escalationsOpen ? VISUAL_COLOR.input : VISUAL_COLOR.idle;
  } else if (lost) { state = 'lost'; c = VISUAL_COLOR.dead; }
  else if (a.current_step) { state = 'active'; c = VISUAL_COLOR.leased; }
  else if (a.role === 'worker') { state = 'idle'; c = VISUAL_COLOR.idle; }
  else { state = 'healthy'; c = VISUAL_COLOR.committed; }
  return {
    id: a.id,
    name: a.name,
    roleLabel: ROLE_LABEL[a.role] ?? a.role,
    group: human ? 'human' : a.role === 'worker' ? 'worker' : 'core',
    state,
    c,
    step: a.current_step ? `${a.current_step}${a.lease_fence != null ? ` · t${a.lease_fence}` : ''}` : human ? 'queue:human' : '—',
    model: primary && a.model && a.model !== primary ? `${a.model} (fallback)` : model,
    hbAge: human ? null : hbAge,
    stale: lost,
    ttlFrac,
    ttlS: remaining != null && a.current_step ? Math.max(0, remaining / 1000) : null,
  };
}

export function initials(name: string): string {
  return name.split(/[\s(-]+/).filter(Boolean).map((w) => w[0]).join('').slice(0, 2).toUpperCase();
}

/** Short display model id: drop the provider prefix. */
export function shortModel(m: string | null | undefined): string {
  if (!m) return '—';
  return m.includes('/') ? m.slice(m.indexOf('/') + 1) : m;
}
