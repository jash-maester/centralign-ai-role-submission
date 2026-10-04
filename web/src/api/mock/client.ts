/**
 * In-browser mock of the Ledger API (VITE_MOCK=1 builds only). It replays the
 * scripted demo run as real ledger events over time and applies them to its
 * own records with the same reducer the store uses, so REST reads and the SSE
 * stream always agree.
 */
import type { ApiClient, ShellConnection, StreamHandlers } from '../client';
import type {
  AgentConfig, AgentConfigPatch, AgentStatus, Escalation, Fact, FaultName, LedgerEvent, LlmBudget, NewRun, Report, Run,
  RunConfig, RunConfigResponse, Step,
} from '../types';
import { reduceEvent, type RunSlice } from '../../store/reduce';
import { DEFAULT_RUN_CONFIG } from '../../lib/runConfig';
import { AGENT_CONFIGS, AGENTS, budgetFixture, buildReport, CRITERIA, GOAL, MODELS, PAST_RUNS } from './fixtures';
import { B1, buildRunScript, samLaneBeats, SAM_ESCALATION, toEvent, type Beat } from './script';
import { MockShell } from './shell';

interface MockRun {
  slice: RunSlice;
  events: LedgerEvent[];
  escalations: Escalation[];
  config: RunConfig;
  timers: ReturnType<typeof setTimeout>[];
  samAnswer: string | null;
  done: boolean;
  t0: number;
}

const wait = (ms = 60) => new Promise((r) => setTimeout(r, ms));
const clone = <T,>(x: T): T => JSON.parse(JSON.stringify(x)) as T;

let hashSeq = 0;
const fakeHash = (c: RunConfig) => {
  const s = JSON.stringify(c);
  let h = 0;
  for (let i = 0; i < s.length; i++) h = (h * 31 + s.charCodeAt(i)) | 0;
  return (h >>> 0).toString(16).padStart(8, '0') + 'c0ffee' + (hashSeq++ % 10);
};

export class MockClient implements ApiClient {
  readonly mode = 'mock' as const;
  private runs = new Map<string, MockRun>();
  private pastRuns: Run[] = clone(PAST_RUNS);
  private subs = new Set<{ runId: string; h: StreamHandlers }>();
  private seq = 0;
  private agentState: Record<string, { alive: boolean; hb: number; leasedAt?: number; model?: string; restarted?: boolean }> = {};
  private configs: Record<string, AgentConfig> = clone(AGENT_CONFIGS);
  private budgetUsed = 12;
  private faultCursor = 0;

  constructor() {
    for (const a of AGENTS) this.agentState[a.id] = { alive: true, hb: Date.now() };
    // The demo run is mid-flight on load ("04:12" in the design): replay the
    // first LIVE_AT seconds instantly, then play the rest in real time.
    this.startRun('run_7f3a', 200, 1);
    setInterval(() => this.heartbeat(), 1000);
  }

  // ---- run engine ---------------------------------------------------------------

  private startRun(runId: string, liveAt: number, speed: number): MockRun {
    const { initial, beats } = buildRunScript(runId);
    const now = Date.now();
    const t0 = now - liveAt * 1000;
    const run: Run = { id: runId, goal: GOAL, input_file: 'event_attendees.csv', input_sha256: '3f9e0c2b…a1', playbook: 'event-leads.md', playbook_hash: 'a91c', status: 'created', criteria: CRITERIA.map((c) => ({ ...c, status: 'pending' as const })), created_at: t0 };
    const mr: MockRun = { slice: { run, steps: {}, stepOrder: [], facts: [] }, events: [], escalations: [], config: { ...DEFAULT_RUN_CONFIG }, timers: [], samAnswer: null, done: false, t0 };
    void initial;
    this.runs.set(runId, mr);
    for (const beat of beats) {
      if (beat.at <= liveAt) this.emit(runId, beat, t0 + beat.at * 1000, false);
      else mr.timers.push(setTimeout(() => this.emit(runId, beat, Date.now()), ((beat.at - liveAt) * 1000) / speed));
    }
    return mr;
  }

  private emit(runId: string, beat: Beat, ts: number, live = true) {
    const mr = this.runs.get(runId);
    if (!mr) return;
    const ev = toEvent(runId, beat, ts, this.seq++);
    mr.events.push(ev);
    mr.slice = reduceEvent(mr.slice, ev).slice;
    this.track(ev);
    if (beat.effect === 'escalate_sam' && !mr.samAnswer) mr.escalations.push({ ...SAM_ESCALATION(runId), created_at: ts });
    if (beat.effect === 'agent_lost_b1') this.agentState[B1] = { ...this.agentState[B1], alive: false, hb: ts };
    if (beat.effect === 'finish') mr.done = true;
    if (live) for (const s of this.subs) if (s.runId === runId) s.h.onEvent(ev);
  }

  private emitNow(runId: string, type: string, actor: string, payload: Record<string, unknown> = {}, step_id: string | null = null) {
    this.emit(runId, { at: 0, type, actor, payload, step_id }, Date.now());
  }

  private track(ev: LedgerEvent) {
    const w = (ev.payload.worker as string) ?? ev.actor;
    if (ev.type === 'step.leased' && this.agentState[w]) this.agentState[w] = { ...this.agentState[w], leasedAt: ev.ts, model: (ev.payload.model as string) ?? this.agentState[w].model };
    if (ev.type === 'model.fallback' && this.agentState[ev.actor]) this.agentState[ev.actor].model = String(ev.payload.to ?? '');
    if (ev.type === 'llm.call' || ev.type === 'step.claimed') this.budgetUsed = Math.min(45, this.budgetUsed + (ev.type === 'llm.call' ? 1 : 0));
  }

  private heartbeat() {
    const now = Date.now();
    for (const [id, st] of Object.entries(this.agentState)) {
      if (!st.alive) continue;
      st.hb = now;
      // lease renews every TTL/3 like the real heartbeat
      if (st.leasedAt && now - st.leasedAt > 5000) st.leasedAt = now;
      this.agentState[id] = st;
    }
  }

  private mr(runId: string): MockRun {
    const r = this.runs.get(runId);
    if (!r) throw new Error(`404 run ${runId}`);
    return r;
  }

  private current(): MockRun | undefined {
    return [...this.runs.values()].sort((a, b) => b.t0 - a.t0)[0];
  }

  // ---- runs --------------------------------------------------------------------

  async listRuns(): Promise<Run[]> {
    await wait();
    const live = [...this.runs.values()].map((r) => {
      const steps = Object.values(r.slice.steps);
      return { ...r.slice.run!, steps_total: steps.length, steps_committed: steps.filter((s) => s.status === 'committed' || s.status === 'replanned').length };
    });
    return [...live, ...this.pastRuns];
  }
  async getRun(runId: string): Promise<Run> {
    await wait();
    const r = this.runs.get(runId);
    if (r) return clone(r.slice.run!);
    const past = this.pastRuns.find((x) => x.id === runId);
    if (!past) throw new Error(`404 run ${runId}`);
    return clone(past);
  }
  async createRun(body: NewRun): Promise<Run> {
    await wait(150);
    const id = `run_${Math.random().toString(16).slice(2, 6)}`;
    const mr = this.startRun(id, 0, 2.5);
    mr.slice.run = { ...mr.slice.run!, goal: body.goal || GOAL, input_file: body.input_file ?? 'event_attendees.csv' };
    return clone(mr.slice.run!);
  }
  async replayRun(runId: string): Promise<Run> {
    const src = this.runs.get(runId);
    const run = await this.createRun({ goal: src?.slice.run?.goal ?? GOAL, input_file: 'event_attendees.csv' });
    const mr = this.mr(run.id);
    mr.config = { ...(src?.config ?? DEFAULT_RUN_CONFIG) };
    mr.slice.run = { ...mr.slice.run!, replay_of: runId };
    return clone(mr.slice.run!);
  }
  async getSteps(runId: string): Promise<Step[]> {
    await wait();
    const r = this.runs.get(runId);
    if (!r) return [];
    return clone(r.slice.stepOrder.map((id) => r.slice.steps[id]));
  }
  async getStep(runId: string, stepId: string): Promise<Step> {
    await wait();
    const s = this.mr(runId).slice.steps[stepId];
    if (!s) throw new Error(`404 step ${stepId}`);
    return clone(s);
  }
  async getEvents(runId: string, opts: { after?: string; limit?: number } = {}): Promise<LedgerEvent[]> {
    await wait();
    const r = this.runs.get(runId);
    if (!r) return [];
    let evs = r.events;
    if (opts.after) evs = evs.slice(evs.findIndex((e) => e.id === opts.after) + 1);
    return clone(evs.slice(-(opts.limit ?? 2000)));
  }
  async getFacts(runId: string): Promise<Fact[]> {
    await wait();
    return clone(this.runs.get(runId)?.slice.facts ?? []);
  }
  async getReport(runId: string): Promise<Report> {
    await wait(120);
    const r = this.mr(runId);
    const dur = Math.round(((r.slice.run?.finished_at ?? Date.now()) - r.t0) / 1000);
    const rep = buildReport(runId, r.samAnswer, r.done, dur);
    rep.reproduce = { ...rep.reproduce, config: r.config, prompts: Object.fromEntries(Object.entries(this.configs).filter(([, c]) => c.prompt).map(([k, c]) => [k, c.prompt_version])) };
    rep.markdown = undefined;
    return rep;
  }
  async getReportMarkdown(runId: string): Promise<string> {
    const r = await this.getReport(runId);
    return `# Evidence report · ${runId}\n\n${r.summary}\n`;
  }

  // ---- run controls ----------------------------------------------------------------

  async setDeterminism(runId: string, level: number, seed?: number | null): Promise<void> {
    await wait();
    const r = this.mr(runId);
    r.config = { ...r.config, determinism: level, seed: seed ?? r.config.seed, seed_pinned: seed != null };
    this.emitNow(runId, 'config.updated', 'api', { summary: `determinism ${level.toFixed(2)} · workers T=${(1 - level).toFixed(2)} · verifier T=0${seed != null ? ` · seed ${seed}` : ' · seed random'}`, determinism: level });
  }
  async getRunConfig(runId: string): Promise<RunConfigResponse> {
    await wait();
    const r = this.runs.get(runId);
    const config = r?.config ?? { ...DEFAULT_RUN_CONFIG };
    return { config: { ...config }, config_hash: fakeHash(config) };
  }
  async putRunConfig(runId: string, patch: Partial<RunConfig>): Promise<RunConfigResponse> {
    await wait();
    const r = this.mr(runId);
    const diff = Object.fromEntries(Object.entries(patch).filter(([k, v]) => r.config[k as keyof RunConfig] !== v));
    r.config = { ...r.config, ...patch };
    if (Object.keys(diff).length) this.emitNow(runId, 'run.config_updated', 'api', { summary: Object.entries(diff).map(([k, v]) => `${k} → ${String(v)}`).join(' · '), diff });
    return { config: { ...r.config }, config_hash: fakeHash(r.config) };
  }
  async injectFault(fault: FaultName, body: Record<string, unknown> = {}): Promise<void> {
    await wait();
    const run = this.current();
    if (!run) return;
    const id = run.slice.run!.id;
    const n = this.faultCursor++;
    this.emitNow(id, 'fault.injected', 'chaos', { fault, ...body, summary: ({ false_claim: 'next browser step claims done without acting', kill_worker: `SIGKILL ${String(body.agent_id ?? 'browser operator holding a lease')}`, expire_session: 'CRM session invalidated', model_outage: 'primary worker model → 503', ui_changed: 'CRM UI selectors changed' } as Record<string, string>)[fault] });
    const later = (ms: number, type: string, actor: string, payload: Record<string, unknown>) =>
      run.timers.push(setTimeout(() => this.emitNow(id, type, actor, payload), ms));
    if (fault === 'false_claim') later(1600, 'step.rejected', 'verifier', { reason: 'REST: 0 records for the claimed contact → retry with reason', injected: n });
    if (fault === 'kill_worker') { later(1600, 'step.heartbeat_lost', 'orchestrator', { summary: 'no heartbeat for 15s' }); later(3200, 'step.lease_expired', 'orchestrator', { summary: 'lease gone → ready · fence +1 · takeover' }); }
    if (fault === 'expire_session') later(1600, 'step.observation', B1, { summary: 're-authenticated · resuming the same step' });
    if (fault === 'model_outage') later(1600, 'model.fallback', 'worker-drafter', { role: 'worker', from: MODELS.worker[0], to: MODELS.worker[1], error: '503' });
    if (fault === 'ui_changed') later(1600, 'plan.revised', 'orchestrator', { summary: 'UI path blocked → route crm writes to api.espocrm' });
  }
  async getBudget(): Promise<LlmBudget> {
    await wait();
    return budgetFixture(this.budgetUsed);
  }

  // ---- escalations ---------------------------------------------------------------------

  async listEscalations(runId?: string): Promise<Escalation[]> {
    await wait();
    const all = [...this.runs.values()].flatMap((r) => r.escalations);
    return clone(all.filter((e) => (!runId || e.run_id === runId) && e.status === 'open'));
  }
  async answerEscalation(escId: string, answer: string, saveAsRule: boolean): Promise<void> {
    await wait(120);
    for (const [runId, r] of this.runs) {
      const esc = r.escalations.find((e) => e.id === escId);
      if (!esc) continue;
      esc.status = 'answered';
      esc.answer = answer;
      esc.save_as_rule = saveAsRule;
      r.samAnswer = answer;
      r.done = false;
      const label = esc.options.find((o) => o.value === answer)?.label ?? answer;
      for (const beat of samLaneBeats(answer, label)) {
        const b = beat.type === 'input.answered' && saveAsRule ? { ...beat, payload: { ...beat.payload, save_as_rule: true, summary: `${String(beat.payload?.summary)} · saved as playbook rule` } } : beat;
        r.timers.push(setTimeout(() => this.emit(runId, b, Date.now()), beat.at * 700));
      }
      return;
    }
    throw new Error(`404 escalation ${escId}`);
  }

  // ---- agents ----------------------------------------------------------------------------

  async listAgents(): Promise<AgentStatus[]> {
    await wait(40);
    const run = this.current();
    const steps = run ? Object.values(run.slice.steps) : [];
    const open = run ? run.escalations.filter((e) => e.status === 'open').length : 0;
    const ttl = (run?.config.lease_ttl_s ?? 15) * 1000;
    return AGENTS.map((a) => {
      const st = this.agentState[a.id];
      const holding = steps.filter((s) => s.lease_owner === a.id && ['leased', 'claimed_done'].includes(s.status)).sort((x, y) => (y.updated_at ?? 0) - (x.updated_at ?? 0))[0];
      const done = steps.filter((s) => (s.history ?? []).some((h) => h.worker === a.id && h.outcome === 'committed')).length;
      const rej = steps.filter((s) => (s.history ?? []).some((h) => h.worker === a.id && h.outcome === 'rejected')).length;
      const verifierDone = a.id === 'verifier' ? steps.filter((s) => s.status === 'committed').length : done;
      return {
        ...clone(a),
        alive: st.alive,
        last_heartbeat_ms: a.role === 'human' ? null : st.hb,
        current_step: st.alive && holding ? holding.id : null,
        lease_fence: holding?.fence ?? null,
        lease_ttl_ms: st.alive && holding && st.leasedAt ? Math.max(0, ttl - (Date.now() - st.leasedAt)) : null,
        lease_total_ms: ttl,
        model: a.model_role ? st.model ?? a.models?.[0] ?? null : null,
        steps_done: a.role === 'human' ? (run?.samAnswer ? 1 : 0) : verifierDone,
        rejections: a.id === 'verifier' ? steps.filter((s) => (s.history ?? []).some((h) => h.outcome === 'rejected')).length : rej,
        ...(a.role === 'human' ? { current_step: open ? 'queue:human' : null } : {}),
      };
    });
  }
  async getAgentConfig(agentId: string): Promise<AgentConfig> {
    await wait();
    const c = this.configs[agentId] ?? { agent_id: agentId, prompt: '', prompt_version: 1, layers: [], tools: AGENTS.find((a) => a.id === agentId)?.tools ?? [] };
    return clone(c);
  }
  async putAgentConfig(agentId: string, patch: AgentConfigPatch): Promise<AgentConfig> {
    await wait();
    const c = (this.configs[agentId] ??= { agent_id: agentId, prompt: '', prompt_version: 1, layers: [], tools: [] });
    const run = this.current()?.slice.run?.id;
    if (patch.prompt !== undefined && patch.prompt !== c.prompt) {
      c.prompt = patch.prompt;
      c.prompt_version += 1;
      if (run) this.emitNow(run, 'prompt.updated', 'api', { agent_id: agentId, version: c.prompt_version, summary: `${agentId} → v${c.prompt_version} · applies on next attempt` });
    }
    if (patch.layers) {
      for (const [id, on] of Object.entries(patch.layers)) {
        const l = c.layers.find((x) => x.id === id);
        if (l && !l.locked && l.enabled !== on) {
          l.enabled = on;
          if (run) this.emitNow(run, 'prompt.updated', 'api', { agent_id: agentId, layer: id, enabled: on, summary: `${agentId} layer "${id}" ${on ? 'on' : 'off'}` });
        }
      }
    }
    if (patch.tools) {
      for (const t of patch.tools) {
        const cur = c.tools.find((x) => x.id === t.id);
        if (!cur) {
          c.tools.push({ ...t });
          if (run) this.emitNow(run, 'tool.toggled', 'api', { agent_id: agentId, tool: t.id, enabled: t.enabled !== false, summary: `${agentId} · ${t.name} added` });
        } else if (!cur.locked_reason && cur.enabled !== t.enabled) {
          cur.enabled = t.enabled;
          if (run) this.emitNow(run, 'tool.toggled', 'api', { agent_id: agentId, tool: t.id, enabled: t.enabled, summary: `${agentId} · ${t.name} ${t.enabled ? 'on' : 'off'}` });
        }
      }
    }
    return clone(c);
  }
  async restartAgent(agentId: string): Promise<void> {
    await wait(300);
    this.agentState[agentId] = { alive: true, hb: Date.now(), restarted: true };
    const run = this.current()?.slice.run?.id;
    if (run) this.emitNow(run, 'agent.registered', agentId, { agent_id: agentId, summary: `${agentId} restarted · heartbeat ok` });
  }
  openShell(agentId: string): ShellConnection {
    const run = this.current()?.slice.run?.id;
    if (run) this.emitNow(run, 'shell.opened', 'api', { agent_id: agentId, summary: `${agentId} · sandboxed exec` });
    return new MockShell(agentId, this.agentState[agentId]?.alive !== false);
  }

  evidenceUrl(path: string): string {
    return `data:image/svg+xml;utf8,${encodeURIComponent(`<svg xmlns="http://www.w3.org/2000/svg" width="320" height="200"><rect width="320" height="200" fill="#EDEDEA"/><text x="160" y="104" font-family="monospace" font-size="12" text-anchor="middle" fill="#8A8D93">${path.split('/').pop()}</text></svg>`)}`;
  }

  stream(runId: string, lastEventId: string | null, h: StreamHandlers): () => void {
    const sub = { runId, h };
    this.subs.add(sub);
    h.onStatus?.('open');
    // Last-Event-ID resume: replay anything newer than what the client has.
    const evs = this.runs.get(runId)?.events ?? [];
    const from = lastEventId ? evs.findIndex((e) => e.id === lastEventId) + 1 : 0;
    const missed = lastEventId && from === 0 ? [] : evs.slice(from);
    setTimeout(() => missed.forEach((e) => this.subs.has(sub) && h.onEvent(clone(e))), 0);
    return () => {
      this.subs.delete(sub);
      h.onStatus?.('closed');
    };
  }
}
