import { useState } from 'react';
import { useLedger } from '../../store/store';
import type { Criterion } from '../../api/types';

export const CRITERION_STYLE: Record<Criterion['status'], { icon: string; c: string }> = {
  verified: { icon: '✓', c: 'var(--s-committed)' },
  pending: { icon: '○', c: 'var(--fg3)' },
  waived: { icon: '–', c: 'var(--s-planned)' },
  failed: { icon: '✗', c: 'var(--s-rejected)' },
};

const short = (v: unknown) => {
  const s = typeof v === 'string' ? v : JSON.stringify(v);
  return s.length > 64 ? s.slice(0, 61) + '…' : s;
};

export function Criteria() {
  const criteria = useLedger((s) => s.run?.criteria ?? []);
  const facts = useLedger((s) => s.facts);
  const [allFacts, setAllFacts] = useState(false);
  const shown = allFacts ? facts : facts.slice(-8);
  return (
    <div className="bg-panel border border-line rounded-[10px] px-[18px] py-4 flex flex-col gap-2.5 shadow-card" data-testid="criteria">
      <span className="text-base+ font-medium">Success criteria</span>
      {criteria.length === 0 && <span className="text-sm text-fg3">Criteria appear once the orchestrator understands the goal.</span>}
      {criteria.map((c) => {
        const st = CRITERION_STYLE[c.status] ?? CRITERION_STYLE.pending;
        return (
          <div key={c.id} className="grid grid-cols-[16px_minmax(0,1fr)] gap-2 text-sm+ leading-[1.45]" title={c.evidence ?? c.check}>
            <span className="mono" style={{ color: st.c }}>{st.icon}</span>
            <span className="text-pretty">{c.text}</span>
          </div>
        );
      })}
      <div className="flex items-center justify-between pt-2 mt-1 border-t border-line">
        <span className="text-base+ font-medium">Committed facts <span className="mono text-xs text-fg3 font-normal">{facts.length}</span></span>
        {facts.length > 8 && (
          <button type="button" onClick={() => setAllFacts(!allFacts)} className="border-0 bg-transparent p-0 text-accent text-sm">{allFacts ? 'latest only' : 'show all'}</button>
        )}
      </div>
      <div className={`flex flex-col ${allFacts ? 'max-h-[320px] overflow-auto' : ''}`}>
        {facts.length === 0 && <span className="text-sm text-fg3">No facts yet. Facts appear only after the verifier commits.</span>}
        {shown.map((f) => (
          <div key={f.key} className="grid grid-cols-[minmax(0,0.9fr)_minmax(0,1.1fr)] gap-2 py-[3px] mono text-xs">
            <span className="text-fg2 truncate">{f.key}</span>
            <span className="truncate" title={typeof f.value === 'string' ? f.value : JSON.stringify(f.value)}>{short(f.value)}</span>
          </div>
        ))}
      </div>
    </div>
  );
}
