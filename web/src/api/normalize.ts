/**
 * Lenient response parsing. The API (Track H) owns the exact envelopes; the GUI
 * accepts a bare list or a `{items|runs|steps|events|...: [...]}` wrapper, a
 * facts hash or list, and a RunConfig with or without a `{config, config_hash}`
 * wrapper, so small envelope differences never blank a screen.
 */
import type { Fact, LedgerEvent, Report, RunConfig, RunConfigResponse } from './types';
import { DEFAULT_RUN_CONFIG } from '../lib/runConfig';

type Obj = Record<string, unknown>;
const isObj = (x: unknown): x is Obj => typeof x === 'object' && x !== null && !Array.isArray(x);

export function asList<T>(x: unknown, ...keys: string[]): T[] {
  if (Array.isArray(x)) return x as T[];
  if (isObj(x)) {
    for (const k of [...keys, 'items', 'data', 'results']) {
      if (Array.isArray(x[k])) return x[k] as T[];
    }
  }
  return [];
}

export function asFacts(x: unknown): Fact[] {
  if (Array.isArray(x)) return x as Fact[];
  const src = isObj(x) && isObj(x.facts) ? x.facts : x;
  if (!isObj(src)) return [];
  return Object.entries(src).map(([key, raw]) => {
    let v: unknown = raw;
    if (typeof raw === 'string') {
      try { v = JSON.parse(raw); } catch { v = raw; }
    }
    if (isObj(v) && 'value' in v) {
      return { key, value: v.value, source_step: String(v.source_step ?? ''), committed_at: Number(v.committed_at ?? 0) || undefined };
    }
    return { key, value: v, source_step: '' };
  });
}

export function asEvent(x: unknown, sseId?: string | null): LedgerEvent | null {
  if (!isObj(x) || typeof x.type !== 'string') return null;
  return {
    id: (x.id as string | null | undefined) ?? sseId ?? null,
    ts: Number(x.ts ?? Date.now()),
    run_id: (x.run_id as string | null | undefined) ?? null,
    step_id: (x.step_id as string | null | undefined) ?? null,
    actor: String(x.actor ?? 'ledger'),
    type: x.type,
    payload: isObj(x.payload) ? x.payload : {},
  };
}

export function asRunConfig(x: unknown): RunConfigResponse {
  if (isObj(x) && isObj(x.config)) {
    return { config: { ...DEFAULT_RUN_CONFIG, ...(x.config as Partial<RunConfig>) }, config_hash: (x.config_hash as string) ?? null };
  }
  if (isObj(x)) {
    const { config_hash, ...rest } = x;
    return { config: { ...DEFAULT_RUN_CONFIG, ...(rest as Partial<RunConfig>) }, config_hash: (config_hash as string) ?? null };
  }
  return { config: { ...DEFAULT_RUN_CONFIG }, config_hash: null };
}

/** Fill every report section so the view can render a partial report. */
export function asReport(x: unknown, runId: string): Report {
  const r: Obj = isObj(x) && isObj(x.report) ? x.report : isObj(x) ? x : {};
  const list = <T,>(k: string) => (Array.isArray(r[k]) ? (r[k] as T[]) : []);
  return {
    run_id: String(r.run_id ?? runId),
    status: (r.status as Report['status']) ?? 'running',
    summary: String(r.summary ?? ''),
    goal: String(r.goal ?? ''),
    playbook: r.playbook as string | undefined,
    duration_s: (r.duration_s as number | null | undefined) ?? null,
    criteria: list('criteria'),
    leads: list('leads'),
    phases: list('phases'),
    faults: list('faults'),
    decisions: list('decisions'),
    coverage: list('coverage'),
    emails: list('emails'),
    agents: list('agents'),
    input: isObj(r.input) ? (r.input as Report['input']) : undefined,
    reproduce: isObj(r.reproduce) ? (r.reproduce as Report['reproduce']) : undefined,
    totals: isObj(r.totals) ? (r.totals as Report['totals']) : undefined,
    markdown: typeof r.markdown === 'string' ? r.markdown : undefined,
  };
}
