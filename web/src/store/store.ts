/**
 * The single client store (zustand). One SSE connection per selected run
 * feeds `applyEvent`; REST endpoints fill the rest and are refetched when an
 * event says the server-side list changed.
 */
import { create } from 'zustand';
import { api } from '../api';
import type { StreamStatus } from '../api/client';
import type {
  AgentStatus, Escalation, FaultName, LedgerEvent, LlmBudget, NewRun, Report, Run, RunConfig,
} from '../api/types';
import { DEFAULT_RUN_CONFIG, DETERMINISM_KEYS } from '../lib/runConfig';
import { emptySlice, reduceEvent, type Refetch, type RunSlice } from './reduce';

export type View = 'dashboard' | 'report' | 'builder';
export type Theme = 'light' | 'dark';
export const MAX_EVENTS = 5000;

export interface Toast { id: number; text: string; tone: 'info' | 'error' }

export interface LedgerState extends RunSlice {
  // data
  runs: Run[];
  runId: string | null;
  events: LedgerEvent[];
  seenIds: Set<string>;
  agents: AgentStatus[];
  agentsAt: number; // when /agents was fetched (TTL ring extrapolation)
  escalations: Escalation[];
  config: RunConfig;
  configHash: string | null;
  budget: LlmBudget | null;
  report: Report | null;
  reportError: string | null;
  stream: StreamStatus;
  faultLast: Partial<Record<FaultName, { at: number; note: string }>>;
  loading: boolean;
  // ui
  view: View;
  theme: Theme;
  openStepId: string | null;
  openAgent: { id: string; tab?: string } | null;
  toasts: Toast[];
}

export interface LedgerActions {
  init: () => Promise<void>;
  selectRun: (runId: string) => Promise<void>;
  applyEvent: (ev: LedgerEvent) => void;
  refetch: (what: Refetch) => void;
  loadReport: () => Promise<void>;
  answerEscalation: (escId: string, answer: string, saveAsRule: boolean) => Promise<void>;
  injectFault: (fault: FaultName, body?: Record<string, unknown>) => Promise<void>;
  setConfig: (patch: Partial<RunConfig>) => void;
  resetConfig: () => void;
  createRun: (body: NewRun) => Promise<Run | null>;
  replay: () => Promise<void>;
  setView: (v: View) => void;
  toggleTheme: () => void;
  openStep: (id: string | null) => void;
  showAgent: (id: string | null, tab?: string) => void;
  toast: (text: string, tone?: Toast['tone']) => void;
  dismissToast: (id: number) => void;
}

export type Store = LedgerState & LedgerActions;

const savedTheme = (): Theme => {
  try {
    return localStorage.getItem('ledger.theme') === 'dark' ? 'dark' : 'light';
  } catch {
    return 'light';
  }
};

let closeStream: (() => void) | null = null;
let pending: Refetch = {};
let refetchTimer: ReturnType<typeof setTimeout> | null = null;
let configTimer: ReturnType<typeof setTimeout> | null = null;
let pendingConfig: Partial<RunConfig> = {};
let toastSeq = 0;

const errText = (e: unknown) => (e instanceof Error ? e.message : String(e));

export const useLedger = create<Store>()((set, get) => ({
  ...emptySlice(),
  runs: [],
  runId: null,
  events: [],
  seenIds: new Set(),
  agents: [],
  agentsAt: 0,
  escalations: [],
  config: { ...DEFAULT_RUN_CONFIG },
  configHash: null,
  budget: null,
  report: null,
  reportError: null,
  stream: 'closed',
  faultLast: {},
  loading: false,
  view: 'dashboard',
  theme: savedTheme(),
  openStepId: null,
  openAgent: null,
  toasts: [],

  init: async () => {
    const c = api();
    const [runs, agents, budget] = await Promise.all([
      c.listRuns().catch(() => [] as Run[]),
      c.listAgents().catch(() => [] as AgentStatus[]),
      c.getBudget().catch(() => null),
    ]);
    const sorted = runs.slice().sort((a, b) => (b.created_at ?? 0) - (a.created_at ?? 0));
    set({ runs: sorted, agents, agentsAt: Date.now(), budget });
    const fromUrl = new URLSearchParams(location.search).get('run');
    const pick = (fromUrl && sorted.find((r) => r.id === fromUrl)?.id) || sorted[0]?.id;
    if (pick) await get().selectRun(pick);
    else get().refetch({ escalations: true });
  },

  selectRun: async (runId) => {
    closeStream?.();
    closeStream = null;
    set({ ...emptySlice(), runId, events: [], seenIds: new Set(), report: null, reportError: null, loading: true, openStepId: null });
    const c = api();
    try {
      const [run, steps, facts, events, cfg, escalations] = await Promise.all([
        c.getRun(runId),
        c.getSteps(runId).catch(() => []),
        c.getFacts(runId).catch(() => []),
        c.getEvents(runId, { limit: 2000 }).catch(() => [] as LedgerEvent[]),
        c.getRunConfig(runId).catch(() => null),
        c.listEscalations(runId).catch(() => [] as Escalation[]),
      ]);
      if (get().runId !== runId) return;
      const stepMap = Object.fromEntries(steps.map((s) => [s.id, s]));
      const tail = events.slice(-MAX_EVENTS);
      set({
        run,
        steps: stepMap,
        stepOrder: steps.map((s) => s.id),
        facts,
        events: tail,
        seenIds: new Set(tail.map((e) => e.id).filter((x): x is string => !!x)),
        config: cfg?.config ?? { ...DEFAULT_RUN_CONFIG },
        configHash: cfg?.config_hash ?? run.config_hash ?? null,
        escalations: escalations.filter((e) => (e.status ?? 'open') === 'open'),
        loading: false,
      });
      try {
        const u = new URL(location.href);
        u.searchParams.set('run', runId);
        history.replaceState(null, '', u);
      } catch { /* non-browser */ }
      const lastId = tail.length ? tail[tail.length - 1].id ?? null : null;
      closeStream = c.stream(runId, lastId, {
        onEvent: (ev) => get().applyEvent(ev),
        onStatus: (s) => set({ stream: s }),
      });
    } catch (e) {
      set({ loading: false });
      get().toast(`Could not load run ${runId}: ${errText(e)}`, 'error');
    }
  },

  applyEvent: (ev) => {
    const s = get();
    if (ev.run_id && s.runId && ev.run_id !== s.runId) return;
    if (ev.id && s.seenIds.has(ev.id)) return;
    const { slice, refetch } = reduceEvent({ run: s.run, steps: s.steps, stepOrder: s.stepOrder, facts: s.facts }, ev);
    const events = s.events.length >= MAX_EVENTS ? [...s.events.slice(-(MAX_EVENTS - 1)), ev] : [...s.events, ev];
    const seenIds = s.seenIds;
    if (ev.id) seenIds.add(ev.id);
    let faultLast = s.faultLast;
    if (ev.type === 'fault.injected') {
      const f = String(ev.payload.fault ?? ev.payload.name ?? '') as FaultName;
      if (f) faultLast = { ...faultLast, [f]: { at: ev.ts, note: 'fired · recovering…' } };
    }
    set({ ...slice, events, seenIds, faultLast });
    if (/^run\.(completed|failed|completed_pending_input)$/.test(ev.type)) {
      refetch.run = true;
      if (s.view === 'report') void get().loadReport();
    }
    get().refetch(refetch);
  },

  refetch: (what) => {
    pending = {
      steps: pending.steps || what.steps,
      escalations: pending.escalations || what.escalations,
      agents: pending.agents || what.agents,
      run: pending.run || what.run,
      config: pending.config || what.config,
      budget: pending.budget || what.budget,
    };
    if (!Object.values(pending).some(Boolean) || refetchTimer) return;
    refetchTimer = setTimeout(async () => {
      refetchTimer = null;
      const w = pending;
      pending = {};
      const c = api();
      const runId = get().runId;
      const jobs: Promise<unknown>[] = [];
      if (w.steps && runId) {
        jobs.push(c.getSteps(runId).then((steps) => {
          if (get().runId !== runId) return;
          set({ steps: Object.fromEntries(steps.map((x) => [x.id, x])), stepOrder: steps.map((x) => x.id) });
        }));
      }
      if (w.escalations) {
        jobs.push(c.listEscalations(runId ?? undefined).then((es) => set({ escalations: es.filter((e) => (e.status ?? 'open') === 'open') })));
      }
      if (w.agents) jobs.push(c.listAgents().then((agents) => set({ agents, agentsAt: Date.now() })));
      if (w.run && runId) {
        jobs.push(c.getRun(runId).then((run) => {
          if (get().runId !== runId) return;
          set((st) => ({ run, runs: st.runs.map((r) => (r.id === run.id ? { ...r, ...run } : r)) }));
        }));
        jobs.push(c.listRuns().then((runs) => set({ runs: runs.slice().sort((a, b) => (b.created_at ?? 0) - (a.created_at ?? 0)) })));
      }
      if (w.config && runId && !configTimer) {
        jobs.push(c.getRunConfig(runId).then((r) => set({ config: r.config, configHash: r.config_hash ?? get().configHash })));
      }
      if (w.budget) jobs.push(c.getBudget().then((budget) => set({ budget })));
      await Promise.allSettled(jobs);
    }, 250);
  },

  loadReport: async () => {
    const runId = get().runId;
    if (!runId) return;
    try {
      const report = await api().getReport(runId);
      if (get().runId === runId) set({ report, reportError: null });
    } catch (e) {
      set({ reportError: errText(e) });
    }
  },

  answerEscalation: async (escId, answer, saveAsRule) => {
    try {
      await api().answerEscalation(escId, answer, saveAsRule);
      set((s) => ({ escalations: s.escalations.filter((e) => e.id !== escId) }));
      get().toast(`Decision committed${saveAsRule ? ' · saved as playbook rule' : ''}`);
      get().refetch({ escalations: true, steps: true });
    } catch (e) {
      get().toast(`Could not commit the decision: ${errText(e)}`, 'error');
    }
  },

  injectFault: async (fault, body) => {
    set((s) => ({ faultLast: { ...s.faultLast, [fault]: { at: Date.now(), note: 'fired · recovering…' } } }));
    try {
      await api().injectFault(fault, body);
    } catch (e) {
      set((s) => ({ faultLast: { ...s.faultLast, [fault]: { at: Date.now(), note: 'failed' } } }));
      get().toast(`Fault ${fault} failed: ${errText(e)}`, 'error');
    }
  },

  setConfig: (patch) => {
    const runId = get().runId;
    set((s) => ({ config: { ...s.config, ...patch } }));
    pendingConfig = { ...pendingConfig, ...patch };
    if (configTimer) clearTimeout(configTimer);
    configTimer = setTimeout(async () => {
      configTimer = null;
      const p = pendingConfig;
      pendingConfig = {};
      if (!runId) return;
      const c = api();
      try {
        const det = DETERMINISM_KEYS.some((k) => k in p);
        const rest = Object.fromEntries(Object.entries(p).filter(([k]) => !(DETERMINISM_KEYS as string[]).includes(k)));
        if (det) {
          const cfg = get().config;
          await c.setDeterminism(runId, cfg.determinism, cfg.seed_pinned ? cfg.seed : null);
        }
        if (Object.keys(rest).length || (det && 'seed_pinned' in p)) {
          const r = await c.putRunConfig(runId, 'seed_pinned' in p ? { ...rest, seed_pinned: p.seed_pinned } : rest);
          set({ configHash: r.config_hash ?? get().configHash });
        }
      } catch (e) {
        get().toast(`Run config not saved: ${errText(e)}`, 'error');
        get().refetch({ config: true });
      }
    }, 450);
  },

  resetConfig: () => get().setConfig({ ...DEFAULT_RUN_CONFIG }),

  createRun: async (body) => {
    try {
      const run = await api().createRun(body);
      set((s) => ({ runs: [run, ...s.runs.filter((r) => r.id !== run.id)] }));
      await get().selectRun(run.id);
      return run;
    } catch (e) {
      get().toast(`Could not start the run: ${errText(e)}`, 'error');
      return null;
    }
  },

  replay: async () => {
    const runId = get().runId;
    if (!runId) return;
    try {
      const run = await api().replayRun(runId);
      set((s) => ({ runs: [run, ...s.runs.filter((r) => r.id !== run.id)] }));
      await get().selectRun(run.id);
    } catch (e) {
      get().toast(`Replay failed: ${errText(e)}`, 'error');
    }
  },

  setView: (view) => {
    set({ view, openStepId: null, openAgent: null });
    if (view === 'report') void get().loadReport();
  },
  toggleTheme: () => {
    const theme: Theme = get().theme === 'light' ? 'dark' : 'light';
    try { localStorage.setItem('ledger.theme', theme); } catch { /* private mode */ }
    document.documentElement.dataset.theme = theme;
    set({ theme });
  },
  openStep: (id) => set({ openStepId: id, openAgent: null }),
  showAgent: (id, tab) => set({ openAgent: id ? { id, tab } : null, openStepId: null }),
  toast: (text, tone = 'info') => {
    const id = ++toastSeq;
    set((s) => ({ toasts: [...s.toasts.slice(-3), { id, text, tone }] }));
    setTimeout(() => get().dismissToast(id), tone === 'error' ? 7000 : 3500);
  },
  dismissToast: (id) => set((s) => ({ toasts: s.toasts.filter((t) => t.id !== id) })),
}));

/** Background polls for data without events (lease TTLs, budget, run list). */
export function startPolling(): () => void {
  const c = api();
  const agents = setInterval(() => {
    c.listAgents().then((a) => useLedger.setState({ agents: a, agentsAt: Date.now() })).catch(() => undefined);
  }, 3000);
  const slow = setInterval(() => {
    c.getBudget().then((budget) => useLedger.setState({ budget })).catch(() => undefined);
    c.listRuns().then((runs) => useLedger.setState({ runs: runs.slice().sort((a, b) => (b.created_at ?? 0) - (a.created_at ?? 0)) })).catch(() => undefined);
  }, 15000);
  return () => {
    clearInterval(agents);
    clearInterval(slow);
  };
}
