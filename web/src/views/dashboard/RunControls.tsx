/** Run controls (plans/01 §6a, features H7 + H13): every RunConfig key, live hints, faults. */
import { useMemo, type ReactNode } from 'react';
import { useLedger } from '../../store/store';
import type { FaultName, RunConfig } from '../../api/types';
import { autoAt, decisions, shortModel } from '../../lib/derive';
import { temperature } from '../../lib/runConfig';
import { RECOVERY_TYPES } from '../../lib/states';
import { Toggle } from '../../components/ui';

const RED = 'var(--s-rejected)';

interface ItemBase { label: string; note: string; noteC?: string | null }
function Item({ note, noteC, children }: { note: string; noteC?: string | null; children: ReactNode }) {
  return (
    <div className="flex flex-col gap-1.5">
      {children}
      <span className="text-xs+ leading-[1.4] text-pretty" style={{ color: noteC ?? 'var(--fg3)' }}>{note}</span>
    </div>
  );
}

function Slider({ label, note, noteC, value, min, max, step, fmt, onChange, testId }: ItemBase & { value: number; min: number; max: number; step: number; fmt: (v: number) => string; onChange: (v: number) => void; testId?: string }) {
  return (
    <Item note={note} noteC={noteC}>
      <div className="flex justify-between items-baseline gap-2"><span className="text-sm+ font-medium">{label}</span><span className="mono text-sm">{fmt(value)}</span></div>
      <input data-testid={testId} aria-label={label} type="range" min={min} max={max} step={step} value={value} onChange={(e) => onChange(Number(e.target.value))} className="w-full" />
    </Item>
  );
}

function Switch({ label, note, noteC, on, onChange }: ItemBase & { on: boolean; onChange: (v: boolean) => void }) {
  return (
    <Item note={note} noteC={noteC}>
      <label className="flex justify-between items-center gap-2.5 cursor-pointer" onClick={() => onChange(!on)}>
        <span className="text-sm+ font-medium">{label}</span>
        <Toggle on={on} onChange={onChange} />
      </label>
    </Item>
  );
}

function Seg<T extends string>({ label, note, value, opts, onChange }: ItemBase & { value: T; opts: [T, string][]; onChange: (v: T) => void }) {
  return (
    <Item note={note}>
      <span className="text-sm+ font-medium">{label}</span>
      <div className="flex border border-line2 rounded-md overflow-hidden">
        {opts.map(([v, l]) => (
          <button key={v} type="button" onClick={() => onChange(v)} className="flex-1 h-[26px] border-0 text-xs+" style={{ background: value === v ? 'var(--fg)' : 'transparent', color: value === v ? 'var(--bg)' : 'var(--fg2)' }}>{l}</button>
        ))}
      </div>
    </Item>
  );
}

const FAULTS: [FaultName, string, string][] = [
  ['false_claim', 'False claim', 'Next browser step reports success without acting'],
  ['kill_worker', 'Kill worker', 'Terminate the browser operator holding a lease'],
  ['expire_session', 'Expire session', 'Invalidate the CRM login'],
  ['model_outage', 'Model outage', 'Make the primary worker model fail'],
];

export function RunControls() {
  const c = useLedger((s) => s.config);
  const set = useLedger((s) => s.setConfig);
  const reset = useLedger((s) => s.resetConfig);
  const events = useLedger((s) => s.events);
  const escalations = useLedger((s) => s.escalations);
  const steps = useLedger((s) => s.steps);
  const budget = useLedger((s) => s.budget);
  const agents = useLedger((s) => s.agents);
  const faultLast = useLedger((s) => s.faultLast);
  const inject = useLedger((s) => s.injectFault);
  const runId = useLedger((s) => s.runId);

  const ds = useMemo(() => decisions(events, escalations), [events, escalations]);
  const hint = autoAt(ds, c);
  const multi = useMemo(() => Object.values(steps).filter((s) => s.attempt > 1), [steps]);
  const maxUsed = multi.reduce((m, s) => Math.max(m, s.attempt), 1);
  const spent = budget?.spent_usd ?? 0;
  const workerModels = (agents.find((a) => a.model_role === 'worker')?.models ?? budget?.models?.worker ?? []).map(shortModel);
  const d = c.determinism;

  // Last time each fault fired and the recovery event that followed it.
  const faultNote = (f: FaultName): string => {
    const fired = [...events].reverse().find((e) => e.type === 'fault.injected' && (e.payload.fault ?? e.payload.name) === f);
    if (!fired) return faultLast[f]?.note ?? 'not fired';
    const rec = events.find((e) => e.ts >= fired.ts && e !== fired && RECOVERY_TYPES.has(e.type));
    return rec ? `recovered · ${rec.type}` : 'recovering…';
  };
  const leaseHolder = agents.find((a) => a.id.startsWith('worker-browser') && a.current_step && a.alive !== false)?.id;

  const warnings = [
    !c.check_then_act && 'duplicates possible',
    !c.model_fallback && 'no model fallback',
    c.max_attempts < 2 && 'retries off',
    spent >= c.spend_cap_usd && spent > 0 && 'over budget',
    !c.llm_judge_enabled && 'judge off',
    c.dry_run && 'dry run',
  ].filter(Boolean) as string[];
  const p = <K extends keyof RunConfig>(k: K) => (v: RunConfig[K]) => set({ [k]: v } as Partial<RunConfig>);
  const f2 = (v: number) => v.toFixed(2);

  const groups: { title: string; items: ReactNode[] }[] = [
    { title: 'Autonomy', items: [
      <Slider key="r" testId="ctl-review" label="Ambiguity auto-threshold" value={c.review_auto_threshold} min={0.4} max={1} step={0.01} fmt={f2} onChange={p('review_auto_threshold')}
        note={hint.ambTotal ? `${hint.ambAuto} of ${hint.ambTotal} ambiguous rows decided automatically, ${hint.ambTotal - hint.ambAuto} escalate` : 'No ambiguous rows reviewed yet'} />,
      <Slider key="a" label="Email auto-approve threshold" value={c.approval_auto_threshold} min={0.5} max={1} step={0.01} fmt={f2} onChange={p('approval_auto_threshold')}
        note={hint.apprForced ? 'Overridden by policy below' : hint.minJudge != null ? `Judge min ${hint.minJudge.toFixed(2)} → ${hint.minJudge >= c.approval_auto_threshold ? 'auto-approved' : 'escalates to you'}` : 'No drafts judged yet'} />,
      <Switch key="h" label="Always ask a human for external email" on={c.always_ask_human_email} onChange={p('always_ask_human_email')} note={c.always_ask_human_email ? 'Every email batch waits for you' : 'Meta-reviewer may approve'} />,
      <Switch key="j" label="LLM judge for soft checks" on={c.llm_judge_enabled} onChange={p('llm_judge_enabled')} note={c.llm_judge_enabled ? 'Runs after deterministic checks (D4)' : 'Rules only; emails cannot be auto-approved'} noteC={c.llm_judge_enabled ? null : RED} />,
    ] },
    { title: 'Reliability', items: [
      <Slider key="t" label="Lease TTL" value={c.lease_ttl_s} min={3} max={60} step={1} fmt={(v) => `${v}s`} onChange={p('lease_ttl_s')} note={`Takeover after ~${c.lease_ttl_s}s · heartbeat every ${(c.lease_ttl_s / 3).toFixed(1)}s`} />,
      <Slider key="m" label="Max attempts per step" value={c.max_attempts} min={1} max={10} step={1} fmt={String} onChange={p('max_attempts')}
        note={c.max_attempts < maxUsed ? `${multi.length} step${multi.length > 1 ? 's' : ''} needed ${maxUsed} attempts; they would be dead` : multi.length ? `${multi.length} step${multi.length > 1 ? 's' : ''} needed ${maxUsed} attempts so far` : 'No retries needed yet'} noteC={c.max_attempts < maxUsed ? RED : null} />,
      <Switch key="c" label="Check-then-act on writes" on={c.check_then_act} onChange={p('check_then_act')} note={c.check_then_act ? 'Takeovers are idempotent' : 'Takeovers can create duplicate contacts (F1 fails)'} noteC={c.check_then_act ? null : RED} />,
      <Slider key="rp" label="Replan after rejections" value={c.replan_after_rejections} min={0} max={10} step={1} fmt={(v) => (v ? String(v) : 'off')} onChange={p('replan_after_rejections')}
        note={c.replan_after_rejections ? `Blocked UI path → API skill after ${c.replan_after_rejections} rejections (B5)` : 'Replanning off: blocked steps go dead instead'} />,
      <Switch key="f" label="Model fallback" on={c.model_fallback} onChange={p('model_fallback')} note={c.model_fallback ? `Next model in the role list on error${workerModels.length ? ` (${workerModels.join(' → ')})` : ''}` : 'Model outage → steps dead after max attempts'} noteC={c.model_fallback ? null : RED} />,
    ] },
    { title: 'Execution', items: [
      <Slider key="d" testId="ctl-determinism" label="Determinism" value={d} min={0} max={1} step={0.05} fmt={f2} onChange={p('determinism')}
        note={`orch T=${temperature('orchestrator', d).toFixed(2)} · workers T=${temperature('worker', d).toFixed(2)} · meta T=${temperature('meta_reviewer', d).toFixed(2)} · verifier T=0`} />,
      <Item key="s" note={c.seed_pinned ? 'Same inputs → same plan (where the provider honours seed; the cache makes it exact)' : 'Random seed per attempt'}>
        <div className="flex justify-between items-center gap-2.5">
          <span className="text-sm+ font-medium flex items-center gap-2">Pin seed
            <input aria-label="seed" type="number" value={c.seed} onChange={(e) => set({ seed: Number(e.target.value) || 0 })} className="w-16 h-6 px-1.5 border border-line2 rounded bg-panel text-fg mono text-xs+ outline-none" />
          </span>
          <Toggle on={c.seed_pinned} onChange={p('seed_pinned')} />
        </div>
      </Item>,
      <Slider key="bc" label="Browser operators" value={c.browser_concurrency} min={1} max={2} step={1} fmt={String} onChange={p('browser_concurrency')} note={`${c.browser_concurrency} consumer${c.browser_concurrency > 1 ? 's' : ''} on queue:browser.espocrm (scale via compose)`} />,
      <Seg key="w" label="CRM write path" value={c.crm_write_path} opts={[['browser', 'Browser'], ['auto', 'Auto'], ['api', 'API']]} onChange={p('crm_write_path')}
        note={c.crm_write_path === 'api' ? 'Faster, but skips the UI demo' : c.crm_write_path === 'auto' ? 'Browser first, API when the UI is blocked' : 'Acts through the UI; verifier reads REST'} />,
    ] },
    { title: 'Safety', items: [
      <Slider key="sc" label="Spend cap" value={c.spend_cap_usd} min={0.5} max={5} step={0.1} fmt={(v) => `$${v.toFixed(2)}`} onChange={p('spend_cap_usd')}
        note={`$${spent.toFixed(2)} spent · ${c.spend_cap_usd ? Math.round((spent / c.spend_cap_usd) * 100) : 0}% of cap${budget ? ` · ${budget.used}/${budget.limit} free requests today` : ''}`} noteC={spent >= c.spend_cap_usd && spent > 0 ? RED : budget && budget.remaining <= (budget.reserve ?? 3) ? RED : null} />,
      <Slider key="fz" label="Fuzzy match threshold" value={c.fuzzy_match_threshold} min={0.7} max={0.99} step={0.01} fmt={f2} onChange={p('fuzzy_match_threshold')} note={`Name + company similarity ≥ ${c.fuzzy_match_threshold.toFixed(2)} proposes a match to the meta-reviewer`} />,
      <Switch key="dr" label="Dry run (do not send email)" on={c.dry_run} onChange={p('dry_run')} note={c.dry_run ? 'Emails approved and verified, not sent' : 'Mailer sends via SMTP'} />,
    ] },
    { title: 'Inject fault', items: FAULTS.map(([f, label, desc]) => (
      <Item key={f} note={desc}>
        <button
          type="button"
          data-testid={`fault-${f}`}
          title={desc}
          disabled={!runId}
          onClick={() => inject(f, f === 'kill_worker' && leaseHolder ? { agent_id: leaseHolder } : undefined)}
          className="text-left px-2.5 py-2 border rounded-md bg-panel text-fg flex justify-between items-center gap-2 hover:[background:color-mix(in_oklch,var(--s-rejected)_6%,var(--panel))]"
          style={{ borderColor: 'color-mix(in oklch, var(--s-rejected) 30%, var(--line))' }}
        >
          <span className="text-sm+ font-medium whitespace-nowrap" style={{ color: RED }}>{label}</span>
          <span className="mono text-2xs text-fg3 text-right truncate" title={faultNote(f)}>{faultNote(f)}</span>
        </button>
      </Item>
    )) },
  ];

  return (
    <section className="bg-panel border border-line rounded-[10px] overflow-hidden shadow-card" data-testid="run-controls">
      <div className="flex items-center gap-3 px-[18px] py-3.5 border-b border-line flex-wrap">
        <span className="text-md font-semibold">Run controls</span>
        <span className="text-sm text-fg3">Changes apply from the next attempt and are logged to the ledger.</span>
        <div className="flex-1" />
        {warnings.length > 0 && (
          <span className="inline-flex items-center gap-1.5 px-[9px] py-[3px] rounded-[10px] text-xs+ font-medium" style={{ color: RED, background: 'color-mix(in oklch, var(--s-rejected) 10%, transparent)' }}>
            {warnings.length} risk{warnings.length > 1 ? 's' : ''}: {warnings.join(', ')}
          </span>
        )}
        <button type="button" onClick={reset} className="h-7 px-2.5 border border-line2 rounded-md bg-transparent text-fg2 text-sm">Reset to playbook defaults</button>
      </div>
      <div className="grid" style={{ gridTemplateColumns: 'repeat(auto-fit, minmax(250px, 1fr))' }}>
        {groups.map((g) => (
          <div key={g.title} className="px-[18px] pt-3.5 pb-[18px] border-r border-b border-line flex flex-col gap-3.5 -mr-px -mb-px">
            <span className="mono text-2xs text-fg3 uppercase tracking-[0.06em]">{g.title}</span>
            {g.items}
          </div>
        ))}
      </div>
    </section>
  );
}
