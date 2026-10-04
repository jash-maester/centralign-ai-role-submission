/** Real API client: every endpoint in plans/01 §10 under the "/api" prefix. */
import type { ApiClient, ShellConnection, StreamHandlers } from './client';
import { asFacts, asList, asReport, asRunConfig } from './normalize';
import { openEventStream } from './sse';
import type {
  AgentConfig, AgentConfigPatch, AgentStatus, Escalation, FaultName, LedgerEvent, LlmBudget, NewRun, Run,
  RunConfig, Step,
} from './types';

export class ApiError extends Error {
  constructor(public status: number, message: string) {
    super(message);
  }
}

const enc = encodeURIComponent;

export class HttpClient implements ApiClient {
  readonly mode = 'http' as const;
  constructor(private base = '/api') {}

  private async req<T>(method: string, path: string, body?: unknown): Promise<T> {
    const res = await fetch(this.base + path, {
      method,
      headers: body === undefined ? { Accept: 'application/json' } : { 'Content-Type': 'application/json', Accept: 'application/json' },
      body: body === undefined ? undefined : JSON.stringify(body),
    });
    if (!res.ok) {
      let detail = res.statusText;
      try {
        const j = await res.json();
        detail = typeof j.detail === 'string' ? j.detail : JSON.stringify(j.detail ?? j);
      } catch { /* not json */ }
      throw new ApiError(res.status, `${method} ${path}: ${res.status} ${detail}`);
    }
    if (res.status === 204) return undefined as T;
    const text = await res.text();
    return (text ? JSON.parse(text) : undefined) as T;
  }

  listRuns = async () => asList<Run>(await this.req('GET', '/runs'), 'runs');
  getRun = (id: string) => this.req<Run>('GET', `/runs/${enc(id)}`);
  createRun = (body: NewRun) => this.req<Run>('POST', '/runs', body);
  replayRun = (id: string) => this.req<Run>('POST', `/runs/${enc(id)}/replay`);
  getSteps = async (id: string) => asList<Step>(await this.req('GET', `/runs/${enc(id)}/steps`), 'steps');
  getStep = (id: string, stepId: string) => this.req<Step>('GET', `/runs/${enc(id)}/steps/${enc(stepId)}`);
  getEvents = async (id: string, opts: { after?: string; limit?: number } = {}) => {
    const q = new URLSearchParams();
    if (opts.after) q.set('after', opts.after);
    if (opts.limit) q.set('limit', String(opts.limit));
    const qs = q.toString();
    return asList<LedgerEvent>(await this.req('GET', `/runs/${enc(id)}/events${qs ? '?' + qs : ''}`), 'events');
  };
  getFacts = async (id: string) => asFacts(await this.req('GET', `/runs/${enc(id)}/facts`));
  getReport = async (id: string) => asReport(await this.req('GET', `/runs/${enc(id)}/report`), id);
  getReportMarkdown = async (id: string) => {
    const res = await fetch(`${this.base}/runs/${enc(id)}/report?format=md`, { headers: { Accept: 'text/markdown' } });
    if (!res.ok) throw new ApiError(res.status, `report markdown ${res.status}`);
    const type = res.headers.get('content-type') || '';
    if (type.includes('json')) {
      const j = await res.json();
      return String(j.markdown ?? '');
    }
    return res.text();
  };

  setDeterminism = (id: string, level: number, seed?: number | null) =>
    this.req<void>('POST', `/runs/${enc(id)}/determinism`, seed == null ? { level } : { level, seed });
  getRunConfig = async (id: string) => asRunConfig(await this.req('GET', `/runs/${enc(id)}/config`));
  putRunConfig = async (id: string, patch: Partial<RunConfig>) => asRunConfig(await this.req('PUT', `/runs/${enc(id)}/config`, patch));
  injectFault = (fault: FaultName, body?: Record<string, unknown>) => this.req<void>('POST', `/chaos/${enc(fault)}`, body ?? {});
  getBudget = () => this.req<LlmBudget>('GET', '/llm/budget');

  listEscalations = async (runId?: string) =>
    asList<Escalation>(await this.req('GET', `/escalations${runId ? `?run_id=${enc(runId)}` : ''}`), 'escalations');
  answerEscalation = (id: string, answer: string, saveAsRule: boolean) =>
    this.req<void>('POST', `/escalations/${enc(id)}`, { answer, save_as_rule: saveAsRule });

  listAgents = async () => asList<AgentStatus>(await this.req('GET', '/agents'), 'agents');
  getAgentConfig = (id: string) => this.req<AgentConfig>('GET', `/agents/${enc(id)}/config`);
  putAgentConfig = (id: string, patch: AgentConfigPatch) => this.req<AgentConfig>('PUT', `/agents/${enc(id)}/config`, patch);
  restartAgent = (id: string) => this.req<void>('POST', `/agents/${enc(id)}/restart`);

  openShell(agentId: string): ShellConnection {
    const proto = location.protocol === 'https:' ? 'wss:' : 'ws:';
    const ws = new WebSocket(`${proto}//${location.host}${this.base}/agents/${enc(agentId)}/exec`);
    ws.binaryType = 'arraybuffer';
    const pending: string[] = [];
    const dataCbs: ((d: string) => void)[] = [];
    const closeCbs: ((r: string) => void)[] = [];
    const dec = new TextDecoder();
    ws.onopen = () => pending.splice(0).forEach((m) => ws.send(m));
    ws.onmessage = (m) => {
      const text = typeof m.data === 'string' ? m.data : dec.decode(new Uint8Array(m.data as ArrayBuffer));
      dataCbs.forEach((cb) => cb(text));
    };
    ws.onclose = (e) => closeCbs.forEach((cb) => cb(e.reason || `connection closed (${e.code})`));
    ws.onerror = () => dataCbs.forEach((cb) => cb('\r\n\x1b[31mshell connection error\x1b[0m\r\n'));
    const send = (s: string) => (ws.readyState === WebSocket.OPEN ? ws.send(s) : pending.push(s));
    return {
      send,
      resize: (cols, rows) => send(JSON.stringify({ type: 'resize', cols, rows })),
      onData: (cb) => dataCbs.push(cb),
      onClose: (cb) => closeCbs.push(cb),
      close: () => ws.close(),
    };
  }

  evidenceUrl = (path: string) => `${this.base}/evidence/${path.replace(/^\/?(evidence\/)?/, '')}`;

  stream(runId: string, lastEventId: string | null, h: StreamHandlers) {
    return openEventStream(`${this.base}/stream?run_id=${enc(runId)}`, lastEventId, h);
  }
}
