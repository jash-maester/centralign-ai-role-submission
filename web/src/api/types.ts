/**
 * Types for the Ledger API (plans/01-architecture.md §10).
 *
 * The protocol models mirror ledger_core/protocol.py; their enums are checked
 * against the generated web/src/api/protocol.schema.json by
 * src/test/protocol.test.ts, so a backend change that renames a member fails
 * the web tests. Response envelopes beyond the protocol models (agent status,
 * agent config, budget, report) are the GUI's expectations and are parsed
 * leniently by src/api/normalize.ts.
 */

// ---- enums (protocol.py) ---------------------------------------------------

export const STEP_STATUSES = [
  'planned', 'ready', 'leased', 'claimed_done', 'verified', 'committed', 'rejected',
  'replanned', 'lease_expired', 'review_required', 'input_required', 'dead',
] as const;
export type StepStatus = (typeof STEP_STATUSES)[number];

export const RUN_STATUSES = [
  'created', 'understanding', 'planning', 'running', 'completed', 'completed_pending_input', 'failed',
] as const;
export type RunStatus = (typeof RUN_STATUSES)[number];

export const SKILLS = ['file.parse', 'browser.espocrm', 'api.espocrm', 'email.draft', 'email.send', 'review', 'human'] as const;
export type Skill = (typeof SKILLS)[number];

export const STEP_KINDS = [
  'file.parse', 'crm.search_contact', 'crm.create_contact', 'crm.update_contact', 'crm.create_task',
  'email.draft', 'email.send', 'review.ambiguity', 'review.approval', 'human.decide', 'run.verify',
] as const;
export type StepKind = (typeof STEP_KINDS)[number];

export const EVENT_TYPES = [
  'run.created', 'run.understood', 'plan.created', 'plan.revised', 'step.ready', 'step.leased',
  'step.heartbeat_lost', 'step.lease_expired', 'step.observation', 'step.claimed', 'step.verified',
  'step.rejected', 'step.committed', 'step.dead', 'step.replanned', 'step.stale_fence', 'fact.committed',
  'input.requested', 'input.answered', 'review.requested', 'review.resolved', 'review.escalated',
  'approval.auto', 'config.updated', 'run.config_updated', 'prompt.updated', 'tool.toggled', 'shell.opened',
  'model.fallback', 'llm.call', 'llm.cache_hit', 'llm.budget_exhausted', 'spend.cap_reached',
  'agent.registered', 'agent.lost', 'fault.injected', 'run.completed', 'run.completed_pending_input',
  'run.failed', 'run.status',
] as const;
export type EventType = (typeof EVENT_TYPES)[number];

export const FAULT_NAMES = ['false_claim', 'kill_worker', 'expire_session', 'model_outage', 'ui_changed'] as const;
export type FaultName = (typeof FAULT_NAMES)[number];

export type ModelRole = 'orchestrator' | 'worker' | 'verifier' | 'meta_reviewer';

// ---- records ---------------------------------------------------------------

export interface Postcondition {
  check: string;
  args?: Record<string, unknown>;
  expect?: Record<string, unknown>;
}

export interface Verdict {
  ok: boolean;
  check: string;
  reason: string;
  observed?: Record<string, unknown>;
  verifier?: string;
  model?: string | null;
  ts?: number;
}

export interface Claim {
  worker: string;
  fence: number;
  summary: string;
  data?: Record<string, unknown>;
  evidence?: string[];
  acted?: boolean;
  ts?: number;
}

export interface Attempt {
  attempt: number;
  worker?: string | null;
  fence?: number;
  model?: string | null;
  observations?: Record<string, unknown>[];
  claim?: Claim | null;
  verdict?: Verdict | null;
  outcome?: string | null;
  started_at?: number;
  ended_at?: number | null;
}

export interface Step {
  id: string;
  run_id: string;
  kind: StepKind;
  skill: Skill;
  title?: string;
  inputs?: Record<string, unknown>;
  postcondition: Postcondition;
  depends_on?: string[];
  side_effect?: boolean;
  idempotency_key?: string | null;
  lane?: string | null;
  status: StepStatus;
  attempt: number;
  max_attempts?: number;
  fence?: number;
  lease_owner?: string | null;
  claim?: Claim | null;
  verdict?: Verdict | null;
  history?: Attempt[];
  created_at?: number;
  updated_at?: number;
}

export interface Criterion {
  id: string;
  text: string;
  check: string;
  status: 'pending' | 'verified' | 'waived' | 'failed';
  evidence?: string | null;
}

export interface Run {
  id: string;
  goal: string;
  input_file?: string | null;
  input_sha256?: string | null;
  playbook?: string;
  playbook_hash?: string | null;
  status: RunStatus;
  criteria: Criterion[];
  config_hash?: string | null;
  replay_of?: string | null;
  created_at: number;
  finished_at?: number | null;
  /** Optional summary counts some list endpoints add. */
  steps_total?: number;
  steps_committed?: number;
}

export interface Fact {
  key: string;
  value: unknown;
  source_step: string;
  committed_at?: number;
}

export interface LedgerEvent {
  id?: string | null; // Redis stream id, also the SSE id
  ts: number;
  run_id?: string | null;
  step_id?: string | null;
  actor: string;
  type: EventType | string;
  payload: Record<string, unknown>;
}

export interface ToolSpec {
  id: string;
  type: 'rest' | 'mcp' | 'function' | 'browser' | 'smtp' | 'llm';
  name: string;
  detail?: string;
  enabled?: boolean;
  locked_reason?: string | null;
}

export interface AgentSkill {
  id: Skill;
  kinds: StepKind[];
}

export interface AgentCard {
  id: string;
  name: string;
  role: 'orchestrator' | 'verifier' | 'meta_reviewer' | 'worker' | 'human';
  skills?: AgentSkill[];
  model_role?: ModelRole | null;
  side_effects?: boolean;
  tools?: ToolSpec[];
  container?: string | null;
}

/** GET /agents row: the card plus liveness and the lease it holds. */
export interface AgentStatus extends AgentCard {
  alive?: boolean;
  last_heartbeat_ms?: number | null; // epoch ms
  current_step?: string | null;
  lease_ttl_ms?: number | null; // remaining
  lease_total_ms?: number | null;
  lease_fence?: number | null;
  models?: string[]; // role model list from .env (primary first)
  model?: string | null; // model currently in use (after fallback)
  steps_done?: number;
  rejections?: number;
}

export interface ReviewOption {
  label: string;
  value: string;
  detail?: string;
}

export interface ReviewDecision {
  decision: 'link_account' | 'match_existing' | 'create_new' | 'skip' | 'approve' | 'reject';
  value?: string | null;
  confidence: number;
  threshold: number;
  evidence?: string[];
  options?: ReviewOption[];
  decided_by?: string;
  model?: string | null;
}

export interface Escalation {
  id: string;
  run_id: string;
  step_id: string;
  lane?: string | null;
  question: string;
  context?: string;
  options: ReviewOption[];
  tried?: string[];
  confidence: number;
  threshold: number;
  status?: 'open' | 'answered';
  answer?: string | null;
  save_as_rule?: boolean;
  created_at?: number;
}

/** RunConfig (ledger_core/config.py, plans/01 §6a). */
export interface RunConfig {
  review_auto_threshold: number;
  approval_auto_threshold: number;
  always_ask_human_email: boolean;
  llm_judge_enabled: boolean;
  lease_ttl_s: number;
  max_attempts: number;
  check_then_act: boolean;
  replan_after_rejections: number;
  model_fallback: boolean;
  determinism: number;
  seed: number;
  seed_pinned: boolean;
  browser_concurrency: number;
  crm_write_path: 'browser' | 'auto' | 'api';
  spend_cap_usd: number;
  fuzzy_match_threshold: number;
  dry_run: boolean;
}

export interface RunConfigResponse {
  config: RunConfig;
  config_hash?: string | null;
}

/** GET /llm/budget (plans/01 §6b). */
export interface LlmBudget {
  date?: string;
  used: number;
  limit: number;
  remaining: number;
  /** "openrouter": the key's own daily count (GET /api/v1/key); "local": the persistent fallback counter. */
  source?: 'openrouter' | 'local' | string;
  /** Requests never spent (the LLM layer refuses once remaining <= reserve). */
  reserve?: number;
  /** remaining - reserve: what live calls may still use today. */
  usable?: number;
  spent_usd?: number;
  cache_hits?: number;
  /** role -> model list, primary first (from .env, never hard-coded). */
  models?: Partial<Record<ModelRole, string[]>>;
}

export interface InjectionLayer {
  id: 'role' | 'playbook' | 'step' | 'history' | 'schema' | string;
  name: string;
  source?: string;
  tokens?: number;
  enabled: boolean;
  locked?: boolean;
  preview?: string;
}

export interface ToolCall {
  ts: number;
  call: string;
  code: string;
  ms?: number | null;
}

/** GET /agents/{id}/config */
export interface AgentConfig {
  agent_id: string;
  prompt: string;
  prompt_version: number;
  layers: InjectionLayer[];
  tools: ToolSpec[];
  recent_calls?: ToolCall[];
  temperature?: number | null;
}

/** PUT /agents/{id}/config body: any subset. */
export interface AgentConfigPatch {
  prompt?: string;
  layers?: Record<string, boolean>;
  tools?: ToolSpec[];
}

export interface PlaybookSection {
  heading: string;
  body: string;
  used_by?: string;
}

export interface Playbook {
  name: string;
  version?: string | number | null;
  hash?: string | null;
  sections: PlaybookSection[];
  markdown?: string;
}

export interface NewRun {
  goal: string;
  input_file?: string | null;
  playbook?: string;
}

// ---- evidence report (GET /runs/{id}/report) -------------------------------

export type LeadOutcome = 'created' | 'updated' | 'skipped' | 'waiting' | 'failed';

export interface ReportLead {
  n: number;
  name: string;
  company?: string;
  email?: string;
  outcome: LeadOutcome;
  outcome_detail?: string;
  owner?: string | null;
  task_due?: string | null;
  email_status?: string | null;
  decided_by?: string;
  check?: string;
  crm_url?: string | null;
  screenshot?: string | null; // evidence path
  tries?: number;
}

export interface ReportPhase {
  label: string;
  agent: string;
  start_s: number;
  end_s: number | null; // null = still open
  kind?: 'work' | 'review' | 'human' | 'verify';
}

export interface ReportFault {
  at_s: number;
  fault: string;
  what: string;
  recovery: string;
  lost_s?: number | null;
}

export interface ReportDecision {
  title: string;
  evidence: string[];
  /** null while the review is still open */
  confidence: number | null;
  threshold: number | null;
  result: string;
  decided_by: string;
  escalated?: boolean;
  /** auto | human | open: only committed decisions count as made (Track N). */
  state?: 'auto' | 'human' | 'open';
  forced_reason?: string | null;
}

export interface ReportCoverage {
  check: string;
  channel: string;
  runs: number;
  pass: number;
  reject: number;
  p50_ms?: number | null;
}

export interface ReportEmail {
  to: string;
  subject: string;
  judge?: number | null;
  approval: string;
  delivery: string;
}

export interface ReportAgentCost {
  agent: string;
  model: string;
  steps: number;
  rejections: number;
  retries: number;
  p50_ms?: number | null;
  tokens?: number | null;
  cost_usd?: number | null;
  requests?: number | null;
}

export interface ReportDataQuality {
  n: number | string;
  label: string;
  example?: string;
}

export interface Report {
  run_id: string;
  status: RunStatus;
  summary: string;
  goal: string;
  playbook?: string;
  duration_s?: number | null;
  criteria: Criterion[];
  leads: ReportLead[];
  phases: ReportPhase[];
  faults: ReportFault[];
  decisions: ReportDecision[];
  coverage: ReportCoverage[];
  emails: ReportEmail[];
  agents: ReportAgentCost[];
  input?: { file?: string | null; rows?: number; sha256?: string | null; quality?: ReportDataQuality[] };
  reproduce?: { config?: Partial<RunConfig>; config_hash?: string | null; prompts?: Record<string, number>; models?: Record<string, string[]>; faults?: string[]; command?: string };
  totals?: { tokens?: number; cost_usd?: number; requests?: number; duplicates?: number; lost_s?: number };
  markdown?: string;
}
