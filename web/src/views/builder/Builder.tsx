/**
 * Builder (H12 + H14): node canvas of the agents around the ledger. Pan / zoom /
 * drag nodes / wire pins. Run submits POST /runs; packets animate from the
 * run's SSE events (not a local simulator). Wiring is a per-viewer view of the
 * bus kept in localStorage; routing itself is by skill on the server.
 */
import { useCallback, useEffect, useMemo, useRef, useState, type MouseEvent as RMouseEvent } from 'react';
import { useLedger } from '../../store/store';
import type { FaultName, LedgerEvent } from '../../api/types';
import {
  buildNodes, defaultWires, eventToWire, nodeH, nodeOf, PIN_COLOR, pinPos, ROLE_STYLE, wireEnds, wirePath, type GNode, type PinType,
} from '../../lib/builderGraph';
import { agentMeta } from '../../lib/agentsMeta';
import { agentView, committedCount, counters, eventLine, fmtClock, shortModel, tagEvents } from '../../lib/derive';
import { runVisual, VISUAL_COLOR } from '../../lib/states';
import { actorColor } from '../dashboard/LiveLedger';
import { EscalationCard } from '../dashboard/NeedsAttention';
import { CRITERION_STYLE } from '../dashboard/Criteria';
import { Logo, NavTabs, ThemeButton } from '../../components/TopBar';
import { useNow } from '../../components/ui';

const LOG_OPEN = 160, LOG_CLOSED = 34, PANEL_W = 400;
const STORE_KEY = 'ledger.builder';
const FAULTS: [FaultName, string, string][] = [
  ['false_claim', 'False claim', 'Next browser step reports success without acting'],
  ['kill_worker', 'Kill worker', 'Terminate the browser operator holding a lease'],
  ['expire_session', 'Expire session', 'Invalidate the CRM login'],
  ['model_outage', 'Model outage', 'Make the primary worker model fail'],
];
const TEMPLATES: Record<string, Omit<GNode, 'id' | 'x' | 'y'>> = {
  browser: { role: 'worker', title: 'Browser operator', w: 230, ins: [{ id: 'lease', label: 'lease', t: 'task' }], outs: [{ id: 'claim', label: 'claim', t: 'claim' }, { id: 'act', label: 'act · UI', t: 'io' }] },
  drafter: { role: 'worker', title: 'Drafter', w: 230, ins: [{ id: 'lease', label: 'lease', t: 'task' }], outs: [{ id: 'claim', label: 'claim', t: 'claim' }] },
  human: { role: 'human', title: 'Human reviewer', w: 230, ins: [{ id: 'q', label: 'review.escalated', t: 'human' }], outs: [{ id: 'answer', label: 'answer', t: 'human' }] },
};
const STATIC_DESC: Record<string, string> = {
  goal: 'One plain-language request plus the attendee file. Nothing else is specified; the playbook fills in the rest.',
  crm: 'The company system being operated. Workers act on its UI; the verifier reads its REST API.',
  mailpit: 'SMTP sink. Real sends, nothing leaves the laptop.',
};

interface Saved { pos?: Record<string, { x: number; y: number }>; wires?: string[] | null; extra?: GNode[] }
const load = (): Saved => {
  try { return JSON.parse(localStorage.getItem(STORE_KEY) || '{}') as Saved; } catch { return {}; }
};
interface Packet { id: number; d: string; c: string; label: string; dur: number }
type Drag = { mode: 'pan'; sx: number; sy: number; ox: number; oy: number } | { mode: 'node'; id: string; sx: number; sy: number; ox: number; oy: number } | { mode: 'wire'; from: string; t: PinType; a: { x: number; y: number } };

export default function Builder({ active, onNeeds }: { active: boolean; onNeeds: () => void }) {
  const agents = useLedger((s) => s.agents);
  const agentsAt = useLedger((s) => s.agentsAt);
  const events = useLedger((s) => s.events);
  const runId = useLedger((s) => s.runId);
  const run = useLedger((s) => s.run);
  const steps = useLedger((s) => s.steps);
  const facts = useLedger((s) => s.facts);
  const escalations = useLedger((s) => s.escalations);
  const config = useLedger((s) => s.config);
  const createRun = useLedger((s) => s.createRun);
  const inject = useLedger((s) => s.injectFault);
  const setConfig = useLedger((s) => s.setConfig);
  const showAgent = useLedger((s) => s.showAgent);
  const setView = useLedger((s) => s.setView);
  const now = useNow(1000);

  const saved = useRef(load()).current;
  const [pos, setPos] = useState<Record<string, { x: number; y: number }>>(saved.pos ?? {});
  const [extra, setExtra] = useState<GNode[]>(saved.extra ?? []);
  const [wiresOverride, setWires] = useState<string[] | null>(saved.wires ?? null);
  const [view, setViewport] = useState({ x: 40, y: 80, k: 0.7 });
  const [sel, setSel] = useState<string | null>(null);
  const [selWire, setSelWire] = useState<string | null>(null);
  const [panel, setPanel] = useState<'inspect' | 'input' | 'evidence' | null>(null);
  const [temp, setTemp] = useState<{ a: { x: number; y: number }; z: { x: number; y: number }; c: string } | null>(null);
  const [logOpen, setLogOpen] = useState(true);
  const [goal, setGoal] = useState("Add the leads from yesterday's event to the CRM and set up follow-ups.");
  const [file, setFile] = useState('event_attendees.csv');
  const [posting, setPosting] = useState(false);
  const [packets, setPackets] = useState<Packet[]>([]);
  const [hot, setHot] = useState<Record<string, number>>({});

  const canvasRef = useRef<HTMLDivElement>(null);
  const logRef = useRef<HTMLDivElement>(null);
  const drag = useRef<Drag | null>(null);
  const pid = useRef(0);
  const fitted = useRef(false);

  const agentKey = agents.map((a) => a.id).join(',');
  // eslint-disable-next-line react-hooks/exhaustive-deps
  const baseNodes = useMemo(() => buildNodes(agents), [agentKey]);
  const nodes = useMemo(() => [...baseNodes, ...extra].map((n) => ({ ...n, ...(pos[n.id] ?? {}) })), [baseNodes, extra, pos]);
  const wires = useMemo(() => wiresOverride ?? defaultWires(baseNodes), [wiresOverride, baseNodes]);
  const connected = useMemo(() => new Set(wires.flatMap((w) => wireEnds(w))), [wires]);
  const lost = useMemo(() => new Set(agents.filter((a) => a.alive === false).map((a) => a.id)), [agents]);

  useEffect(() => {
    try { localStorage.setItem(STORE_KEY, JSON.stringify({ pos, wires: wiresOverride, extra })); } catch { /* private mode */ }
  }, [pos, wiresOverride, extra]);

  // ---- geometry ---------------------------------------------------------------
  const fit = useCallback(() => {
    const el = canvasRef.current;
    if (!el || !nodes.length) return;
    const r = el.getBoundingClientRect();
    if (!r.width || !r.height) return;
    fitted.current = true;
    const minX = Math.min(...nodes.map((n) => n.x)), maxX = Math.max(...nodes.map((n) => n.x + n.w));
    const minY = Math.min(...nodes.map((n) => n.y)), maxY = Math.max(...nodes.map((n) => n.y + nodeH(n)));
    const top = 60, W = r.width - (panel ? PANEL_W : 0) - 60, H = r.height - (logOpen ? LOG_OPEN : LOG_CLOSED) - top - 30;
    const k = Math.min(1, Math.max(0.25, Math.min(W / (maxX - minX), H / (maxY - minY))));
    setViewport({ k, x: 30 + (W - (maxX - minX) * k) / 2 - minX * k, y: top + (H - (maxY - minY) * k) / 2 - minY * k });
  }, [nodes, panel, logOpen]);

  useEffect(() => {
    if (active && !fitted.current) requestAnimationFrame(fit);
  }, [active, fit]);

  const zoomAt = useCallback((mx: number, my: number, f: number) => {
    setViewport((v) => {
      const k = Math.min(1.8, Math.max(0.25, v.k * f));
      return { k, x: mx - (mx - v.x) * (k / v.k), y: my - (my - v.y) * (k / v.k) };
    });
  }, []);

  useEffect(() => {
    const el = canvasRef.current;
    if (!el) return;
    const onWheel = (e: WheelEvent) => {
      e.preventDefault();
      const r = el.getBoundingClientRect();
      zoomAt(e.clientX - r.left, e.clientY - r.top, Math.exp(-e.deltaY * 0.0015));
    };
    el.addEventListener('wheel', onWheel, { passive: false });
    return () => el.removeEventListener('wheel', onWheel);
  }, [zoomAt]);

  const viewRef = useRef(view);
  viewRef.current = view;
  const nodesRef = useRef(nodes);
  nodesRef.current = nodes;

  useEffect(() => {
    const onMove = (e: MouseEvent) => {
      const d = drag.current;
      if (!d) return;
      if (d.mode === 'pan') setViewport((v) => ({ ...v, x: d.ox + e.clientX - d.sx, y: d.oy + e.clientY - d.sy }));
      else if (d.mode === 'node') {
        const k = viewRef.current.k;
        setPos((p) => ({ ...p, [d.id]: { x: d.ox + (e.clientX - d.sx) / k, y: d.oy + (e.clientY - d.sy) / k } }));
      } else if (d.mode === 'wire') {
        const r = canvasRef.current!.getBoundingClientRect(), v = viewRef.current;
        setTemp((t) => (t ? { ...t, z: { x: (e.clientX - r.left - v.x) / v.k, y: (e.clientY - r.top - v.y) / v.k } } : t));
      }
    };
    const onUp = () => {
      if (drag.current?.mode === 'wire') setTemp(null);
      drag.current = null;
    };
    window.addEventListener('mousemove', onMove);
    window.addEventListener('mouseup', onUp);
    return () => {
      window.removeEventListener('mousemove', onMove);
      window.removeEventListener('mouseup', onUp);
    };
  }, []);

  useEffect(() => {
    if (!active) return;
    const onKey = (e: KeyboardEvent) => {
      if (/INPUT|TEXTAREA|SELECT/.test((document.activeElement as HTMLElement | null)?.tagName ?? '')) return;
      if ((e.key === 'Delete' || e.key === 'Backspace') && selWire) {
        setWires(wires.filter((w) => w !== selWire));
        setSelWire(null);
      } else if (e.key === 'Escape') {
        setPanel(null);
        setSel(null);
        setSelWire(null);
      }
    };
    window.addEventListener('keydown', onKey);
    return () => window.removeEventListener('keydown', onKey);
  }, [active, selWire, wires]);

  // ---- packets from SSE events ---------------------------------------------------
  const seen = useRef<{ run: string | null; n: number }>({ run: null, n: 0 });
  const stepsRef = useRef(steps);
  stepsRef.current = steps;
  const wiresRef = useRef(wires);
  wiresRef.current = wires;
  useEffect(() => {
    const s = seen.current;
    if (s.run !== runId) {
      seen.current = { run: runId, n: events.length };
      return;
    }
    const fresh: LedgerEvent[] = events.length >= s.n ? events.slice(s.n) : events.slice(-1);
    seen.current = { run: runId, n: events.length };
    if (!fresh.length) return;
    const skillOf = (id: string) => stepsRef.current[id]?.skill;
    const spawned: Packet[] = [];
    const hotNext: Record<string, number> = {};
    for (const ev of fresh.slice(-16)) {
      if (active && (ev.type === 'review.escalated' || ev.type === 'input.requested')) {
        setPanel('input');
        setSel('human');
      }
      if (active && /^run\.(completed|completed_pending_input|failed)$/.test(ev.type)) setPanel('evidence');
      const hop = eventToWire(ev, skillOf);
      if (!hop || !wiresRef.current.includes(hop.wire)) continue;
      const [fa, fz] = wireEnds(hop.wire);
      const a = pinPos(fa, nodesRef.current), z = pinPos(fz, nodesRef.current);
      if (!a || !z) continue;
      const id = ++pid.current;
      spawned.push({ id, d: wirePath(a, z), c: PIN_COLOR[a.t], label: hop.label, dur: 950 });
      hotNext[hop.wire] = id;
      setTimeout(() => {
        setPackets((p) => p.filter((x) => x.id !== id));
        setHot((h) => {
          if (h[hop.wire] !== id) return h;
          const n = { ...h };
          delete n[hop.wire];
          return n;
        });
      }, 1070);
    }
    if (spawned.length) {
      setPackets((p) => [...p.slice(-40), ...spawned]);
      setHot((h) => ({ ...h, ...hotNext }));
    }
  }, [events, runId, active]);

  useEffect(() => {
    const el = logRef.current;
    if (el) el.scrollTop = el.scrollHeight;
  }, [events.length]);

  // ---- node status ---------------------------------------------------------------
  const claimed = Object.values(steps).find((s) => s.status === 'claimed_done');
  const reviewing = Object.values(steps).find((s) => s.status === 'review_required');
  const rv = runVisual(run?.status);
  const statusOf = (n: GNode): { text: string; c: string } => {
    const a = agents.find((x) => x.id === n.id);
    if (n.id === 'human') return escalations.length ? { text: `${escalations.length} escalation${escalations.length > 1 ? 's' : ''}`, c: VISUAL_COLOR.input } : { text: 'idle · escalations only', c: VISUAL_COLOR.idle };
    if (n.id === 'verifier' && claimed) return { text: `checking ${claimed.id}`, c: VISUAL_COLOR.claimed };
    if (n.id === 'meta-reviewer' && reviewing) return { text: `reviewing ${reviewing.id}`, c: VISUAL_COLOR.leased };
    if (n.id === 'meta-reviewer') return { text: `auto ≥ ${config.review_auto_threshold.toFixed(2)}`, c: VISUAL_COLOR.idle };
    if (n.id === 'orchestrator') return { text: run ? rv.label : 'waiting for a goal', c: run ? rv.c : VISUAL_COLOR.idle };
    if (n.id === 'goal') return { text: run ? 'submitted' : 'one line + file', c: run ? VISUAL_COLOR.committed : VISUAL_COLOR.idle };
    if (n.id === 'playbook') return { text: run?.playbook ?? 'event-leads.md', c: VISUAL_COLOR.idle };
    if (n.id === 'ledger') return { text: 'healthy · AOF on', c: VISUAL_COLOR.committed };
    if (n.id === 'crm') return { text: 'company system', c: VISUAL_COLOR.idle };
    if (n.id === 'mailpit') return { text: 'smtp sink', c: VISUAL_COLOR.idle };
    if (a) {
      const v = agentView(a, now, agentsAt, config.lease_ttl_s, escalations.length);
      return { text: v.stale ? `lost${a.current_step ? ` · dropped ${a.current_step}` : ''}` : a.current_step ? `${a.current_step}${a.lease_fence ? ` · token ${a.lease_fence}` : ''}` : v.state, c: v.c };
    }
    return { text: n.extra ? 'not deployed (local sketch)' : 'idle', c: VISUAL_COLOR.idle };
  };

  // ---- actions --------------------------------------------------------------------
  const startRun = async () => {
    setPosting(true);
    const r = await createRun({ goal, input_file: file || null });
    setPosting(false);
    if (r) setPanel(null);
  };
  const addAgent = (kind: keyof typeof TEMPLATES) => {
    const n = ++pid.current;
    const id = `${kind}-sketch-${n}`;
    const el = canvasRef.current;
    const r = el ? el.getBoundingClientRect() : { width: 800, height: 600 };
    setExtra((x) => [...x, { ...TEMPLATES[kind], id, extra: true, title: `${TEMPLATES[kind].title} ${x.length + 1}`, x: (r.width / 2 - view.x) / view.k - 115 + n * 4, y: (r.height / 3 - view.y) / view.k + n * 4 }]);
    setSel(id);
    setPanel('inspect');
  };
  const removeSelected = () => {
    if (!sel) return;
    setExtra((x) => x.filter((n) => n.id !== sel));
    setWires(wires.filter((w) => !wireEnds(w).some((p) => nodeOf(p) === sel)));
    setSel(null);
    setPanel(null);
  };
  const holder = agents.find((a) => a.id.includes('browser') && a.current_step && a.alive !== false)?.id;

  // ---- render values ----------------------------------------------------------------
  const c = counters(events);
  const { done, total } = committedCount(steps);
  const tags = useMemo(() => tagEvents(events), [events]);
  const logRows = events.slice(-300);
  const tagOffset = events.length - logRows.length;
  const logH = logOpen ? LOG_OPEN : LOG_CLOSED;
  const selNode = nodes.find((n) => n.id === sel);

  return (
    <div className="flex flex-col h-screen min-w-[1024px]" data-testid="builder">
      <header className="h-[54px] flex-none flex items-center gap-3 px-3.5 border-b border-line bg-bg2">
        <div className="pr-1.5"><Logo /></div>
        <NavTabs />
        <input data-testid="builder-goal" value={goal} onChange={(e) => setGoal(e.target.value)} placeholder="Add the leads from yesterday's event to the CRM and set up follow-ups." className="flex-1 min-w-[140px] h-[34px] bg-panel border border-line rounded-md text-fg text-base px-3 outline-none" />
        <label className="flex items-center gap-1.5 h-[34px] px-2.5 border border-line2 rounded-md text-xs+ text-fg2 mono" title="Input file under DATA_DIR, sent as input_file with POST /runs">
          <span>file</span>
          <select value={file} onChange={(e) => setFile(e.target.value)} className="bg-transparent text-fg outline-none mono text-xs+">
            <option value="event_attendees.csv">event_attendees.csv</option>
            <option value="">(none)</option>
          </select>
        </label>
        <button type="button" data-testid="builder-run" onClick={() => void startRun()} disabled={posting || !goal.trim()} className="h-[34px] px-4 border-0 rounded-md text-base font-medium whitespace-nowrap disabled:opacity-60" style={{ background: 'var(--fg)', color: 'var(--bg)' }}>
          {posting ? 'Starting…' : 'Run'}
        </button>
        <div className="flex gap-3.5 mono text-xs+ text-fg2 whitespace-nowrap pl-1.5 min-w-0 shrink overflow-hidden">
          <span><span className="text-fg3">committed </span><span style={{ color: 'var(--s-committed)' }}>{done}</span>/{total}</span>
          <span><span className="text-fg3">retries </span>{c.retries}</span>
          <span><span className="text-fg3">takeovers </span>{c.takeovers}</span>
          <span><span className="text-fg3">fallbacks </span>{c.fallbacks}</span>
        </div>
        {escalations.length > 0 && (
          <button type="button" onClick={() => { setPanel('input'); setSel('human'); }} className="h-[34px] px-3 border rounded-md text-sm font-medium flex items-center gap-[7px] whitespace-nowrap" style={{ borderColor: 'var(--s-input)', background: 'color-mix(in oklch, var(--s-input) 18%, transparent)', color: 'var(--s-input)' }}>
            <span className="w-[7px] h-[7px] rounded-full" style={{ background: 'var(--s-input)' }} />Input required
          </button>
        )}
        <ThemeButton />
      </header>

      <div className="flex-1 min-h-0 relative overflow-hidden">
        <div
          ref={canvasRef}
          onMouseDown={(e) => {
            if (e.button !== 0) return;
            drag.current = { mode: 'pan', sx: e.clientX, sy: e.clientY, ox: view.x, oy: view.y };
            setSelWire(null);
          }}
          className="absolute inset-0 overflow-hidden"
          style={{ backgroundColor: 'var(--bg)', backgroundImage: 'radial-gradient(var(--dot) 1.2px, transparent 1.2px)', backgroundSize: `${Math.max(8, 24 * view.k)}px ${Math.max(8, 24 * view.k)}px`, backgroundPosition: `${view.x}px ${view.y}px` }}
        >
          <div className="absolute left-0 top-0" style={{ transformOrigin: '0 0', transform: `translate(${view.x}px, ${view.y}px) scale(${view.k})` }}>
            <svg width={1} height={1} className="absolute left-0 top-0 overflow-visible pointer-events-none">
              {wires.map((w) => {
                const [fa, fz] = wireEnds(w);
                const a = pinPos(fa, nodes), z = pinPos(fz, nodes);
                if (!a || !z) return null;
                const dead = lost.has(nodeOf(fa)) || lost.has(nodeOf(fz));
                const isHot = !!hot[w], isSel = selWire === w;
                const d = wirePath(a, z);
                return (
                  <g key={w}>
                    <path d={d} fill="none" strokeLinecap="round" style={{ stroke: isSel ? 'var(--fg)' : dead ? 'var(--s-dead)' : PIN_COLOR[a.t], strokeWidth: isHot || isSel ? 3 : 1.6, strokeOpacity: isHot || isSel ? 1 : dead ? 0.5 : 0.38, strokeDasharray: dead ? '5 5' : 'none' }} />
                    <path d={d} fill="none" stroke="transparent" strokeWidth={14} style={{ pointerEvents: 'stroke', cursor: 'pointer' }} onMouseDown={(e) => { e.stopPropagation(); setSelWire(w); }} />
                  </g>
                );
              })}
              {temp && <path d={wirePath(temp.a, temp.z)} fill="none" style={{ stroke: temp.c, strokeWidth: 2, strokeDasharray: '6 5' }} />}
            </svg>

            {nodes.map((n) => {
              const role = ROLE_STYLE[n.role];
              const st = statusOf(n);
              const isSel = sel === n.id;
              const asking = n.role === 'human' && escalations.length > 0;
              const rows = Array.from({ length: Math.max(n.ins.length, n.outs.length, 1) }, (_, i) => [n.ins[i], n.outs[i]] as const);
              return (
                <div
                  key={n.id}
                  data-testid={`node-${n.id}`}
                  className="absolute bg-panel rounded-lg"
                  style={{ left: n.x, top: n.y, width: n.w, border: `1px solid ${isSel ? 'var(--fg)' : asking ? 'var(--s-input)' : n.role === 'ledger' ? 'var(--fg3)' : 'var(--line2)'}`, opacity: lost.has(n.id) ? 0.45 : 1, boxShadow: '0 10px 28px rgba(0,0,0,0.12)', borderStyle: n.extra ? 'dashed' : 'solid' }}
                >
                  <div
                    onMouseDown={(e: RMouseEvent) => {
                      e.stopPropagation();
                      drag.current = { mode: 'node', id: n.id, sx: e.clientX, sy: e.clientY, ox: n.x, oy: n.y };
                      setSel(n.id);
                      setSelWire(null);
                      setPanel('inspect');
                    }}
                    className="h-9 rounded-t-[7px] px-3 flex items-center justify-between gap-2 bg-panel2 cursor-grab select-none"
                    style={{ borderTop: `3px solid ${role.c}` }}
                  >
                    <span className="text-base font-medium whitespace-nowrap">{n.title}</span>
                    <span className="mono text-[10px] text-fg3 whitespace-nowrap">{n.role === 'meta' ? `auto ≥ ${config.review_auto_threshold.toFixed(2)}` : role.label}</span>
                  </div>
                  <div className="py-1.5">
                    {rows.map(([l, r], i) => (
                      <div key={i} className="relative h-6 flex items-center justify-between gap-2.5 mono text-xs text-fg2 select-none">
                        <span className="pl-3.5 whitespace-nowrap">{l?.label ?? ''}</span>
                        <span className="pr-3.5 whitespace-nowrap">{r?.label ?? ''}</span>
                        {l && (
                          <span
                            title="input pin"
                            onMouseUp={() => {
                              const d = drag.current;
                              if (!d || d.mode !== 'wire' || d.t !== l.t || nodeOf(d.from) === n.id) return;
                              const id = `${d.from}>${n.id}.${l.id}`;
                              if (!wires.includes(id)) setWires([...wires, id]);
                            }}
                            className="absolute -left-2 top-1 w-4 h-4 rounded-full cursor-crosshair"
                            style={{ border: `2px solid ${PIN_COLOR[l.t]}`, background: connected.has(`${n.id}.${l.id}`) ? PIN_COLOR[l.t] : 'var(--panel)' }}
                          />
                        )}
                        {r && (
                          <span
                            title="drag to wire"
                            onMouseDown={(e) => {
                              e.stopPropagation();
                              e.preventDefault();
                              const a = pinPos(`${n.id}.${r.id}`, nodes);
                              if (!a) return;
                              drag.current = { mode: 'wire', from: `${n.id}.${r.id}`, t: r.t, a };
                              setTemp({ a, z: a, c: PIN_COLOR[r.t] });
                            }}
                            className="absolute -right-2 top-1 w-4 h-4 rounded-full cursor-crosshair"
                            style={{ border: `2px solid ${PIN_COLOR[r.t]}`, background: connected.has(`${n.id}.${r.id}`) ? PIN_COLOR[r.t] : 'var(--panel)' }}
                          />
                        )}
                      </div>
                    ))}
                  </div>
                  <div className="border-t border-line px-3 py-2 flex items-center gap-2 mono text-xs min-h-[30px]" style={{ color: st.c }}>
                    <span className="flex-none w-[7px] h-[7px] rounded-[2px]" style={{ background: st.c }} />
                    <span className="truncate">{st.text}</span>
                  </div>
                  {n.id === 'ledger' && (
                    <div className="px-3 pb-2.5 flex flex-col gap-[3px] text-xs+ text-fg2 leading-[1.4]">
                      <span>{events.length.toLocaleString()} events · {facts.length} facts committed</span>
                      <span>leases · fencing tokens · consumer groups</span>
                    </div>
                  )}
                </div>
              );
            })}

            {packets.map((p) => (
              <div key={p.id} className="absolute left-0 top-0 z-[5] pointer-events-none flex items-center gap-1.5" style={{ offsetPath: `path('${p.d}')`, offsetRotate: '0deg', offsetAnchor: '7px 50%', animation: `ldg-travel ${p.dur}ms cubic-bezier(.45,0,.55,1) forwards` } as React.CSSProperties}>
                <span className="w-3.5 h-3.5 rounded-full" style={{ background: p.c, boxShadow: `0 0 0 5px color-mix(in oklch, ${p.c} 25%, transparent)` }} />
                {p.label && <span className="mono text-xs px-[7px] py-0.5 rounded whitespace-nowrap bg-bg" style={{ border: `1px solid ${p.c}`, color: p.c }}>{p.label}</span>}
              </div>
            ))}
          </div>
        </div>

        {/* toolbar */}
        <div className="absolute top-3 left-3 flex items-center gap-1.5 p-1.5 bg-panel border border-line rounded-lg" style={{ boxShadow: '0 8px 24px rgba(0,0,0,0.1)' }}>
          <span className="mono text-2xs text-fg3 px-1.5">ADD</span>
          {([['browser', 'Browser operator'], ['drafter', 'Drafter'], ['human', 'Human reviewer']] as const).map(([k, label]) => (
            <button key={k} type="button" onClick={() => addAgent(k)} className="h-7 px-2.5 border border-line2 rounded-[5px] bg-transparent text-fg text-sm whitespace-nowrap hover:bg-panel2">+ {label}</button>
          ))}
          <span className="w-px h-5 bg-line2 mx-1" />
          <span className="mono text-2xs px-1.5" style={{ color: 'var(--s-rejected)' }}>INJECT</span>
          {FAULTS.map(([f, label, desc]) => (
            <button key={f} type="button" title={desc} disabled={!runId} onClick={() => inject(f, f === 'kill_worker' && holder ? { agent_id: holder } : undefined)} className="h-7 px-2.5 border rounded-[5px] bg-transparent text-sm whitespace-nowrap disabled:opacity-50" style={{ borderColor: 'color-mix(in oklch, var(--s-rejected) 40%, var(--line2))', color: 'var(--s-rejected)' }}>{label}</button>
          ))}
          <span className="w-px h-5 bg-line2 mx-1" />
          <span className="mono text-2xs text-fg3 px-1">DETERMINISM</span>
          <input type="range" min={0} max={1} step={0.05} value={config.determinism} onChange={(e) => setConfig({ determinism: Number(e.target.value) })} title={`workers T=${(1 - config.determinism).toFixed(2)} · orchestrator T=${(0.8 * (1 - config.determinism)).toFixed(2)} · verifier T=0`} className="w-[90px]" disabled={!runId} />
          <span className="mono text-xs text-fg min-w-[30px]">{config.determinism.toFixed(2)}</span>
        </div>

        {/* zoom */}
        <div className="absolute flex items-center gap-0.5 p-1 bg-panel border border-line rounded-lg mono text-xs" style={{ right: (panel ? PANEL_W : 0) + 14, bottom: logH + 12 }}>
          <button type="button" onClick={() => { const r = canvasRef.current!.getBoundingClientRect(); zoomAt(r.width / 2, r.height / 2, 1 / 1.2); }} className="w-7 h-[26px] border-0 rounded bg-transparent text-fg">−</button>
          <span className="min-w-11 text-center text-fg2">{Math.round(view.k * 100)}%</span>
          <button type="button" onClick={() => { const r = canvasRef.current!.getBoundingClientRect(); zoomAt(r.width / 2, r.height / 2, 1.2); }} className="w-7 h-[26px] border-0 rounded bg-transparent text-fg">+</button>
          <button type="button" onClick={fit} className="h-[26px] px-2 border-0 rounded bg-transparent text-fg2">fit</button>
          <button type="button" onClick={() => { setWires(null); setPos({}); setExtra([]); requestAnimationFrame(fit); }} className="h-[26px] px-2 border-0 rounded bg-transparent text-fg2" title="Reset wiring and positions">reset</button>
        </div>
        <div className="absolute left-3.5 mono text-2xs text-fg3 pointer-events-none" style={{ bottom: logH + 12 }}>drag canvas to pan · scroll to zoom · drag from a pin to wire · click wire + Del to remove</div>

        {/* ledger stream */}
        <div className="absolute left-0 bottom-0 bg-bg2 border-t border-line flex flex-col" style={{ right: panel ? PANEL_W : 0, height: logH }}>
          <div className="h-[34px] flex-none flex items-center gap-3 px-3.5 border-b border-line">
            <span className="mono text-xs text-fg2 tracking-[0.04em]">ledger:events</span>
            <span className="mono text-xs text-fg3 whitespace-nowrap">{events.length} events · append-only{runId ? ` · ${runId}` : ''}</span>
            <button type="button" onClick={() => setLogOpen(!logOpen)} className="ml-auto h-[22px] px-2 border border-line2 rounded bg-transparent text-fg2 mono text-2xs">{logOpen ? 'collapse' : 'expand'}</button>
          </div>
          <div ref={logRef} className="flex-1 min-h-0 overflow-auto py-1">
            {events.length === 0 && <div className="p-3.5 text-fg3 text-sm">Press Run to watch the agents coordinate through the ledger.</div>}
            {logRows.map((e, i) => {
              const tag = tags[tagOffset + i];
              return (
                <div key={e.id ?? i} className="grid gap-3 items-center px-3.5 py-1 mono text-xs" style={{ gridTemplateColumns: '64px 120px 170px minmax(0,1fr) 72px', background: tag === 'FAULT' ? 'color-mix(in oklch, var(--s-rejected) 9%, transparent)' : 'transparent' }}>
                  <span className="text-fg3">{fmtClock(e.ts)}</span>
                  <span className="truncate" style={{ color: actorColor(e.actor) }}>{e.actor}</span>
                  <span className="text-fg whitespace-nowrap">{e.type}</span>
                  <span className="text-fg2 font-sans text-sm truncate">{eventLine(e)}</span>
                  <span className="text-[9.5px] tracking-[0.06em] text-right" style={{ color: tag === 'FAULT' ? 'var(--s-rejected)' : 'var(--s-committed)' }}>{tag}</span>
                </div>
              );
            })}
          </div>
        </div>

        {/* right panel */}
        {panel && (
          <aside onMouseDown={(e) => e.stopPropagation()} className="absolute top-0 right-0 bottom-0 bg-panel border-l border-line flex flex-col" style={{ width: PANEL_W, boxShadow: '-12px 0 32px rgba(0,0,0,0.1)' }}>
            {panel === 'inspect' && selNode && (() => {
              const a = agents.find((x) => x.id === selNode.id);
              const meta = a ? agentMeta(a) : null;
              const st = statusOf(selNode);
              const role = ROLE_STYLE[selNode.role];
              const unwired = selNode.ins.length > 0 && !selNode.ins.some((p) => connected.has(`${selNode.id}.${p.id}`));
              const list: { icon: string; c: string; text: string }[] = selNode.id === 'orchestrator'
                ? (run?.criteria ?? []).map((cr) => ({ icon: CRITERION_STYLE[cr.status].icon, c: CRITERION_STYLE[cr.status].c, text: cr.text }))
                : selNode.id === 'ledger' ? (facts.length ? facts.slice(-12).map((f) => ({ icon: '■', c: 'var(--s-committed)', text: `${f.key} = ${typeof f.value === 'string' ? f.value : JSON.stringify(f.value)}` })) : [{ icon: '·', c: 'var(--fg3)', text: 'No facts yet. Facts appear only after the verifier commits.' }])
                : selNode.id === 'goal' ? [{ icon: '·', c: 'var(--fg2)', text: run?.goal ?? goal }, { icon: '·', c: 'var(--fg2)', text: run?.input_file ?? file ?? '—' }]
                : [];
              return (
                <div className="flex flex-col min-h-0 h-full">
                  <div className="px-5 pt-[18px] pb-3.5 border-b border-line flex justify-between items-start gap-3" style={{ borderTop: `3px solid ${role.c}` }}>
                    <div className="flex flex-col gap-1">
                      <span className="text-xl font-medium">{selNode.title}</span>
                      <span className="mono text-xs text-fg3">{selNode.id} · {role.label}</span>
                    </div>
                    <button type="button" onClick={() => { setPanel(null); setSel(null); }} className="border border-line2 bg-transparent text-fg2 rounded px-2 py-[3px]">×</button>
                  </div>
                  <div className="flex-1 overflow-auto px-5 py-[18px] flex flex-col gap-[18px]">
                    <p className="m-0 leading-[1.55] text-fg text-pretty">{meta?.desc ?? STATIC_DESC[selNode.id] ?? agentMeta({ id: selNode.id, role: 'worker' }).desc}</p>
                    <div className="flex items-center gap-2 px-3 py-2.5 rounded-md bg-bg mono text-xs+" style={{ color: st.c }}><span className="w-[7px] h-[7px] rounded-[2px]" style={{ background: st.c }} />{st.text}</div>
                    {a && (
                      <div className="grid grid-cols-[90px_minmax(0,1fr)] gap-x-3 gap-y-2 mono text-xs+">
                        <span className="text-fg3">skills</span><span>{(a.skills ?? []).map((s) => s.id).join(' · ') || '—'}</span>
                        <span className="text-fg3">model</span><span>{a.model_role ? shortModel(a.model ?? a.models?.[0]) : '— (no LLM)'}</span>
                        <span className="text-fg3">fallback</span><span>{(a.models ?? []).slice(1).map(shortModel).join(' → ') || '—'}</span>
                        <span className="text-fg3">reads</span><span className="text-fg2">{meta?.reads}</span>
                        <span className="text-fg3">writes</span><span className="text-fg2">{meta?.writes}</span>
                      </div>
                    )}
                    {unwired && <div className="px-3 py-2.5 border border-dashed border-line2 rounded-md text-fg2 text-sm leading-normal">Not wired yet. Drag from a <span className="mono">queue:</span> pin on the Ledger into this node's <span className="mono">lease</span> pin, then its <span className="mono">claim</span> into the Verifier.</div>}
                    {selNode.extra && <div className="text-sm text-fg3">A local sketch. Adding real operators is a compose change (<span className="mono">browser_concurrency</span> + scale).</div>}
                    {list.length > 0 && (
                      <div className="flex flex-col gap-2">
                        <span className="mono text-xs text-fg3 uppercase tracking-[0.06em]">{selNode.id === 'orchestrator' ? 'Success criteria' : selNode.id === 'ledger' ? 'Committed facts' : 'Request'}</span>
                        {list.map((it, i) => (
                          <div key={i} className="grid grid-cols-[16px_minmax(0,1fr)] gap-2 text-sm+ leading-[1.45] py-1.5 border-b border-line"><span className="mono" style={{ color: it.c }}>{it.icon}</span><span className="text-pretty [overflow-wrap:anywhere]">{it.text}</span></div>
                        ))}
                      </div>
                    )}
                    <div className="flex gap-2">
                      {(a || ['ledger', 'playbook', 'human'].includes(selNode.id)) && <button type="button" onClick={() => showAgent(selNode.id)} className="px-3 py-[7px] border border-line2 rounded-md bg-transparent text-fg text-sm">Open agent sheet</button>}
                      {selNode.extra && <button type="button" onClick={removeSelected} className="px-3 py-[7px] border border-line2 rounded-md bg-transparent text-sm" style={{ color: 'var(--s-rejected)' }}>Remove agent</button>}
                    </div>
                  </div>
                </div>
              );
            })()}
            {panel === 'input' && (
              <div className="flex flex-col min-h-0 h-full">
                <div className="px-5 pt-[18px] pb-3.5 border-b border-line flex justify-between items-start gap-3" style={{ borderTop: '3px solid var(--s-input)' }}>
                  <div className="flex flex-col gap-1">
                    <span className="mono text-xs" style={{ color: 'var(--s-input)' }}>input required · queue:human</span>
                    <span className="text-xl font-medium">{escalations[0]?.question ?? 'Inbox'}</span>
                  </div>
                  <button type="button" onClick={() => setPanel(null)} className="border border-line2 bg-transparent text-fg2 rounded px-2 py-[3px]">×</button>
                </div>
                <div className="flex-1 overflow-auto px-5 py-4 flex flex-col gap-3.5">
                  {escalations.length === 0 ? <div className="text-fg2">Nothing is waiting on you. When a step needs a decision it lands here.</div> : escalations.map((e) => <EscalationCard key={e.id} esc={e} />)}
                  <span className="text-sm text-fg2 leading-[1.45]">Your answer is committed as a fact and the lane goes back to <span className="mono">ready</span>.</span>
                </div>
              </div>
            )}
            {panel === 'evidence' && (() => {
              const all = Object.values(steps);
              const created = all.filter((s) => s.kind === 'crm.create_contact' && s.status === 'committed').length;
              const updated = all.filter((s) => s.kind === 'crm.update_contact' && s.status === 'committed').length;
              const skipped = new Set(all.filter((s) => s.status === 'replanned' && s.lane).map((s) => s.lane)).size;
              const sent = all.filter((s) => s.kind === 'email.send' && s.status === 'committed').length;
              const faults = events.filter((e) => e.type === 'fault.injected');
              return (
                <div className="flex flex-col min-h-0 h-full">
                  <div className="px-5 pt-[18px] pb-3.5 border-b border-line flex justify-between items-start gap-3" style={{ borderTop: '3px solid var(--s-committed)' }}>
                    <div className="flex flex-col gap-1">
                      <span className="mono text-xs" style={{ color: rv.c }}>{rv.label} · evidence</span>
                      <span className="text-xl font-medium">Every outcome checked against the CRM</span>
                    </div>
                    <button type="button" onClick={() => setPanel(null)} className="border border-line2 bg-transparent text-fg2 rounded px-2 py-[3px]">×</button>
                  </div>
                  <div className="flex-1 overflow-auto px-5 py-[18px] flex flex-col gap-5">
                    <div className="grid grid-cols-4 gap-2">
                      {([[created, 'created', 'var(--s-committed)'], [updated, 'updated', 'var(--s-leased)'], [skipped, 'skipped', 'var(--fg2)'], [sent, 'emails sent', 'var(--s-input)']] as const).map(([n, label, col]) => (
                        <div key={label} className="p-2.5 border border-line rounded-md flex flex-col gap-1"><span className="mono text-[20px]" style={{ color: col }}>{n}</span><span className="text-xs text-fg2">{label}</span></div>
                      ))}
                    </div>
                    <div className="flex flex-col gap-2">
                      <span className="mono text-xs text-fg3 uppercase tracking-[0.06em]">What broke and how it recovered</span>
                      {faults.length === 0 && <span className="text-fg2 text-sm">No faults injected this run. Use INJECT in the toolbar during a run.</span>}
                      {faults.map((f) => {
                        const rec = events.find((e) => e.ts >= f.ts && ['step.rejected', 'step.lease_expired', 'model.fallback', 'plan.revised'].includes(e.type));
                        return (
                          <div key={f.id ?? f.ts} className="flex flex-col gap-1 py-2.5 border-b border-line">
                            <span className="mono text-xs+" style={{ color: 'var(--s-rejected)' }}>{String(f.payload.fault ?? 'fault')}</span>
                            <span className="text-sm+ leading-[1.45] text-pretty">{rec ? `${rec.type}: ${eventLine(rec)}` : 'recovering…'}</span>
                          </div>
                        );
                      })}
                    </div>
                    <div className="flex flex-col gap-2">
                      <span className="mono text-xs text-fg3 uppercase tracking-[0.06em]">Committed facts</span>
                      {facts.slice(-12).map((f) => <span key={f.key} className="mono text-xs+ text-fg truncate">{f.key} = {typeof f.value === 'string' ? f.value : JSON.stringify(f.value)}</span>)}
                    </div>
                    <div className="flex gap-2">
                      <button type="button" onClick={() => setView('report')} className="px-3 py-[7px] border border-line2 rounded-md bg-transparent text-fg text-sm">Open evidence report</button>
                      <button type="button" onClick={onNeeds} className="px-3 py-[7px] border border-line2 rounded-md bg-transparent text-fg text-sm">Dashboard</button>
                    </div>
                  </div>
                </div>
              );
            })()}
          </aside>
        )}
      </div>
    </div>
  );
}
