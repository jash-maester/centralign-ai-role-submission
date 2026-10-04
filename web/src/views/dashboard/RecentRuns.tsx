import { useLedger } from '../../store/store';
import { fmtDuration } from '../../lib/derive';
import { runVisual } from '../../lib/states';
import { useNow } from '../../components/ui';

export function RecentRuns() {
  const runs = useLedger((s) => s.runs);
  const runId = useLedger((s) => s.runId);
  const steps = useLedger((s) => s.steps);
  const select = useLedger((s) => s.selectRun);
  const replay = useLedger((s) => s.replay);
  const now = useNow(5000);
  const cur = Object.values(steps);
  return (
    <section className="bg-panel border border-line rounded-[10px] overflow-hidden shadow-card" data-testid="recent-runs">
      <div className="flex items-center px-[18px] py-3.5 border-b border-line">
        <span className="text-base+ font-medium">Recent runs</span>
        <div className="flex-1" />
        {runId && (
          <button type="button" onClick={() => void replay()} title="New run from the same goal, input, playbook, prompts and config (cache-only at replay-only)" className="h-7 px-2.5 border border-line2 rounded-md bg-transparent text-fg2 text-sm">
            Replay {runId}
          </button>
        )}
      </div>
      {runs.length === 0 && <div className="px-[18px] py-3 text-sm text-fg3">No runs yet. Start one from the Builder.</div>}
      {runs.slice(0, 12).map((r) => {
        const rv = runVisual(r.status);
        const isCur = r.id === runId;
        const done = isCur ? cur.filter((s) => s.status === 'committed' || s.status === 'replanned').length : r.steps_committed;
        const total = isCur ? cur.length : r.steps_total;
        const dur = r.created_at ? Math.round(((r.finished_at ?? now) - r.created_at) / 1000) : null;
        return (
          <button
            key={r.id}
            type="button"
            onClick={() => !isCur && void select(r.id)}
            className="w-full text-left grid gap-4 items-center px-[18px] py-[11px] border-0 border-b border-line text-sm+ text-fg hover:bg-panel2"
            style={{ gridTemplateColumns: '90px minmax(0,1fr) 190px 70px 80px', background: isCur ? 'var(--panel2)' : 'transparent' }}
          >
            <span className="mono text-xs+" style={{ color: isCur ? 'var(--fg)' : 'var(--fg3)' }}>{r.id}</span>
            <span className="truncate">{r.goal}</span>
            <span className="inline-flex items-center gap-1.5 mono text-xs" style={{ color: rv.c }}><span className="w-1.5 h-1.5 rounded-full" style={{ background: rv.c }} />{rv.label}</span>
            <span className="mono text-xs+ text-fg2">{dur != null ? fmtDuration(dur) : '—'}</span>
            <span className="mono text-xs+">{total != null ? `${done ?? 0} / ${total}` : '—'}</span>
          </button>
        );
      })}
    </section>
  );
}
