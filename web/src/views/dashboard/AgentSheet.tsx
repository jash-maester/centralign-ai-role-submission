/** Agent sheet (H9-H11): Overview / Prompt / Tools / Shell + Streams, Checks, Content, Inbox. */
import { lazy, Suspense, useEffect, useMemo, useState } from 'react';
import { api } from '../../api';
import type { AgentConfig, AgentStatus, Playbook, ToolSpec } from '../../api/types';
import { useLedger } from '../../store/store';
import { agentMeta, CHECK_CHANNEL, PSEUDO_AGENTS, TOOL_COLOR, TOOL_LABEL } from '../../lib/agentsMeta';
import { agentView, decisions, fmtClock, shortModel } from '../../lib/derive';
import { temperature } from '../../lib/runConfig';
import { EscButton, Kicker, KV, Sheet, Toggle, useNow } from '../../components/ui';
import { agentTabs } from './Workflow';
import { EscalationCard } from './NeedsAttention';

const ShellTab = lazy(() => import('./ShellTab'));

function useAgentConfig(id: string) {
  const [cfg, setCfg] = useState<AgentConfig | null>(null);
  const [err, setErr] = useState<string | null>(null);
  useEffect(() => {
    let alive = true;
    setCfg(null);
    setErr(null);
    api().getAgentConfig(id).then((c) => alive && setCfg(c)).catch((e) => alive && setErr(String(e.message ?? e)));
    return () => { alive = false; };
  }, [id]);
  const put = async (patch: Parameters<ReturnType<typeof api>['putAgentConfig']>[1]) => {
    try {
      setCfg(await api().putAgentConfig(id, patch));
    } catch (e) {
      useLedger.getState().toast(`Agent config not saved: ${(e as Error).message}`, 'error');
    }
  };
  return { cfg, err, put };
}

function Overview({ a }: { a: AgentStatus }) {
  const meta = agentMeta(a);
  const now = useNow(1000);
  const agentsAt = useLedger((s) => s.agentsAt);
  const config = useLedger((s) => s.config);
  const events = useLedger((s) => s.events);
  const escalations = useLedger((s) => s.escalations);
  const steps = useLedger((s) => s.steps);
  const facts = useLedger((s) => s.facts);
  const toast = useLedger((s) => s.toast);
  const run = useLedger((s) => s.run);
  const v = agentView(a, now, agentsAt, config.lease_ttl_s, escalations.length);
  const ds = useMemo(() => decisions(events, escalations), [events, escalations]);
  const all = Object.values(steps);
  let metrics: [string, string][];
  if (a.id === 'ledger') metrics = [[events.length.toLocaleString(), 'events (this run)'], [String(facts.length), 'facts'], [String(events.filter((e) => e.type === 'step.stale_fence').length), 'stale-token writes']];
  else if (a.id === 'playbook') metrics = [[run?.playbook_hash ? run.playbook_hash.slice(0, 6) : '—', 'hash'], [String(all.length), 'steps it shaped'], [config.review_auto_threshold.toFixed(2), 'review threshold']];
  else if (a.role === 'human') metrics = [[String(escalations.length), 'open'], [String(ds.filter((d) => d.by === 'you').length), 'answered'], ['—', 'avg response']];
  else if (a.role === 'meta_reviewer') metrics = [[String(ds.filter((d) => !d.escalated && d.by !== 'you').length), 'auto-resolved'], [String(ds.filter((d) => d.escalated || d.by === 'you').length), 'escalated'], [config.review_auto_threshold.toFixed(2), 'threshold']];
  else if (a.role === 'verifier') metrics = [[String(all.filter((s) => s.status === 'committed').length), 'commits'], [String(events.filter((e) => e.type === 'step.rejected').length), 'rejections'], ['0', 'temperature']];
  else if (a.role === 'orchestrator') metrics = [[String(all.length), 'steps planned'], [String(events.filter((e) => e.type === 'plan.revised').length), 'replans'], ['2s', 'reaper interval']];
  else {
    const done = a.steps_done ?? 0, rej = a.rejections ?? 0;
    metrics = [[String(done), 'steps done'], [`${done + rej ? Math.round((rej / (done + rej)) * 100) : 0}%`, 'rejected'], [v.hbAge != null ? `${v.hbAge}s` : '—', 'since heartbeat']];
  }
  const models = a.models ?? [];
  const tempRole = a.model_role === 'meta_reviewer' ? 'meta_reviewer' : a.model_role ?? null;
  const props: [string, string][] = [['agent_id', a.id]];
  if (a.skills?.length) props.push(['skills', a.skills.map((s) => s.id).join(', ')]);
  if (a.model_role) {
    props.push(['model', a.model ?? models[0] ?? '—'], ['fallback', models.slice(1).join(' → ') || '—']);
    if (tempRole) props.push(['temperature', `${temperature(tempRole, config.determinism).toFixed(2)} (determinism ${config.determinism.toFixed(2)})`]);
  } else if (a.role === 'worker') props.push(['model', 'none (no LLM)']);
  if (a.current_step) props.push(['lease', `${a.current_step}${a.lease_fence != null ? ` · token ${a.lease_fence}` : ''}${v.ttlS != null ? ` · ttl ${v.ttlS.toFixed(1)}s` : ''}`]);
  if (a.container) props.push(['container', a.container]);
  if (a.role === 'worker') props.push(['side_effects', String(!!a.side_effects)]);
  props.push(['reads', meta.reads], ['writes', meta.writes]);
  if (a.id === 'ledger') props.push(['lease_ttl', `${config.lease_ttl_s * 1000} ms`], ['heartbeat', `${(config.lease_ttl_s / 3).toFixed(1)} s`]);
  return (
    <>
      <p className="m-0 leading-[1.6] text-fg2 text-pretty">{meta.desc}</p>
      {v.stale && a.container && (
        <div className="flex items-center gap-3 px-3 py-2.5 rounded-lg border" style={{ borderColor: 'color-mix(in oklch, var(--s-dead) 40%, var(--line))', background: 'color-mix(in oklch, var(--s-dead) 6%, var(--panel))' }}>
          <span className="text-sm+ flex-1">Heartbeat stale for {v.hbAge ?? '?'}s. Its leases expire and another worker takes over; late writes are fenced out.</span>
          <button type="button" onClick={() => api().restartAgent(a.id).then(() => toast(`Restarting ${a.id}`)).catch((e) => toast(String(e.message ?? e), 'error'))} className="h-7 px-2.5 border border-line2 rounded-md bg-panel text-fg text-sm whitespace-nowrap">Restart container</button>
        </div>
      )}
      <div className="grid grid-cols-3 gap-2">
        {metrics.map(([val, k]) => (
          <div key={k} className="border border-line rounded-lg px-3 py-2.5 flex flex-col gap-[3px]">
            <span className="mono text-2xl">{val}</span>
            <span className="text-xs+ text-fg3">{k}</span>
          </div>
        ))}
      </div>
      <div className="flex flex-col">
        <Kicker className="pb-1.5">Properties</Kicker>
        {props.map(([k, val]) => <KV key={k} k={k} v={val} kw={150} />)}
      </div>
    </>
  );
}

function PromptTab({ a }: { a: AgentStatus }) {
  const { cfg, err, put } = useAgentConfig(a.id);
  const [draft, setDraft] = useState<string | null>(null);
  if (err) return <span className="text-sm text-fg3">Agent config unavailable: {err}</span>;
  if (!cfg) return <span className="text-sm text-fg3">Loading prompt…</span>;
  const dirty = draft != null && draft !== cfg.prompt;
  const tok = cfg.layers.filter((l) => l.enabled).reduce((n, l) => n + (l.tokens ?? 0), 0);
  return (
    <>
      <div className="flex flex-col gap-2">
        <div className="flex justify-between items-baseline">
          <Kicker>System prompt</Kicker>
          <span className="mono text-xs text-fg3">prompts/{a.id}.md · v{cfg.prompt_version}</span>
        </div>
        <textarea
          key={`${a.id}-${cfg.prompt_version}`}
          defaultValue={cfg.prompt}
          onChange={(e) => setDraft(e.target.value)}
          className="w-full min-h-[150px] bg-panel2 border border-line rounded-lg text-fg mono text-sm leading-[1.6] px-3.5 py-3 resize-y outline-none"
        />
        <div className="flex items-center gap-2.5">
          <button type="button" disabled={!dirty} onClick={async () => { await put({ prompt: draft! }); setDraft(null); }} className="h-[30px] px-3 border-0 rounded-md text-sm font-medium disabled:cursor-not-allowed" style={{ background: dirty ? 'var(--fg)' : 'var(--fg3)', color: 'var(--bg)' }}>
            Save as v{cfg.prompt_version + 1}
          </button>
          <span className="text-xs+ text-fg3">{dirty ? 'Unsaved changes' : 'Versioned in the ledger (prompt.updated). Applies from the next attempt.'}</span>
        </div>
      </div>
      <div className="flex flex-col gap-2">
        <div className="flex justify-between items-baseline">
          <Kicker>Injections · rebuilt every attempt</Kicker>
          <span className="mono text-xs text-fg2">≈ {tok} tok</span>
        </div>
        {cfg.layers.length === 0 && <span className="text-sm text-fg3">This agent does not call a model.</span>}
        {cfg.layers.map((l, i) => {
          const locked = !!l.locked || l.id === 'history';
          return (
            <div key={l.id} className="border border-line rounded-lg px-3.5 py-[11px] grid grid-cols-[22px_minmax(0,1fr)_auto_34px] gap-3 items-start" style={{ opacity: l.enabled ? 1 : 0.45 }}>
              <span className="mono text-xs text-fg3 pt-px">{i + 1}</span>
              <div className="flex flex-col gap-[3px] min-w-0">
                <span className="text-base font-medium">{l.name}</span>
                <span className="mono text-xs text-fg3">{l.source}{locked ? (l.id === 'history' ? ' · locked (D3: rejections must feed the next attempt)' : ' · locked') : ''}</span>
                {l.preview && <span className="mono text-xs+ text-fg2 leading-normal whitespace-pre-wrap">{l.preview}</span>}
              </div>
              <span className="mono text-xs text-fg3">{l.tokens ?? '—'} tok</span>
              <Toggle on={l.enabled} disabled={locked} title={locked ? 'locked' : undefined} onChange={(on) => put({ layers: { [l.id]: on } })} />
            </div>
          );
        })}
        <div className="text-sm text-fg3 leading-normal">Never injected: other agents' messages, uncommitted claims, full run history.</div>
      </div>
    </>
  );
}

function ToolsTab({ a }: { a: AgentStatus }) {
  const { cfg, put } = useAgentConfig(a.id);
  const tools: ToolSpec[] = cfg?.tools ?? a.tools ?? [];
  const add = (type: ToolSpec['type'], name: string, detail: string) => {
    const n = tools.filter((t) => t.id.startsWith(name)).length + 1;
    void put({ tools: [...tools, { id: `${name}-${n}`, type, name: `${name}-${n}`, detail, enabled: false }] });
  };
  const isWorker = a.role === 'worker';
  return (
    <>
      <div className="flex flex-col gap-2">
        <div className="flex justify-between items-center">
          <Kicker>Tools</Kicker>
          <div className="flex gap-1.5">
            <button type="button" onClick={() => add('mcp', 'new-mcp', 'stdio · configure command + args')} className="px-2.5 py-[5px] border border-line2 rounded-[5px] bg-transparent text-fg text-sm">+ MCP server</button>
            <button type="button" onClick={() => add('rest', 'http-tool', 'https://… · auth: none')} className="px-2.5 py-[5px] border border-line2 rounded-[5px] bg-transparent text-fg text-sm">+ HTTP API</button>
            <button type="button" onClick={() => add('function', 'custom_fn', 'module.path:function')} className="px-2.5 py-[5px] border border-line2 rounded-[5px] bg-transparent text-fg text-sm">+ Function</button>
          </div>
        </div>
        {tools.length === 0 && <span className="text-sm text-fg3">No tools registered.</span>}
        {tools.map((t) => {
          const crmRestForWorker = isWorker && t.type === 'rest' && /espocrm|crm/i.test(`${t.id} ${t.name}`);
          const locked = t.locked_reason ?? (crmRestForWorker ? 'Verifier-only channel. Workers act through the UI.' : null);
          const on = t.enabled !== false && !crmRestForWorker;
          return (
            <div key={t.id} className="border border-line rounded-lg px-3.5 py-[11px] grid grid-cols-[62px_minmax(0,1fr)_34px] gap-3 items-center" style={{ opacity: on ? 1 : 0.55 }}>
              <span className="justify-self-start px-1.5 py-0.5 rounded mono text-[10px] tracking-[0.04em]" style={{ color: TOOL_COLOR[t.type], background: `color-mix(in oklch, ${TOOL_COLOR[t.type]} 12%, transparent)` }}>{TOOL_LABEL[t.type] ?? t.type}</span>
              <div className="flex flex-col gap-[3px] min-w-0">
                <span className="mono text-sm+">{t.name}</span>
                <span className="mono text-xs text-fg3 [overflow-wrap:anywhere]">{t.detail}</span>
                {locked && <span className="text-xs+" style={{ color: 'var(--s-rejected)' }}>{locked}</span>}
              </div>
              <Toggle on={on} disabled={!!locked} title={locked ?? undefined} onChange={(en) => put({ tools: tools.map((x) => (x.id === t.id ? { ...x, enabled: en } : x)) })} />
            </div>
          );
        })}
      </div>
      <div className="flex flex-col">
        <Kicker className="pb-1.5">Recent tool calls</Kicker>
        {(cfg?.recent_calls ?? []).length === 0 && <span className="text-sm text-fg3">No calls recorded.</span>}
        {(cfg?.recent_calls ?? []).map((c, i) => (
          <div key={i} className="grid grid-cols-[58px_minmax(0,1fr)_56px_52px] gap-2.5 py-[7px] border-b border-line mono text-xs items-center">
            <span className="text-fg3">{fmtClock(c.ts)}</span>
            <span className="truncate">{c.call}</span>
            <span style={{ color: /^2/.test(c.code) ? 'var(--s-committed)' : 'var(--s-rejected)' }}>{c.code}</span>
            <span className="text-fg3 text-right">{c.ms != null ? (c.ms >= 1000 ? `${(c.ms / 1000).toFixed(1)}s` : `${c.ms}ms`) : '—'}</span>
          </div>
        ))}
      </div>
    </>
  );
}

function StreamsTab() {
  const runId = useLedger((s) => s.runId);
  const steps = useLedger((s) => s.steps);
  const events = useLedger((s) => s.events);
  const facts = useLedger((s) => s.facts);
  const esc = useLedger((s) => s.escalations);
  const all = Object.values(steps);
  const ready = new Map<string, number>();
  all.filter((s) => s.status === 'ready').forEach((s) => ready.set(s.skill, (ready.get(s.skill) ?? 0) + 1));
  const leased = all.filter((s) => s.status === 'leased');
  const rows: [string, string, string][] = [
    ['ledger:events', 'stream', `${events.length.toLocaleString()} (this run)`],
    [`run:${runId}`, 'hash', `${all.length ? 'running' : '—'}`],
    [`run:${runId}:steps`, 'list', String(all.length)],
    [`facts:${runId}`, 'hash', String(facts.length)],
    ...[...ready.entries()].map(([k, n]) => [`queue:${k}`, 'stream', `${n} pending`] as [string, string, string]),
    ['queue:verify', 'stream', `${all.filter((s) => s.status === 'claimed_done').length} pending`],
    ...leased.map((s) => [`lease:${s.id}`, 'string', s.lease_owner ?? '—'] as [string, string, string]),
    ...leased.map((s) => [`fence:${s.id}`, 'counter', String(s.fence ?? 0)] as [string, string, string]),
    ['escalations:open', 'set', String(esc.length)],
  ];
  return (
    <div className="flex flex-col">
      <span className="text-xs+ text-fg3 pb-2">Derived from the ledger API for the selected run.</span>
      {rows.map(([k, t, n]) => (
        <div key={k} className="grid grid-cols-[minmax(0,1fr)_70px_120px] gap-3 py-2 border-b border-line mono text-xs+">
          <span className="truncate">{k}</span><span className="text-fg3">{t}</span><span className="text-right text-fg2">{n}</span>
        </div>
      ))}
    </div>
  );
}

function ChecksTab() {
  const steps = useLedger((s) => s.steps);
  const counts = new Map<string, { runs: number; rej: number }>();
  Object.values(steps).forEach((s) => (s.history ?? []).forEach((h) => {
    if (!h.verdict) return;
    const c = counts.get(h.verdict.check || s.postcondition.check) ?? { runs: 0, rej: 0 };
    c.runs += 1;
    if (!h.verdict.ok) c.rej += 1;
    counts.set(h.verdict.check || s.postcondition.check, c);
  }));
  return (
    <div className="flex flex-col">
      <div className="grid grid-cols-[minmax(0,1fr)_130px_60px_60px] gap-3 pb-2 text-xs font-medium text-fg3 uppercase tracking-[0.04em]"><span>Check</span><span>Channel</span><span className="text-right">Runs</span><span className="text-right">Reject</span></div>
      {Object.keys(CHECK_CHANNEL).map((k) => (
        <div key={k} className="grid grid-cols-[minmax(0,1fr)_130px_60px_60px] gap-3 py-[9px] border-b border-line text-sm items-center">
          <span className="mono text-xs+">{k}</span>
          <span className="mono text-xs text-fg3">{CHECK_CHANNEL[k]}</span>
          <span className="mono text-xs text-fg2 text-right">{counts.get(k)?.runs ?? 0}</span>
          <span className="mono text-xs text-right" style={{ color: counts.get(k)?.rej ? 'var(--s-rejected)' : 'var(--fg3)' }}>{counts.get(k)?.rej ?? 0}</span>
        </div>
      ))}
    </div>
  );
}

function ContentTab() {
  const name = useLedger((s) => s.run?.playbook ?? 'event-leads.md');
  const [pb, setPb] = useState<Playbook | null>(null);
  const [err, setErr] = useState<string | null>(null);
  useEffect(() => {
    api().getPlaybook(name).then(setPb).catch((e) => setErr(String(e.message ?? e)));
  }, [name]);
  if (err) return <span className="text-sm text-fg3">The playbook lives in <span className="mono">playbooks/{name}</span>. ({err})</span>;
  if (!pb) return <span className="text-sm text-fg3">Loading playbook…</span>;
  return (
    <>
      <span className="mono text-xs text-fg3">playbooks/{pb.name}{pb.version ? ` · ${pb.version}` : ''}{pb.hash ? ` · ${pb.hash}` : ''}</span>
      {pb.sections.map((s) => (
        <div key={s.heading} className="flex flex-col gap-1.5">
          <div className="flex justify-between"><span className="mono text-xs+ text-fg2">§ {s.heading}</span>{s.used_by && <span className="mono text-xs text-fg3">→ {s.used_by}</span>}</div>
          <textarea readOnly defaultValue={s.body} className="w-full min-h-16 bg-panel2 border border-line rounded-lg text-fg text-sm+ leading-[1.55] px-3 py-2.5 resize-y outline-none" />
        </div>
      ))}
    </>
  );
}

function InboxTab() {
  const escalations = useLedger((s) => s.escalations);
  if (!escalations.length) return <span className="text-fg3">Inbox is empty. Your answers were committed to the ledger as facts.</span>;
  return (
    <>
      <div className="flex flex-col gap-[3px]">
        <span className="text-md font-medium">{escalations.length} decision{escalations.length > 1 ? 's' : ''} waiting on you</span>
        <span className="text-sm text-fg3">The meta-reviewer resolved everything else automatically; these scored below the auto threshold.</span>
      </div>
      {escalations.map((e) => <EscalationCard key={e.id} esc={e} />)}
    </>
  );
}

export function AgentSheet() {
  const open = useLedger((s) => s.openAgent);
  const agents = useLedger((s) => s.agents);
  const show = useLedger((s) => s.showAgent);
  const agentsAt = useLedger((s) => s.agentsAt);
  const ttl = useLedger((s) => s.config.lease_ttl_s);
  const esc = useLedger((s) => s.escalations.length);
  const now = useNow(1000);
  if (!open) return null;
  const a: AgentStatus = agents.find((x) => x.id === open.id) ?? PSEUDO_AGENTS.find((x) => x.id === open.id) ?? (open.id === 'human' ? { id: 'human', name: 'Human (you)', role: 'human' } : { id: open.id, name: open.id, role: 'worker' });
  const tabs = agentTabs(a);
  const tab = open.tab && tabs.includes(open.tab) ? open.tab : tabs[0];
  const pseudo = a.id === 'ledger' || a.id === 'playbook';
  const v = pseudo ? { c: 'var(--s-committed)', state: a.id === 'ledger' ? 'healthy' : 'loaded' } : agentView(a, now, agentsAt, ttl, esc);
  const roleLabel = a.id === 'ledger' ? 'source of truth' : a.id === 'playbook' ? 'context' : a.role.replace('_', '-');
  return (
    <Sheet open onClose={() => show(null)} width={640} accent={v.c}>
      <div className="px-[22px] pt-[18px] flex flex-col gap-3.5" data-testid="agent-sheet">
        <div className="flex items-start justify-between gap-3">
          <div className="flex flex-col gap-[5px]">
            <span className="mono text-xs text-fg3">{a.id} · {roleLabel}</span>
            <span className="text-2xl font-medium">{a.name}</span>
            <span className="flex items-center gap-[7px] mono text-xs+" style={{ color: v.c }}>
              <span className="w-[7px] h-[7px] rounded-[2px]" style={{ background: v.c }} />
              {v.state}{!pseudo && a.model_role ? ` · ${shortModel(a.model ?? a.models?.[0])}` : ''}{a.current_step ? ` · ${a.current_step}` : ''}
            </span>
          </div>
          <EscButton onClick={() => show(null)} />
        </div>
        <div className="flex gap-0.5 border-b border-line">
          {tabs.map((t) => (
            <button key={t} type="button" onClick={() => show(a.id, t)} className="px-3 py-[9px] border-0 -mb-px bg-transparent text-sm+ font-medium" style={{ borderBottom: `2px solid ${tab === t ? 'var(--fg)' : 'transparent'}`, color: tab === t ? 'var(--fg)' : 'var(--fg3)' }}>{t}</button>
          ))}
        </div>
      </div>
      <div className="flex-1 min-h-0 overflow-auto px-[22px] pt-5 pb-8 flex flex-col gap-5">
        {tab === 'Overview' && <Overview a={a} />}
        {tab === 'Prompt' && <PromptTab a={a} />}
        {tab === 'Tools' && <ToolsTab a={a} />}
        {tab === 'Shell' && (
          <Suspense fallback={<span className="text-fg3">Loading terminal…</span>}>
            <ShellTab agent={a.id === 'ledger' ? { ...a, id: 'redis', container: 'redis' } : a} />
          </Suspense>
        )}
        {tab === 'Streams' && <StreamsTab />}
        {tab === 'Checks' && <ChecksTab />}
        {tab === 'Content' && <ContentTab />}
        {tab === 'Inbox' && <InboxTab />}
      </div>
    </Sheet>
  );
}
