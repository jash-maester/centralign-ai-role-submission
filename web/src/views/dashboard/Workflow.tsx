import { lazy, Suspense, useMemo, useState } from 'react';
import { useLedger } from '../../store/store';
import { agentView, buildLanes, initials, LANE_COLUMNS, shortModel, type AgentView, type Lane } from '../../lib/derive';
import { Chip, Count, Dot, Empty, PillButton, TtlRing, useNow } from '../../components/ui';
import type { AgentStatus } from '../../api/types';

const StepGraph = lazy(() => import('./StepGraph'));

type Tab = 'agents' | 'steps' | 'graph';
const AGENT_GRID = 'minmax(180px,1.4fr) 90px 150px minmax(160px,1.3fr) 170px 90px 60px 64px 170px';
const STEP_GRID = 'minmax(180px,1.3fr) repeat(5, minmax(120px,1fr)) 70px 150px';

export function agentTabs(a: AgentStatus | { id: string; role?: string; model_role?: string | null }): string[] {
  if (a.id === 'human' || a.role === 'human') return ['Inbox', 'Overview'];
  if (a.id === 'playbook') return ['Overview', 'Content'];
  if (a.id === 'ledger') return ['Overview', 'Streams', 'Shell'];
  if (a.id === 'verifier') return ['Overview', 'Prompt', 'Checks', 'Tools', 'Shell'];
  if (!a.model_role) return ['Overview', 'Tools', 'Shell'];
  return ['Overview', 'Prompt', 'Tools', 'Shell'];
}

function useAgentViews(): { a: AgentStatus; v: AgentView }[] {
  const agents = useLedger((s) => s.agents);
  const agentsAt = useLedger((s) => s.agentsAt);
  const ttl = useLedger((s) => s.config.lease_ttl_s);
  const esc = useLedger((s) => s.escalations.length);
  const now = useNow(1000);
  return agents.map((a) => ({ a, v: agentView(a, now, agentsAt, ttl, esc) }));
}

function FlowStrip({ onWorkers }: { onWorkers: () => void }) {
  const showAgent = useLedger((s) => s.showAgent);
  const nSteps = useLedger((s) => s.stepOrder.length);
  const nEvents = useLedger((s) => s.events.length);
  const steps = useLedger((s) => s.steps);
  const th = useLedger((s) => s.config.review_auto_threshold);
  const esc = useLedger((s) => s.escalations.length);
  const run = useLedger((s) => s.run);
  const views = useAgentViews();
  const commits = useMemo(() => Object.values(steps).filter((x) => x.status === 'committed').length, [steps]);
  const workers = views.filter((x) => x.v.group === 'worker');
  const byId = (id: string) => views.find((x) => x.a.id === id)?.v.c ?? 'var(--fg3)';
  const items: { name: string; meta: string; c: string; open: () => void; arrow: string }[] = [
    { name: 'Playbook', meta: run?.playbook_hash ? `#${run.playbook_hash.slice(0, 4)}` : (run?.playbook ?? '—'), c: 'var(--s-committed)', open: () => showAgent('playbook'), arrow: '→' },
    { name: 'Orchestrator', meta: `${nSteps} steps`, c: byId('orchestrator'), open: () => showAgent('orchestrator'), arrow: '→' },
    { name: 'Ledger', meta: `${nEvents.toLocaleString()} ev`, c: 'var(--s-committed)', open: () => showAgent('ledger'), arrow: '⇄' },
    { name: 'Workers', meta: `${workers.length} workers`, c: workers.some((w) => w.v.stale) ? 'var(--s-dead)' : 'var(--s-leased)', open: onWorkers, arrow: '→' },
    { name: 'Verifier', meta: `${commits} commits`, c: byId('verifier'), open: () => showAgent('verifier'), arrow: '→' },
    { name: 'Meta-reviewer', meta: `auto ≥ ${th.toFixed(2)}`, c: byId('meta-reviewer'), open: () => showAgent('meta-reviewer'), arrow: '→' },
    { name: 'Human (you)', meta: esc ? `${esc} escalation${esc > 1 ? 's' : ''}` : 'escalations only', c: esc ? 'var(--s-input)' : 'var(--fg3)', open: () => showAgent('human', 'Inbox'), arrow: '' },
  ];
  return (
    <div className="flex items-center gap-1.5 px-[18px] py-3 border-b border-line bg-panel2 flex-wrap gap-y-2">
      {items.map((it) => (
        <div key={it.name} className="flex items-center gap-1.5 flex-none">
          <button type="button" onClick={it.open} className="flex items-center gap-2 h-8 px-3 border border-line rounded-2xl bg-panel text-fg text-sm+ whitespace-nowrap hover:border-line2">
            <Dot c={it.c} />
            <span className="font-medium">{it.name}</span>
            <span className="mono text-xs text-fg3">{it.meta}</span>
          </button>
          <span className="text-fg3 text-sm">{it.arrow}</span>
        </div>
      ))}
    </div>
  );
}

function AgentsTable({ rows }: { rows: { a: AgentStatus; v: AgentView }[] }) {
  const showAgent = useLedger((s) => s.showAgent);
  return (
    <div className="overflow-x-auto" data-testid="agents-table">
      <div className="min-w-[1000px]">
        <div className="grid gap-3.5 px-[18px] py-[9px] bg-panel2 border-b border-line text-xs font-medium text-fg3 uppercase tracking-[0.04em]" style={{ gridTemplateColumns: AGENT_GRID }}>
          <span>Agent</span><span>Role</span><span>Status</span><span>Current step</span><span>Model</span><span>Heartbeat</span><span className="text-right">Done</span><span className="text-right">Reject</span><span />
        </div>
        {rows.map(({ a, v }) => {
          const done = a.steps_done ?? 0, rej = a.rejections ?? 0;
          const rate = a.role === 'worker' ? (done + rej ? `${Math.round((rej / (done + rej)) * 100)}%` : '0%') : '—';
          return (
            <div
              key={a.id}
              onClick={() => showAgent(a.id)}
              className="grid gap-3.5 items-center px-[18px] py-2.5 border-b border-line cursor-pointer hover:bg-panel2"
              style={{ gridTemplateColumns: AGENT_GRID, opacity: v.stale ? 0.7 : 1 }}
            >
              <div className="flex items-center gap-2.5 min-w-0">
                <span className="flex-none w-7 h-7 rounded-md bg-bg2 grid place-items-center mono text-xs font-medium text-fg2">{initials(a.name)}</span>
                <div className="flex flex-col min-w-0">
                  <span className="text-base font-medium truncate">{a.name}</span>
                  <span className="mono text-2xs text-fg3">{a.id}</span>
                </div>
              </div>
              <span className="text-sm text-fg2">{v.roleLabel}</span>
              <span><Chip c={v.c} label={v.state} /></span>
              <span className="mono text-xs+ text-fg2 truncate flex items-center gap-1.5">
                <TtlRing frac={v.ttlFrac} title={v.ttlS != null ? `lease ttl ${v.ttlS.toFixed(1)}s` : undefined} />
                <span className="truncate">{v.step}</span>
              </span>
              <span className="mono text-xs+ text-fg2 truncate" title={(a.models ?? []).join(' → ')}>{v.model.split(' ').map((x, i) => (i === 0 ? shortModel(x) : x)).join(' ')}</span>
              <span className="mono text-xs+" style={{ color: v.stale ? 'var(--s-rejected)' : 'var(--fg2)' }}>{v.hbAge == null ? (a.role === 'human' ? 'now' : '—') : `${v.hbAge}s`}</span>
              <span className="mono text-sm text-right">{a.steps_done ?? '—'}</span>
              <span className="mono text-sm text-right text-fg2">{rate}</span>
              <div className="flex gap-1 justify-end">
                {agentTabs(a).filter((t) => ['Prompt', 'Tools', 'Shell', 'Inbox'].includes(t)).map((t) => (
                  <button key={t} type="button" onClick={(e) => { e.stopPropagation(); showAgent(a.id, t); }} className="h-6 px-2 border border-line2 rounded-[5px] bg-panel text-fg2 text-xs+ hover:text-fg hover:border-fg3">{t}</button>
                ))}
              </div>
            </div>
          );
        })}
      </div>
    </div>
  );
}

function StepsTable({ lanes }: { lanes: Lane[] }) {
  const openStep = useLedger((s) => s.openStep);
  const showAgent = useLedger((s) => s.showAgent);
  const agents = useLedger((s) => s.agents);
  const nameOf = (id: string | null) => (id ? agents.find((a) => a.id === id)?.name ?? id : '—');
  return (
    <div className="overflow-x-auto" data-testid="steps-table">
      <div className="min-w-[1000px]">
        <div className="grid gap-3 px-[18px] py-[9px] bg-panel2 border-b border-line text-xs font-medium text-fg3 uppercase tracking-[0.04em]" style={{ gridTemplateColumns: STEP_GRID }}>
          <span>Lead</span><span>Search / review</span><span>Create / update</span><span>Task</span><span>Draft</span><span>Send</span><span className="text-right">Tries</span><span>Held by</span>
        </div>
        {lanes.map((l) => (
          <div key={l.lane} className="grid gap-3 items-center px-[18px] py-2.5 border-b border-line hover:bg-panel2" style={{ gridTemplateColumns: STEP_GRID }}>
            <div className="flex flex-col min-w-0">
              <span className="text-base font-medium truncate">{l.lead.name}</span>
              <span className="text-xs+ text-fg3 truncate">{[l.lead.company, l.lead.email || l.lead.phone].filter(Boolean).join(' · ') || l.lane}</span>
            </div>
            {LANE_COLUMNS.map((col) => {
              const cell = l.cells[col];
              return (
                <span key={col}>
                  {cell.step ? (
                    <button type="button" onClick={() => openStep(cell.step!.id)} className="border-0 bg-transparent p-0" title={`${cell.step.id} · ${cell.step.kind}`}>
                      <Chip c={cell.c} label={cell.label} className={cell.v === 'leased' ? 'pulse' : ''} />
                    </button>
                  ) : (
                    <span className="mono text-xs text-fg3">—</span>
                  )}
                </span>
              );
            })}
            <span className="mono text-sm text-right" style={{ color: l.tries > 1 ? 'var(--s-claimed)' : 'var(--fg2)' }}>{l.tries}</span>
            <button type="button" onClick={() => l.holder && showAgent(l.holder, l.holder === 'human' ? 'Inbox' : undefined)} className="justify-self-start border-0 bg-transparent p-0 text-accent mono text-xs+ whitespace-nowrap">
              {nameOf(l.holder)}
            </button>
          </div>
        ))}
      </div>
    </div>
  );
}

export function Workflow() {
  const [tab, setTab] = useState<Tab>('agents');
  const [q, setQ] = useState('');
  const [filter, setFilter] = useState('all');
  const steps = useLedger((s) => s.steps);
  const order = useLedger((s) => s.stepOrder);
  const facts = useLedger((s) => s.facts);
  const setView = useLedger((s) => s.setView);
  const views = useAgentViews();
  const lanes = useMemo(() => buildLanes(steps, order, facts), [steps, order, facts]);
  const ql = q.trim().toLowerCase();

  const agentMatch = (x: { a: AgentStatus; v: AgentView }, f: string) => f === 'all' || (f === 'attention' ? x.v.stale || (x.v.group === 'human' && x.v.state !== 'all clear') : x.v.group === f);
  const laneMatch = (l: Lane, f: string) => f === 'all' || l.category === f;
  const agentsQ = views.filter((x) => !ql || `${x.a.name} ${x.a.id} ${x.v.step} ${x.v.model}`.toLowerCase().includes(ql));
  const lanesQ = lanes.filter((l) => !ql || `${l.lead.name} ${l.lead.company} ${l.lead.email}`.toLowerCase().includes(ql));
  const agentRows = agentsQ.filter((x) => agentMatch(x, filter));
  const laneRows = lanesQ.filter((l) => laneMatch(l, filter));
  const filters: [string, string, number][] = tab === 'agents'
    ? [['all', 'All', 0], ['core', 'Core', 0], ['worker', 'Workers', 0], ['attention', 'Needs attention', 0]].map(([k, label]) => [k as string, label as string, agentsQ.filter((x) => agentMatch(x, k as string)).length])
    : [['all', 'All'], ['active', 'In flight'], ['input', 'Waiting on you'], ['done', 'Done']].map(([k, label]) => [k, label, lanesQ.filter((l) => laneMatch(l, k)).length]);
  const tabs: [Tab, string, string][] = [['agents', 'Agents', String(views.length)], ['steps', 'Steps', `${lanes.length} lanes`], ['graph', 'Graph', `${order.length} steps`]];

  return (
    <section className="bg-panel border border-line rounded-[10px] shadow-card overflow-hidden">
      <div className="flex items-center gap-[18px] px-[18px] border-b border-line">
        <span className="text-md font-semibold py-3.5">Workflow</span>
        <div className="flex gap-1 self-stretch">
          {tabs.map(([k, label, n]) => (
            <button key={k} type="button" data-testid={`wf-${k}`} onClick={() => { setTab(k); setFilter('all'); setQ(''); }} className="flex items-center gap-[7px] px-2.5 border-0 bg-transparent text-base font-medium whitespace-nowrap" style={{ borderBottom: `2px solid ${tab === k ? 'var(--fg)' : 'transparent'}`, color: tab === k ? 'var(--fg)' : 'var(--fg3)' }}>
              {label}<span className="px-1.5 py-px rounded-[10px] bg-bg2 mono text-2xs text-fg2">{n}</span>
            </button>
          ))}
        </div>
        <div className="flex-1" />
        <button type="button" onClick={() => setView('builder')} className="border-0 bg-transparent p-0 text-accent text-sm+ whitespace-nowrap">Edit wiring in builder →</button>
      </div>
      <FlowStrip onWorkers={() => { setTab('agents'); setFilter('worker'); }} />
      {tab !== 'graph' && (
        <div className="flex items-center gap-2.5 px-[18px] py-2.5 border-b border-line flex-wrap">
          <input value={q} onChange={(e) => setQ(e.target.value)} placeholder={tab === 'agents' ? 'Search agents, steps, models…' : 'Search leads…'} className="w-60 h-[30px] px-2.5 border border-line2 rounded-md bg-panel text-fg text-sm+ outline-none" />
          <div className="flex gap-1">
            {filters.map(([k, label, n]) => (
              <PillButton key={k} on={filter === k} onClick={() => setFilter(k)}>{label} <Count>{n}</Count></PillButton>
            ))}
          </div>
          <div className="flex-1" />
          <span className="mono text-xs text-fg3">
            {tab === 'agents' ? `${agentRows.length} of ${views.length} agents` : `${laneRows.length} of ${lanes.length} lead lanes · ${order.length} steps in the graph`}
          </span>
        </div>
      )}
      {tab === 'agents' && (agentRows.length ? <AgentsTable rows={agentRows} /> : <Empty>Nothing matches these filters.</Empty>)}
      {tab === 'steps' && (laneRows.length ? <StepsTable lanes={laneRows} /> : <Empty>{lanes.length ? 'Nothing matches these filters.' : 'No lead lanes yet. They appear when the plan fans out.'}</Empty>)}
      {tab === 'graph' && (
        <Suspense fallback={<Empty>Loading graph…</Empty>}>
          <StepGraph />
        </Suspense>
      )}
    </section>
  );
}
