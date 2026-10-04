import { useMemo, useState } from 'react';
import type { Escalation } from '../../api/types';
import { useLedger } from '../../store/store';
import { decisions, handledAutomatically } from '../../lib/derive';
import { Dot } from '../../components/ui';

export function EscalationCard({ esc }: { esc: Escalation }) {
  const answer = useLedger((s) => s.answerEscalation);
  const [pick, setPick] = useState<string | null>(null);
  // A batch email approval is a one-off yes/no, not a reusable rule.
  const isApproval = !esc.lane && esc.options.some((o) => o.value === 'approve');
  const [saveRule, setSaveRule] = useState(!isApproval);
  const [busy, setBusy] = useState(false);
  const below = esc.confidence < esc.threshold;
  return (
    <div data-testid="escalation" className="bg-panel rounded-[10px] shadow-card overflow-hidden border" style={{ borderColor: 'color-mix(in oklch, var(--s-input) 35%, var(--line))' }}>
      <div className="px-[18px] pt-4 pb-3 flex flex-col gap-[5px]">
        <span className="mono text-2xs tracking-[0.06em]" style={{ color: 'var(--s-input)' }}>ESCALATED · META-REVIEWER UNSURE</span>
        <span className="text-lg font-semibold">{esc.question}</span>
        {esc.context && <span className="text-sm+ text-fg2 text-pretty">{esc.context}</span>}
      </div>
      <div className="mx-[18px] mb-3 px-3 py-2.5 rounded-lg bg-panel2 flex flex-col gap-[5px]">
        <span className="text-xs+ font-medium text-fg2">What the meta-reviewer tried</span>
        {(esc.tried ?? []).map((t, i) => (
          <span key={i} className="grid grid-cols-[12px_minmax(0,1fr)] gap-1.5 text-sm text-fg2 leading-[1.45]"><span className="text-fg3">·</span><span>{t}</span></span>
        ))}
        <span className="mono text-xs" style={{ color: below ? 'var(--s-rejected)' : 'var(--fg2)' }}>
          confidence {esc.confidence.toFixed(2)} {below ? '<' : '≥'} {esc.threshold.toFixed(2)} (auto threshold)
        </span>
      </div>
      <div className="px-[18px] pb-3.5 flex flex-col gap-1.5" role="radiogroup">
        {esc.options.map((o) => {
          const on = pick === o.value;
          return (
            <button
              key={o.value}
              type="button"
              role="radio"
              aria-checked={on}
              onClick={() => setPick(o.value)}
              className="grid grid-cols-[16px_minmax(0,1fr)_auto] gap-2.5 items-center px-3 py-[9px] border rounded-lg text-fg text-left"
              style={{ borderColor: on ? 'var(--s-input)' : 'var(--line)', background: on ? 'color-mix(in oklch, var(--s-input) 7%, var(--panel))' : 'var(--panel)' }}
            >
              <span className="w-3.5 h-3.5 rounded-full grid place-items-center" style={{ border: `1.5px solid ${on ? 'var(--s-input)' : 'var(--line2)'}` }}>
                <span className="w-1.5 h-1.5 rounded-full" style={{ background: on ? 'var(--s-input)' : 'transparent' }} />
              </span>
              <span className="text-base font-medium">{o.label}</span>
              <span className="mono text-xs text-fg3">{o.detail || o.value}</span>
            </button>
          );
        })}
      </div>
      <div className="flex items-center gap-3 px-[18px] py-3 border-t border-line bg-panel2 flex-wrap">
        <button
          type="button"
          disabled={!pick || busy}
          onClick={async () => {
            if (!pick) return;
            setBusy(true);
            await answer(esc.id, pick, saveRule);
            setBusy(false);
          }}
          className="h-8 px-3.5 border-0 rounded-md text-white text-sm+ font-semibold whitespace-nowrap disabled:cursor-not-allowed"
          style={{ background: pick ? 'var(--s-input)' : 'color-mix(in oklch, var(--s-input) 40%, var(--line2))' }}
        >
          {busy ? 'Committing…' : 'Commit decision'}
        </button>
        {!isApproval && <label className="flex items-center gap-[7px] text-sm text-fg2 cursor-pointer select-none">
          <input type="checkbox" className="sr-only" checked={saveRule} onChange={(e) => setSaveRule(e.target.checked)} />
          <span className="w-3.5 h-3.5 rounded-[3px] border grid place-items-center text-white text-[10px]" style={{ borderColor: saveRule ? 'var(--s-input)' : 'var(--line2)', background: saveRule ? 'var(--s-input)' : 'transparent' }}>
            {saveRule ? '✓' : ''}
          </span>
          Save as playbook rule
        </label>}
      </div>
    </div>
  );
}

export function NeedsAttention() {
  const escalations = useLedger((s) => s.escalations);
  const events = useLedger((s) => s.events);
  const showAgent = useLedger((s) => s.showAgent);
  const auto = useMemo(() => handledAutomatically(events), [events]);
  const ds = useMemo(() => decisions(events, escalations), [events, escalations]);
  const autoN = ds.filter((d) => !d.escalated && d.by !== 'you').length;
  const n = escalations.length;
  return (
    <section id="needs" className="flex flex-col gap-3">
      <div className="flex items-baseline gap-2.5 flex-wrap">
        <span className="text-xl font-semibold">Needs your attention</span>
        <span className="text-sm+ text-fg3">
          {n ? `${n} escalation${n > 1 ? 's' : ''} · the meta-reviewer resolved ${autoN} of ${ds.length} decisions` : 'nothing waiting on you'}
        </span>
      </div>
      <div className="grid gap-3.5 items-start" style={{ gridTemplateColumns: 'repeat(auto-fit, minmax(380px, 1fr))' }}>
        <div className="flex flex-col gap-3.5">
          {escalations.map((e) => <EscalationCard key={e.id} esc={e} />)}
          {n === 0 && (
            <div className="bg-panel border border-line rounded-[10px] px-5 py-[18px] flex flex-col gap-1.5 shadow-card">
              <span className="flex items-center gap-2 text-md font-semibold"><Dot c="var(--s-committed)" size={8} />All clear</span>
              <span className="text-sm+ text-fg2">Every decision cleared its threshold or was answered. The run finishes on its own.</span>
            </div>
          )}
        </div>
        <div className="bg-panel border border-line rounded-[10px] shadow-card overflow-hidden" data-testid="handled-auto">
          <div className="flex items-center justify-between px-[18px] pt-3.5 pb-2.5">
            <span className="text-base+ font-semibold">Handled automatically <span className="mono text-xs+ text-fg3 font-normal">{auto.length}</span></span>
            <button type="button" onClick={() => showAgent('meta-reviewer')} className="border-0 bg-transparent p-0 text-accent text-sm">Meta-reviewer settings</button>
          </div>
          <div className="max-h-[360px] overflow-auto">
            {auto.length === 0 && <div className="px-[18px] py-3 border-t border-line text-sm text-fg3">No automatic decisions yet.</div>}
            {auto.map((a, i) => (
              <div key={i} className="grid grid-cols-[8px_minmax(0,1fr)_auto] gap-3 items-start px-[18px] py-2.5 border-t border-line">
                <span className="w-[7px] h-[7px] rounded-[2px] mt-[5px]" style={{ background: a.c }} />
                <div className="flex flex-col gap-0.5 min-w-0">
                  <span className="text-sm+ font-medium">{a.title}</span>
                  {a.why && <span className="text-xs+ text-fg3 text-pretty">{a.why}</span>}
                </div>
                <span className="mono text-2xs text-fg3 whitespace-nowrap pt-0.5">{a.by}</span>
              </div>
            ))}
          </div>
        </div>
      </div>
    </section>
  );
}
