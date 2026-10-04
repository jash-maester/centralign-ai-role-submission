/**
 * Step status -> visual state (colour token + label). The amber "claimed" vs
 * green "committed" split is the product's main visual idea: a claim is not a
 * fact, so `claimed_done` and `verified` stay amber until the verifier commits.
 */
import type { RunStatus, StepStatus } from '../api/types';

export type Visual = 'planned' | 'ready' | 'leased' | 'claimed' | 'committed' | 'rejected' | 'input' | 'dead' | 'skipped' | 'idle';

export const VISUAL_COLOR: Record<Visual, string> = {
  planned: 'var(--s-planned)',
  ready: 'var(--s-ready)',
  leased: 'var(--s-leased)',
  claimed: 'var(--s-claimed)',
  committed: 'var(--s-committed)',
  rejected: 'var(--s-rejected)',
  input: 'var(--s-input)',
  dead: 'var(--s-dead)',
  skipped: 'var(--fg3)',
  idle: 'var(--fg3)',
};

const STATUS_VISUAL: Record<StepStatus, [Visual, string]> = {
  planned: ['planned', 'planned'],
  ready: ['ready', 'ready'],
  leased: ['leased', 'leased'],
  claimed_done: ['claimed', 'verifying'],
  verified: ['claimed', 'verified'],
  committed: ['committed', 'committed'],
  rejected: ['rejected', 'rejected'],
  replanned: ['skipped', 'replanned'],
  lease_expired: ['dead', 'lease expired'],
  review_required: ['leased', 'reviewing'],
  input_required: ['input', 'escalated'],
  dead: ['dead', 'dead'],
};

export function stepVisual(status: StepStatus | string | undefined, opts: { skipped?: boolean } = {}): { v: Visual; c: string; label: string } {
  if (opts.skipped) return { v: 'skipped', c: VISUAL_COLOR.skipped, label: 'skipped' };
  const [v, label] = STATUS_VISUAL[status as StepStatus] ?? ['planned', String(status ?? '—')];
  return { v, c: VISUAL_COLOR[v], label };
}

export const LEGEND: { v: Visual; label: string }[] = [
  { v: 'committed', label: 'committed' },
  { v: 'claimed', label: 'verifying' },
  { v: 'leased', label: 'leased' },
  { v: 'ready', label: 'ready' },
  { v: 'input', label: 'escalated' },
  { v: 'rejected', label: 'rejected' },
  { v: 'dead', label: 'dead / expired' },
  { v: 'planned', label: 'planned' },
  { v: 'skipped', label: 'skipped' },
];

export function runVisual(status: RunStatus | string | undefined): { c: string; label: string } {
  switch (status) {
    case 'completed': return { c: VISUAL_COLOR.committed, label: 'completed' };
    case 'completed_pending_input': return { c: VISUAL_COLOR.input, label: 'completed · input pending' };
    case 'failed': return { c: VISUAL_COLOR.rejected, label: 'failed' };
    case 'created': case 'understanding': case 'planning': return { c: VISUAL_COLOR.ready, label: String(status) };
    case 'running': return { c: VISUAL_COLOR.leased, label: 'running' };
    default: return { c: VISUAL_COLOR.idle, label: String(status ?? '—') };
  }
}

/** Ledger event type -> colour (live ledger + builder log). */
export function eventColor(type: string): string {
  if (/rejected|lost|dead|fault|error|outage|exhausted|cap_reached|stale_fence|failed|heartbeat_lost/.test(type)) return VISUAL_COLOR.rejected;
  if (/committed|resolved|approval\.auto|run\.completed$/.test(type)) return VISUAL_COLOR.committed;
  if (/claimed|verified|fallback/.test(type)) return VISUAL_COLOR.claimed;
  if (/input|escalated|pending_input/.test(type)) return VISUAL_COLOR.input;
  if (/leased|lease_expired|ready/.test(type)) return VISUAL_COLOR.leased;
  return 'var(--fg)';
}

/** Event types that count as a fault (cause) vs a recovery (effect). */
export const FAULT_TYPES = new Set(['fault.injected', 'step.heartbeat_lost', 'agent.lost', 'llm.budget_exhausted', 'spend.cap_reached', 'step.stale_fence']);
export const RECOVERY_TYPES = new Set(['step.rejected', 'step.lease_expired', 'model.fallback', 'plan.revised', 'step.replanned', 'agent.registered']);
