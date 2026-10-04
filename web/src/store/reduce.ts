/**
 * Pure ledger-event reducer: applies one SSE event to the run slice (run,
 * steps, facts). The store uses it for live updates and the mock backend uses
 * the same function to keep its own records consistent with the events it
 * emits. Events it cannot place (unknown step, plan change without step
 * bodies, escalation changes) ask the store to refetch instead of guessing.
 */
import type { Attempt, Fact, LedgerEvent, Run, RunStatus, Step, StepStatus } from '../api/types';
import { STEP_STATUSES } from '../api/types';

export interface RunSlice {
  run: Run | null;
  steps: Record<string, Step>;
  stepOrder: string[];
  facts: Fact[];
}

export interface Refetch {
  steps?: boolean;
  escalations?: boolean;
  agents?: boolean;
  run?: boolean;
  config?: boolean;
  budget?: boolean;
}

export const emptySlice = (): RunSlice => ({ run: null, steps: {}, stepOrder: [], facts: [] });

/** Event type -> the step status it implies (plans/01 §3 state machine). */
export const EVENT_STEP_STATUS: Partial<Record<string, StepStatus>> = {
  'step.ready': 'ready',
  'step.leased': 'leased',
  'step.claimed': 'claimed_done',
  'step.verified': 'verified',
  'step.committed': 'committed',
  'step.rejected': 'rejected',
  'step.lease_expired': 'lease_expired',
  'step.dead': 'dead',
  'step.replanned': 'replanned',
  'review.requested': 'review_required',
  'review.escalated': 'input_required',
  'review.resolved': 'ready',
  'approval.auto': 'ready',
  'input.requested': 'input_required',
  'input.answered': 'ready',
};

const EVENT_RUN_STATUS: Partial<Record<string, RunStatus>> = {
  'run.created': 'created',
  'run.understood': 'planning',
  'plan.created': 'running',
  'run.completed': 'completed',
  'run.completed_pending_input': 'completed_pending_input',
  'run.failed': 'failed',
};

const isStatus = (x: unknown): x is StepStatus => typeof x === 'string' && (STEP_STATUSES as readonly string[]).includes(x);
const str = (x: unknown): string | undefined => (typeof x === 'string' && x ? x : undefined);
const num = (x: unknown): number | undefined => (typeof x === 'number' && Number.isFinite(x) ? x : undefined);

function upsertSteps(slice: RunSlice, incoming: Step[]): RunSlice {
  const steps = { ...slice.steps };
  const order = slice.stepOrder.slice();
  for (const s of incoming) {
    if (!s || !s.id) continue;
    if (!steps[s.id]) order.push(s.id);
    steps[s.id] = { ...steps[s.id], ...s };
  }
  return { ...slice, steps, stepOrder: order };
}

function withAttempt(step: Step, patch: Partial<Attempt>, ev: LedgerEvent): Attempt[] {
  const hist = (step.history ?? []).slice();
  const n = patch.attempt ?? step.attempt;
  const i = hist.findIndex((a) => a.attempt === n);
  const base: Attempt = i >= 0 ? hist[i] : { attempt: n, started_at: ev.ts, observations: [] };
  const merged = { ...base, ...patch };
  if (i >= 0) hist[i] = merged;
  else hist.push(merged);
  return hist;
}

export function reduceEvent(slice: RunSlice, ev: LedgerEvent): { slice: RunSlice; refetch: Refetch } {
  const refetch: Refetch = {};
  const p = ev.payload ?? {};
  let next = slice;

  // ---- plan changes carry the step bodies when the API includes them ------
  if (ev.type === 'plan.created' || ev.type === 'plan.revised') {
    if (Array.isArray(p.steps) && p.steps.length && typeof p.steps[0] === 'object') next = upsertSteps(next, p.steps as Step[]);
    else refetch.steps = true;
  }

  // ---- run status / criteria ----------------------------------------------
  const rs = EVENT_RUN_STATUS[ev.type];
  if (next.run && (!ev.run_id || ev.run_id === next.run.id)) {
    let run = next.run;
    if (rs) run = { ...run, status: rs, finished_at: rs.startsWith('completed') || rs === 'failed' ? ev.ts : run.finished_at };
    if (Array.isArray(p.criteria)) run = { ...run, criteria: p.criteria as Run['criteria'] };
    if (run !== next.run) next = { ...next, run };
  } else if (rs) refetch.run = true;

  // ---- committed facts ------------------------------------------------------
  if (ev.type === 'fact.committed' && str(p.key)) {
    const fact: Fact = { key: p.key as string, value: p.value, source_step: ev.step_id ?? str(p.source_step) ?? '', committed_at: ev.ts };
    next = { ...next, facts: [...next.facts.filter((f) => f.key !== fact.key), fact] };
  }

  // ---- step status ----------------------------------------------------------
  const target = isStatus(p.status) ? p.status : isStatus(p.to) ? p.to : EVENT_STEP_STATUS[ev.type];
  if (ev.step_id && (target || ev.type === 'step.observation')) {
    const step = next.steps[ev.step_id];
    if (!step) {
      refetch.steps = true;
    } else {
      const s: Step = { ...step, updated_at: ev.ts };
      if (target) s.status = target;
      const worker = str(p.worker) ?? str(p.agent_id) ?? ev.actor;
      switch (ev.type) {
        case 'step.leased': {
          s.attempt = num(p.attempt) ?? step.attempt + 1;
          s.fence = num(p.fence) ?? (step.fence ?? 0) + 1;
          s.lease_owner = worker;
          s.history = withAttempt(s, { attempt: s.attempt, worker, fence: s.fence, model: str(p.model) ?? null }, ev);
          break;
        }
        case 'step.observation': {
          const obs = { ...p, ts: ev.ts };
          const hist = (step.history ?? []).slice();
          const last = hist[hist.length - 1];
          if (last) hist[hist.length - 1] = { ...last, observations: [...(last.observations ?? []), obs] };
          s.history = hist;
          break;
        }
        case 'step.claimed': {
          const claim = {
            worker,
            fence: num(p.fence) ?? step.fence ?? 0,
            summary: str(p.summary) ?? '',
            data: (p.data as Record<string, unknown>) ?? {},
            evidence: Array.isArray(p.evidence) ? (p.evidence as string[]) : [],
            acted: p.acted !== false,
            ts: ev.ts,
          };
          s.claim = claim;
          s.history = withAttempt(s, { claim }, ev);
          break;
        }
        case 'step.committed':
        case 'step.verified':
        case 'step.rejected': {
          const ok = ev.type !== 'step.rejected';
          const verdict = { ok, check: str(p.check) ?? step.postcondition?.check ?? '', reason: str(p.reason) ?? '', verifier: ev.actor, model: str(p.model) ?? null, ts: ev.ts };
          s.verdict = verdict;
          if (ev.type !== 'step.verified') {
            s.lease_owner = null;
            s.history = withAttempt(s, { verdict, outcome: ok ? 'committed' : 'rejected', ended_at: ev.ts }, ev);
          }
          break;
        }
        case 'step.lease_expired':
          s.history = withAttempt(s, { outcome: 'lease_expired', ended_at: ev.ts }, ev);
          s.lease_owner = null;
          break;
        case 'step.dead':
        case 'step.replanned':
        case 'step.ready':
          s.lease_owner = null;
          break;
        default:
          break;
      }
      next = { ...next, steps: { ...next.steps, [s.id]: s } };
    }
  }

  // ---- side lists the store keeps by refetching ---------------------------
  if (/^(review\.escalated|input\.(requested|answered)|review\.resolved)$/.test(ev.type)) refetch.escalations = true;
  if (/^(agent\.|step\.(leased|claimed|committed|rejected|lease_expired|heartbeat_lost)|model\.fallback)/.test(ev.type)) refetch.agents = true;
  if (ev.type === 'run.config_updated' || ev.type === 'config.updated') refetch.config = true;
  if (/^llm\.|spend\.cap_reached/.test(ev.type)) refetch.budget = true;

  return { slice: next, refetch };
}
