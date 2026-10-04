import type {
  AgentConfig, AgentConfigPatch, AgentStatus, Escalation, Fact, FaultName, LedgerEvent, LlmBudget, NewRun,
  Report, Run, RunConfig, RunConfigResponse, Step,
} from './types';

export type StreamStatus = 'connecting' | 'open' | 'reconnecting' | 'closed';

export interface StreamHandlers {
  onEvent: (e: LedgerEvent) => void;
  onStatus?: (s: StreamStatus) => void;
}

/** A sandboxed shell into an agent's container (WS /agents/{id}/exec). */
export interface ShellConnection {
  send: (data: string) => void;
  resize?: (cols: number, rows: number) => void;
  onData: (cb: (data: string) => void) => void;
  onClose: (cb: (reason: string) => void) => void;
  close: () => void;
}

/**
 * Everything the GUI calls. `HttpClient` talks to the real API under /api;
 * `MockClient` (VITE_MOCK=1 only) replays the design's fixtures.
 */
export interface ApiClient {
  readonly mode: 'http' | 'mock';
  // runs
  listRuns(): Promise<Run[]>;
  getRun(runId: string): Promise<Run>;
  createRun(body: NewRun): Promise<Run>;
  replayRun(runId: string): Promise<Run>;
  getSteps(runId: string): Promise<Step[]>;
  getStep(runId: string, stepId: string): Promise<Step>;
  getEvents(runId: string, opts?: { after?: string; limit?: number }): Promise<LedgerEvent[]>;
  getFacts(runId: string): Promise<Fact[]>;
  getReport(runId: string): Promise<Report>;
  getReportMarkdown(runId: string): Promise<string>;
  // run controls
  setDeterminism(runId: string, level: number, seed?: number | null): Promise<void>;
  getRunConfig(runId: string): Promise<RunConfigResponse>;
  putRunConfig(runId: string, patch: Partial<RunConfig>): Promise<RunConfigResponse>;
  injectFault(fault: FaultName, body?: Record<string, unknown>): Promise<void>;
  getBudget(): Promise<LlmBudget>;
  // escalations
  listEscalations(runId?: string): Promise<Escalation[]>;
  answerEscalation(escId: string, answer: string, saveAsRule: boolean): Promise<void>;
  // agents
  listAgents(): Promise<AgentStatus[]>;
  getAgentConfig(agentId: string): Promise<AgentConfig>;
  putAgentConfig(agentId: string, patch: AgentConfigPatch): Promise<AgentConfig>;
  restartAgent(agentId: string): Promise<void>;
  openShell(agentId: string): ShellConnection;
  // evidence + live stream
  evidenceUrl(path: string): string;
  stream(runId: string, lastEventId: string | null, handlers: StreamHandlers): () => void;
}
