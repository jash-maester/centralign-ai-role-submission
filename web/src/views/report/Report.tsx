/** Evidence report (H6): printable, every section of the design, Export .md and Print / PDF. */
import { useEffect, useMemo, useState, type ReactNode } from 'react';
import { api } from '../../api';
import type { LeadOutcome, Report } from '../../api/types';
import { useLedger } from '../../store/store';
import { reportToMarkdown } from '../../lib/reportMd';
import { runVisual } from '../../lib/states';
import { shortModel } from '../../lib/derive';
import { CRITERION_STYLE } from '../dashboard/Criteria';
import { ClampText, Count, PillButton } from '../../components/ui';

const OUT_C: Record<LeadOutcome, string> = { created: 'var(--s-committed)', updated: 'var(--s-leased)', skipped: 'var(--fg3)', waiting: 'var(--s-input)', failed: 'var(--s-rejected)' };
const OUT_LABEL: Record<LeadOutcome, string> = { created: 'Created', updated: 'Updated', skipped: 'Skipped', waiting: 'Waiting on you', failed: 'Failed' };
const mmss = (s: number | null | undefined) => (s == null ? '—' : `${Math.floor(s / 60)}:${String(Math.round(s % 60)).padStart(2, '0')}`);
const f2 = (v: number | null | undefined) => (v == null ? '—' : v.toFixed(2));
const ms = (v: number | null | undefined) => (v == null ? '—' : v >= 1000 ? `${(v / 1000).toFixed(1)}s` : `${v}ms`);
const ktok = (v: number | null | undefined) => (v == null ? '—' : v >= 1000 ? `${Math.round(v / 1000)}k` : String(v));
const HEAD = 'px-4 py-[9px] bg-panel2 border-b border-line text-xs font-medium text-fg3 uppercase tracking-[0.04em]';

function Section({ id, title, note, mono, children, right }: { id: string; title: string; note?: ReactNode; mono?: boolean; children: ReactNode; right?: ReactNode }) {
  return (
    <section id={id} className="flex flex-col gap-2.5 break-inside-avoid-page">
      <div className="flex items-baseline gap-2.5 flex-wrap">
        <span className="text-lg font-semibold">{title}</span>
        {note && <span className={`${mono ? 'mono' : ''} text-sm text-fg3`}>{note}</span>}
        {right && <><div className="flex-1" />{right}</>}
      </div>
      {children}
    </section>
  );
}

const Box = ({ children, className = '' }: { children: ReactNode; className?: string }) => (
  <div className={`bg-panel border border-line rounded-[10px] overflow-hidden ${className}`}>{children}</div>
);

function Timeline({ r }: { r: Report }) {
  const waiting = r.phases.some((p) => p.end_s == null);
  const lastEnd = Math.max(r.duration_s ?? 0, ...r.phases.map((p) => p.end_s ?? 0), ...r.faults.map((f) => f.at_s));
  const total = Math.max(1, waiting ? lastEnd + 30 : lastEnd);
  const pct = (v: number) => `${((v / total) * 100).toFixed(2)}%`;
  const ticks = [0, 60, 120, 180, 240, 300, 360, 420, 480, 600].filter((t) => t < total - total * 0.08).concat([total]);
  if (!r.phases.length) return <Box className="px-4 py-3.5 text-sm text-fg3">No phases recorded yet.</Box>;
  return (
    <Box className="px-4 py-3.5 flex flex-col gap-[7px]">
      <div className="grid grid-cols-[200px_minmax(0,1fr)] gap-3.5">
        <span />
        <div className="relative h-[26px] border-b border-line">
          {r.faults.map((f, i) => (
            <div key={i}>
              <div title={`${mmss(f.at_s)} ${f.fault}`} className="absolute top-0 w-0 pointer-events-none" style={{ left: pct(f.at_s), bottom: -(r.phases.length * 23 + 10), borderLeft: '1px dashed color-mix(in oklch, var(--s-rejected) 55%, transparent)' }} />
              <span className="absolute -translate-x-1/2 mono text-[9.5px] whitespace-nowrap bg-panel px-[3px]" style={{ left: pct(f.at_s), top: i % 2 ? 13 : 0, color: 'var(--s-rejected)' }}>{f.fault}</span>
            </div>
          ))}
        </div>
      </div>
      {r.phases.map((p, i) => {
        const end = p.end_s ?? total;
        const c = p.kind === 'human' ? 'var(--s-input)' : p.kind === 'review' ? 'color-mix(in oklch, var(--s-input) 55%, var(--s-leased))' : p.kind === 'verify' ? 'var(--s-committed)' : 'var(--s-leased)';
        return (
          <div key={i} className="grid grid-cols-[200px_minmax(0,1fr)] gap-3.5 items-center">
            <div className="flex flex-col min-w-0">
              <span className="text-sm+ truncate">{p.label}</span>
              <span className="mono text-2xs text-fg3">{p.agent}</span>
            </div>
            <div className="relative h-4 rounded-[3px] bg-panel2">
              <div className="absolute top-0 bottom-0 rounded-[3px]" style={{ left: pct(p.start_s), width: pct(Math.max(0.5, end - p.start_s)), background: c }} />
              <span className="absolute top-0 mono text-2xs leading-4 text-fg3 whitespace-nowrap" style={{ left: `calc(${pct(Math.min(end, total * 0.9))} + 6px)` }}>{p.end_s == null ? 'open' : mmss(end - p.start_s)}</span>
            </div>
          </div>
        );
      })}
      <div className="grid grid-cols-[200px_minmax(0,1fr)] gap-3.5">
        <span />
        <div className="relative h-3.5 mono text-2xs text-fg3">
          {ticks.map((t, i) => (
            <span key={t} className="absolute whitespace-nowrap" style={{ left: pct(t), transform: `translateX(${i === 0 ? '0' : i === ticks.length - 1 ? '-100%' : '-50%'})` }}>{mmss(t)}</span>
          ))}
        </div>
      </div>
    </Box>
  );
}

export default function ReportView() {
  const report = useLedger((s) => s.report);
  const error = useLedger((s) => s.reportError);
  const run = useLedger((s) => s.run);
  const runId = useLedger((s) => s.runId);
  const load = useLedger((s) => s.loadReport);
  const [filter, setFilter] = useState<'all' | LeadOutcome>('all');

  useEffect(() => {
    void load();
    const t = setInterval(() => {
      const st = useLedger.getState().run?.status;
      if (st !== 'completed' && st !== 'failed') void load();
    }, 5000);
    return () => clearInterval(t);
  }, [load, runId]);

  const counts = useMemo(() => {
    const c: Record<LeadOutcome, number> = { created: 0, updated: 0, skipped: 0, waiting: 0, failed: 0 };
    report?.leads.forEach((l) => (c[l.outcome] = (c[l.outcome] ?? 0) + 1));
    return c;
  }, [report]);

  if (!runId) return <div className="p-10 text-fg3">No run selected.</div>;
  if (!report) {
    return (
      <div className="max-w-[760px] mx-auto p-10 flex flex-col gap-2">
        <span className="text-lg font-semibold">Evidence report · {runId}</span>
        <span className="text-fg2 text-sm+">{error ? `The report is not available yet (${error}).` : 'Loading report…'}</span>
        {error && <button type="button" onClick={() => void load()} className="self-start h-8 px-3 border border-line2 rounded-md bg-panel text-sm+">Retry</button>}
      </div>
    );
  }

  const r = report;
  const rv = runVisual(r.status);
  // an open decision (not committed yet) is never "decided automatically"
  const autoN = r.decisions.filter((d) => (d.state ? d.state === 'auto' : d.decided_by !== 'you' && !d.escalated)).length;
  const sent = r.emails.filter((e) => /^sent/.test(e.delivery)).length;
  const lost = r.totals?.lost_s ?? r.faults.reduce((n, f) => n + (f.lost_s ?? 0), 0);
  const dups = r.totals?.duplicates ?? 0;
  const tokens = r.totals?.tokens ?? r.agents.reduce((n, a) => n + (a.tokens ?? 0), 0);
  const cost = r.totals?.cost_usd ?? r.agents.reduce((n, a) => n + (a.cost_usd ?? 0), 0);
  const requests = r.totals?.requests ?? r.agents.reduce((n, a) => n + (a.requests ?? 0), 0);
  const rowCount = r.input?.rows ?? r.leads.length;
  const stats: [string, string, string, string][] = [
    [String(counts.created + counts.updated), 'contacts verified', 'var(--s-committed)', `REST · ${dups} duplicates`],
    [String(counts.skipped), 'skipped with reason', 'var(--fg2)', 'logged per row'],
    [`${autoN}/${r.decisions.length}`, 'decided automatically', 'var(--s-leased)', 'meta-reviewer'],
    [String(counts.waiting), 'waiting on you', 'var(--s-input)', counts.waiting ? 'see Decisions' : 'none'],
    [String(sent), 'emails sent', 'var(--s-committed)', 'confirmed in Mailpit'],
    [String(r.faults.length), 'faults recovered', 'var(--s-rejected)', `${mmss(lost)} lost`],
  ];
  const leads = r.leads.filter((l) => filter === 'all' || l.outcome === filter);
  const exportMd = async () => {
    let md = r.markdown;
    if (!md) {
      try { md = await api().getReportMarkdown(r.run_id); } catch { md = ''; }
    }
    if (!md || md.length < 80) md = reportToMarkdown(r);
    const blob = new Blob([md], { type: 'text/markdown' });
    const a = document.createElement('a');
    a.href = URL.createObjectURL(blob);
    a.download = `${r.run_id}-evidence.md`;
    a.click();
    setTimeout(() => URL.revokeObjectURL(a.href), 1000);
  };
  const toc: [string, string][] = [['rep-summary', 'Summary'], ['rep-timeline', 'Timeline'], ['rep-leads', 'Every row'], ['rep-decisions', 'Decisions'], ['rep-criteria', 'Criteria'], ['rep-verification', 'Verification'], ['rep-emails', 'Emails'], ['rep-recoveries', 'Recoveries'], ['rep-agents', 'Agents and cost'], ['rep-data', 'Input file'], ['rep-repro', 'Reproduce']];
  const repro = r.reproduce ?? {};
  const cfg = repro.config;
  const reproRows: [string, string][] = [
    ['determinism', cfg ? `${cfg.determinism?.toFixed(2)} · seed ${cfg.seed_pinned ? `${cfg.seed} (pinned)` : 'random'}` : '—'],
    ['playbook', r.playbook ?? run?.playbook ?? '—'],
    ['input', `${r.input?.file ?? run?.input_file ?? '—'} · sha256 ${(r.input?.sha256 ?? run?.input_sha256 ?? '—').slice(0, 12)}…`],
    ['prompts', repro.prompts ? Object.entries(repro.prompts).map(([k, v]) => `${k} v${v}`).join(' · ') : '—'],
    ['models', repro.models ? Object.entries(repro.models).map(([k, v]) => `${k} ${v.map(shortModel).join(' → ')}`).join(' · ') : '—'],
    ['thresholds', cfg ? `ambiguity ${cfg.review_auto_threshold?.toFixed(2)} · email ${cfg.approval_auto_threshold?.toFixed(2)} · fuzzy ${cfg.fuzzy_match_threshold?.toFixed(2)}` : '—'],
    ['reliability', cfg ? `lease ${cfg.lease_ttl_s}s · max attempts ${cfg.max_attempts} · check-then-act ${cfg.check_then_act ? 'on' : 'off'} · replan after ${cfg.replan_after_rejections}` : '—'],
    ['faults injected', repro.faults?.join(', ') || r.faults.map((f) => f.fault).join(', ') || 'none'],
    ['config_hash', repro.config_hash ?? run?.config_hash ?? '—'],
  ];

  return (
    <div className="w-full max-w-[1280px] mx-auto px-7 pt-7 pb-16 grid gap-8 items-start grid-cols-[180px_minmax(0,1fr)] print:block" data-testid="report">
      <nav data-noprint className="sticky top-2 flex flex-col gap-0.5 pt-1">
        <span className="mono text-2xs text-fg3 uppercase tracking-[0.06em] px-2.5 pb-2">Report</span>
        {toc.map(([id, label]) => (
          <a key={id} href={`#${id}`} onClick={(e) => { e.preventDefault(); document.getElementById(id)?.scrollIntoView({ behavior: 'smooth' }); }} className="px-2.5 py-1.5 rounded-md text-sm+ text-fg2 hover:bg-bg2 hover:text-fg">{label}</a>
        ))}
      </nav>
      <main className="min-w-0 flex flex-col gap-8">
        <section id="rep-summary" className="flex flex-col gap-4">
          <div className="flex items-start justify-between gap-5 flex-wrap">
            <div className="flex flex-col gap-2 max-w-[760px]">
              <span className="mono text-sm text-fg3">Evidence report · {r.run_id} · <span style={{ color: rv.c }}>{rv.label}</span> · {mmss(r.duration_s)}</span>
              <span className="text-3xl font-semibold text-pretty">{r.summary || 'Summary not available yet.'}</span>
              <span className="text-base text-fg2">Goal: {r.goal || run?.goal} · Playbook: {r.playbook ?? run?.playbook ?? '—'}</span>
            </div>
            <div data-noprint className="flex gap-1.5">
              <button type="button" onClick={() => void exportMd()} className="h-8 px-3 border border-line2 rounded-md bg-panel text-fg text-sm+ whitespace-nowrap">Export .md</button>
              <button type="button" onClick={() => window.print()} className="h-8 px-3 border-0 rounded-md text-sm+ font-medium whitespace-nowrap" style={{ background: 'var(--fg)', color: 'var(--bg)' }}>Print / PDF</button>
            </div>
          </div>
          <div className="grid gap-2.5" style={{ gridTemplateColumns: 'repeat(auto-fit, minmax(130px, 1fr))' }}>
            {stats.map(([n, label, c, sub]) => (
              <div key={label} className="bg-panel border border-line rounded-lg px-3.5 py-3 flex flex-col gap-[3px]">
                <span className="mono text-3xl" style={{ color: c }}>{n}</span>
                <span className="text-sm text-fg2">{label}</span>
                <span className="text-xs text-fg3">{sub}</span>
              </div>
            ))}
          </div>
          <div className="flex flex-col gap-2">
            <div className="flex h-2.5 rounded-[5px] overflow-hidden gap-0.5">
              {(Object.keys(OUT_C) as LeadOutcome[]).filter((k) => counts[k]).map((k) => <div key={k} title={OUT_LABEL[k]} style={{ flex: `${counts[k]} 1 0`, background: OUT_C[k] }} />)}
            </div>
            <div className="flex flex-wrap gap-x-[18px] gap-y-1.5 text-sm text-fg2">
              {(Object.keys(OUT_C) as LeadOutcome[]).filter((k) => counts[k]).map((k) => (
                <span key={k} className="inline-flex items-center gap-1.5"><span className="w-2 h-2 rounded-[2px]" style={{ background: OUT_C[k] }} />{OUT_LABEL[k]} <span className="mono text-fg">{counts[k]}</span></span>
              ))}
              <span className="text-fg3">{rowCount} rows in the file</span>
            </div>
          </div>
        </section>

        <Section id="rep-timeline" title="Timeline" note="Phases by agent. Red ticks are injected faults; each is followed by its recovery.">
          <Timeline r={r} />
        </Section>

        <Section id="rep-leads" title="Every row in the file" note="What happened to each lead, who decided, and how it was checked." right={
          <div data-noprint className="flex gap-1">
            <PillButton size="sm" on={filter === 'all'} onClick={() => setFilter('all')}>All <Count>{r.leads.length}</Count></PillButton>
            {(['created', 'updated', 'skipped', 'waiting'] as LeadOutcome[]).map((k) => (
              <PillButton key={k} size="sm" on={filter === k} onClick={() => setFilter(k)}>{k === 'waiting' ? 'Waiting' : OUT_LABEL[k]} <Count>{counts[k]}</Count></PillButton>
            ))}
          </div>
        }>
          <Box className="overflow-x-auto">
            <div className="min-w-[960px]">
              <div className={`grid gap-3 ${HEAD}`} style={{ gridTemplateColumns: '30px 56px minmax(150px,1.2fr) minmax(150px,1.3fr) 76px 92px 84px minmax(140px,1fr) 56px' }}>
                <span>#</span><span /><span>Lead</span><span>Outcome</span><span>Owner</span><span>Task</span><span>Email</span><span>Decided / verified</span><span>CRM</span>
              </div>
              {leads.map((l) => {
                const has = l.outcome === 'created' || l.outcome === 'updated';
                return (
                  <div key={`${l.n}-${l.name}`} className="grid gap-3 items-center px-4 py-[9px] border-b border-line" style={{ gridTemplateColumns: '30px 56px minmax(150px,1.2fr) minmax(150px,1.3fr) 76px 92px 84px minmax(140px,1fr) 56px' }}>
                    <span className="mono text-xs text-fg3">{l.n}</span>
                    {l.screenshot ? (
                      <a href={api().evidenceUrl(l.screenshot)} target="_blank" rel="noreferrer"><img src={api().evidenceUrl(l.screenshot)} alt={`row ${l.n}`} className="w-14 h-[34px] rounded border border-line object-cover bg-panel2" /></a>
                    ) : (
                      <div className="w-14 h-[34px] rounded border border-line" style={{ background: 'repeating-linear-gradient(135deg, var(--panel2) 0 4px, var(--bg2) 4px 8px)', opacity: has ? 1 : 0.35 }} />
                    )}
                    <div className="flex flex-col min-w-0">
                      <span className="text-base font-medium truncate">{l.name}</span>
                      <span className="mono text-2xs text-fg3 truncate">{l.company || '—'} · {l.email || '—'}</span>
                    </div>
                    <div className="flex flex-col items-start gap-0.5 min-w-0" title={l.outcome_detail ?? undefined}>
                      <span className="inline-flex items-center px-2 py-0.5 rounded-[10px] text-xs font-medium whitespace-nowrap tint-11" style={{ ['--c' as string]: OUT_C[l.outcome] }}>{OUT_LABEL[l.outcome] ?? l.outcome}</span>
                      {l.outcome_detail && <span data-testid="outcome-detail" className="text-2xs text-fg3 truncate max-w-full">{l.outcome_detail}</span>}
                    </div>
                    <span className="mono text-xs+ text-fg2 truncate">{l.owner ?? '—'}</span>
                    <span className="mono text-xs+ text-fg2 truncate">{l.task_due ?? '—'}</span>
                    <span className="mono text-xs whitespace-nowrap" style={{ color: /^sent/.test(l.email_status ?? '') ? 'var(--s-committed)' : l.email_status === 'waiting' ? 'var(--s-input)' : 'var(--fg3)' }}>{l.email_status ?? '—'}</span>
                    <div className="flex flex-col min-w-0">
                      <span className="text-sm truncate">{l.decided_by ?? '—'}</span>
                      <span className="mono text-2xs text-fg3 truncate" title={l.check}>{l.check}</span>
                    </div>
                    {l.crm_url ? <a href={l.crm_url} target="_blank" rel="noreferrer" className="mono text-xs+">open ↗</a> : <span />}
                  </div>
                );
              })}
            </div>
          </Box>
        </Section>

        <Section id="rep-decisions" title="Decisions" note="Confidence against the threshold in force. The line marks the threshold.">
          <Box>
            {r.decisions.length === 0 && <div className="px-4 py-3 text-sm text-fg3">No review decisions in this run.</div>}
            {r.decisions.map((d, i) => {
              // an open (undecided) review has no confidence/threshold yet
              const conf = d.confidence ?? null, thr = d.threshold ?? null;
              const auto = !d.escalated && d.decided_by !== 'you' && conf != null && thr != null && conf >= thr;
              return (
                <div key={i} className="grid gap-4 items-center px-4 py-3 border-b border-line" style={{ gridTemplateColumns: 'minmax(0,1.2fr) 200px minmax(0,1fr) 110px' }}>
                  <div className="flex flex-col gap-0.5 min-w-0">
                    <span className="text-base font-medium">{d.title}</span>
                    <ClampText text={d.evidence.join(' · ')} lines={2} className="text-xs+ text-fg3 text-pretty" />
                  </div>
                  <div className="flex flex-col gap-1">
                    <div className="relative h-2 rounded bg-bg2">
                      <div className="absolute left-0 top-0 bottom-0 rounded" style={{ width: `${Math.round((conf ?? 0) * 100)}%`, background: auto ? 'var(--s-committed)' : 'var(--s-input)' }} />
                      {thr != null && <div className="absolute -top-[3px] -bottom-[3px] w-0.5" style={{ left: `${Math.round(thr * 100)}%`, background: 'var(--fg)' }} />}
                    </div>
                    <span className="mono text-2xs text-fg3">{d.forced_reason ? `${f2(conf)} · ${d.forced_reason}` : `${f2(conf)} vs ${f2(thr)}`}</span>
                  </div>
                  <span className="text-sm+ text-pretty">{d.result}</span>
                  <span className="mono text-xs text-fg3 text-right">{d.decided_by}</span>
                </div>
              );
            })}
          </Box>
        </Section>

        <Section id="rep-criteria" title="Success criteria">
          <Box>
            {r.criteria.map((c) => {
              const col = c.status === 'verified' ? 'var(--s-committed)' : c.status === 'waived' ? 'var(--fg3)' : c.status === 'failed' ? 'var(--s-rejected)' : 'var(--s-claimed)';
              return (
                <div key={c.id} className="grid grid-cols-[90px_minmax(0,1fr)_minmax(0,1fr)] gap-3.5 px-4 py-[11px] border-b border-line items-start">
                  <span><span className="inline-flex px-2 py-0.5 rounded-[10px] text-xs font-medium tint-11" style={{ ['--c' as string]: col }}>{CRITERION_STYLE[c.status]?.icon} {c.status}</span></span>
                  <span className="text-base text-pretty">{c.text}</span>
                  <ClampText text={c.evidence ?? c.check} lines={2} className="mono text-xs+ text-fg2 text-pretty" />
                </div>
              );
            })}
          </Box>
        </Section>

        <Section id="rep-verification" title="Verification coverage" note="Every claim checked through a different channel from the one used to act.">
          <Box>
            <div className={`grid gap-3 ${HEAD}`} style={{ gridTemplateColumns: 'minmax(0,1.3fr) 150px 60px 60px 70px 70px' }}><span>Check</span><span>Channel</span><span className="text-right">Runs</span><span className="text-right">Pass</span><span className="text-right">Reject</span><span className="text-right">p50</span></div>
            {r.coverage.map((c) => (
              <div key={c.check} className="grid gap-3 px-4 py-[9px] border-b border-line mono text-xs+" style={{ gridTemplateColumns: 'minmax(0,1.3fr) 150px 60px 60px 70px 70px' }}>
                <span>{c.check}</span><span className="text-fg2">{c.channel}</span><span className="text-right">{c.runs}</span><span className="text-right" style={{ color: 'var(--s-committed)' }}>{c.pass}</span><span className="text-right" style={{ color: c.reject ? 'var(--s-rejected)' : 'var(--fg3)' }}>{c.reject}</span><span className="text-right text-fg2">{ms(c.p50_ms)}</span>
              </div>
            ))}
          </Box>
        </Section>

        <Section id="rep-emails" title="Follow-up emails" note={sent ? `${sent} sent via SMTP, each confirmed in Mailpit by the verifier.` : 'Not sent yet.'}>
          <Box>
            <div className={`grid gap-3 ${HEAD}`} style={{ gridTemplateColumns: 'minmax(0,1fr) minmax(0,1.3fr) 70px 110px 130px' }}><span>Recipient</span><span>Subject</span><span className="text-right">Judge</span><span>Approval</span><span>Delivery</span></div>
            {r.emails.map((e) => (
              <div key={e.to} className="grid gap-3 items-center px-4 py-[9px] border-b border-line" style={{ gridTemplateColumns: 'minmax(0,1fr) minmax(0,1.3fr) 70px 110px 130px' }}>
                <span className="mono text-xs+ truncate">{e.to}</span>
                <span className="text-sm text-fg2 truncate">{e.subject}</span>
                <span className="mono text-xs+ text-right">{e.judge != null ? e.judge.toFixed(2) : '—'}</span>
                <span className="mono text-xs text-fg2">{e.approval}</span>
                <span className="mono text-xs" style={{ color: /^sent/.test(e.delivery) ? 'var(--s-committed)' : 'var(--fg3)' }}>{e.delivery}</span>
              </div>
            ))}
          </Box>
        </Section>

        <Section id="rep-recoveries" title="What went wrong and how it recovered" note={`${mmss(lost)} lost in total · ${dups} duplicates`} mono>
          <Box>
            {r.faults.length === 0 && <div className="px-4 py-3 text-sm text-fg3">No faults in this run.</div>}
            {r.faults.map((f, i) => (
              <div key={i} className="grid gap-3.5 px-4 py-3 border-b border-line items-start" style={{ gridTemplateColumns: '50px 120px minmax(0,1fr) minmax(0,1fr) 50px' }}>
                <span className="mono text-xs text-fg3">{mmss(f.at_s)}</span>
                <span className="mono text-xs+" style={{ color: 'var(--s-rejected)' }}>{f.fault}</span>
                <span className="text-sm+ text-fg2 text-pretty">{f.what}</span>
                <span className="text-sm+ text-pretty">{f.recovery}</span>
                <span className="mono text-xs+ text-fg2 text-right">{mmss(f.lost_s)}</span>
              </div>
            ))}
          </Box>
        </Section>

        <Section id="rep-agents" title="Agents and cost" note={`${ktok(tokens)} tokens · $${cost.toFixed(2)} · ${requests} LLM requests`} mono>
          <Box>
            <div className={`grid gap-3 ${HEAD}`} style={{ gridTemplateColumns: 'minmax(0,1fr) minmax(0,1.4fr) 56px 64px 60px 60px 64px 64px' }}><span>Agent</span><span>Model</span><span className="text-right">Steps</span><span className="text-right">Reject</span><span className="text-right">Retry</span><span className="text-right">p50</span><span className="text-right">Tokens</span><span className="text-right">Cost</span></div>
            {r.agents.map((a) => (
              <div key={a.agent} className="grid gap-3 px-4 py-[9px] border-b border-line mono text-xs+" style={{ gridTemplateColumns: 'minmax(0,1fr) minmax(0,1.4fr) 56px 64px 60px 60px 64px 64px' }}>
                <span className="font-sans text-sm+">{a.agent}</span><span className="text-fg2 truncate" title={a.model}>{a.model}</span><span className="text-right">{a.steps}</span><span className="text-right text-fg2">{a.rejections}</span><span className="text-right text-fg2">{a.retries}</span><span className="text-right text-fg2">{ms(a.p50_ms)}</span><span className="text-right text-fg2">{ktok(a.tokens)}</span><span className="text-right">{a.cost_usd != null ? `$${a.cost_usd.toFixed(2)}` : '—'}</span>
              </div>
            ))}
          </Box>
        </Section>

        <Section id="rep-data" title="Input file" note={`${r.input?.file ?? '—'} · ${r.input?.rows ?? '—'} rows · sha256 ${(r.input?.sha256 ?? '—').slice(0, 4)}…${(r.input?.sha256 ?? '').slice(-2)}`} mono>
          <div className="grid gap-2.5" style={{ gridTemplateColumns: 'repeat(auto-fit, minmax(160px, 1fr))' }}>
            {(r.input?.quality ?? []).map((q) => (
              <div key={q.label} className="bg-panel border border-line rounded-lg px-3 py-2.5 flex flex-col gap-[3px]">
                <span className="mono text-xl">{q.n}</span>
                <span className="text-sm text-fg2">{q.label}</span>
                {q.example && <span className="mono text-2xs text-fg3">{q.example}</span>}
              </div>
            ))}
          </div>
        </Section>

        <Section id="rep-repro" title="Reproduce this run">
          <Box>
            {reproRows.map(([k, v]) => (
              <div key={k} className="grid grid-cols-[180px_minmax(0,1fr)] gap-3.5 px-4 py-2 border-b border-line mono text-xs+"><span className="text-fg3">{k}</span><span className="[overflow-wrap:anywhere]">{v}</span></div>
            ))}
            <div className="px-4 py-3 bg-panel2 mono text-sm">$ {repro.command ?? `make replay RUN=${r.run_id}`}{cfg ? ` SEED=${cfg.seed_pinned ? cfg.seed : 'random'} DETERMINISM=${cfg.determinism?.toFixed(2)}` : ''}</div>
          </Box>
        </Section>
      </main>
    </div>
  );
}
