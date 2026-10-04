import { useMemo } from 'react';
import { useLedger } from '../../store/store';
import { committedCount, counters, elapsedS, fmtDuration, progressCounts } from '../../lib/derive';
import { runVisual } from '../../lib/states';
import { Chip, useNow } from '../../components/ui';

const plural = (n: number, one: string, many = one + 's') => `${n} ${n === 1 ? one : many}`;

export function spendLine(budget: { spent_usd?: number; used: number; limit: number } | null, cap: number) {
  const spent = budget?.spent_usd ?? 0;
  return `$${spent.toFixed(2)} of $${cap.toFixed(2)}${budget ? ` · ${budget.used}/${budget.limit} free requests` : ''}`;
}

export function RunStrip() {
  const run = useLedger((s) => s.run);
  const steps = useLedger((s) => s.steps);
  const events = useLedger((s) => s.events);
  const budget = useLedger((s) => s.budget);
  const cap = useLedger((s) => s.config.spend_cap_usd);
  const setView = useLedger((s) => s.setView);
  const now = useNow(1000);
  const progress = useMemo(() => progressCounts(steps), [steps]);
  const c = useMemo(() => counters(events), [events]);
  const { done, total } = committedCount(steps);
  if (!run) return null;
  const rv = runVisual(run.status);
  const over = (budget?.spent_usd ?? 0) >= cap || (budget ? budget.remaining <= 0 : false);
  return (
    <section data-testid="run-strip" className="bg-panel border border-line rounded-[10px] px-[18px] py-3 flex items-center gap-4 flex-wrap shadow-card">
      <Chip c={rv.c} label={rv.label} size="md" />
      <span className="mono text-xs+ text-fg3">{run.id} · {fmtDuration(elapsedS(run, now))}</span>
      <span className="flex-1 min-w-[200px] text-base text-fg2 whitespace-nowrap overflow-hidden text-ellipsis" title={run.goal}>{run.goal}</span>
      <div className="flex items-center gap-2.5">
        <div className="flex w-40 h-1.5 rounded-[3px] overflow-hidden gap-px bg-bg2">
          {progress.map((p) => <div key={p.v} title={`${p.label} ${p.n}`} style={{ flex: `${p.n} 1 0`, background: p.c }} />)}
        </div>
        <span className="mono text-sm">{done}<span className="text-fg3">/{total}</span></span>
      </div>
      <span className="mono text-xs+ whitespace-nowrap" style={{ color: over ? 'var(--s-rejected)' : 'var(--fg3)' }}>
        {plural(c.retries, 'retry', 'retries')} · {plural(c.takeovers, 'takeover')} · {plural(c.fallbacks, 'fallback')} · {spendLine(budget, cap)}
      </span>
      <button type="button" onClick={() => setView('report')} className="border-0 bg-transparent p-0 text-accent text-sm+ whitespace-nowrap">Evidence report →</button>
    </section>
  );
}
