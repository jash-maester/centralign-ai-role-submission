/**
 * Static product copy per agent (from the designs). Everything that changes
 * at runtime (models, liveness, leases, tools, prompts) comes from the API.
 */
import type { AgentStatus } from '../api/types';

export interface AgentMeta { desc: string; reads: string; writes: string }

const BROWSER: AgentMeta = {
  desc: 'Drives EspoCRM through the browser. Check-then-act: if the postcondition already holds it claims done without acting, so takeovers never duplicate. Attaches a screenshot to every claim.',
  reads: 'queue:browser.espocrm, committed facts', writes: 'claim + screenshots',
};

export const AGENT_META: Record<string, AgentMeta> = {
  orchestrator: { desc: 'Turns goal + playbook into checkable success criteria and a step graph. Routes by skill, never by worker. Replans after repeated rejections and reaps expired leases every 2s.', reads: 'facts only', writes: 'task.submit · plan.*' },
  'meta-reviewer': { desc: 'Reviews anything a worker or the verifier flags as ambiguous, and every step the playbook marks as needing approval. It decides automatically when its confidence clears the threshold; only below that does a step go to queue:human.', reads: 'queue:review, facts, playbook, CRM REST (read-only)', writes: 'review.resolved · approval.auto · review.escalated' },
  verifier: { desc: 'Never trusts a claim. Reads the world through a different channel from the one used to act (REST for the CRM, the API for Mailpit). Deterministic checks run first; the LLM judge only where no hard check exists. The only agent that can commit a fact.', reads: 'claims, CRM REST, Mailpit API', writes: 'verified · committed · facts' },
  'worker-parser': { desc: 'Normalises messy attendee files: names, emails, phones, companies. Flags unusable rows with a reason instead of guessing.', reads: 'queue:file.parse', writes: 'claim' },
  'worker-drafter': { desc: 'Writes one follow-up email per lead from committed facts only, following the playbook tone rules.', reads: 'queue:email.draft, committed facts', writes: 'claim' },
  'worker-mailer': { desc: 'Sends only emails that have a committed approval fact. Deterministic, no model.', reads: 'queue:email.send, approval facts', writes: 'claim (SMTP)' },
  human: { desc: 'You are just another agent on the bus, consulted last. Steps only reach queue:human when the meta-reviewer cannot decide with enough confidence. Your answer is committed as a fact and the waiting lane continues.', reads: 'queue:human', writes: 'input.answered' },
  ledger: { desc: "Redis 7 with streams and hashes. Append-only event log, step records, leases with fencing tokens, per-skill queues and committed facts. Agents never read each other's messages; they read this.", reads: '—', writes: '—' },
  playbook: { desc: 'Company context that fills in everything the one-line goal leaves out. It is split into sections; each worker only receives the sections relevant to its step kind.', reads: '—', writes: '—' },
};

export function agentMeta(a: Pick<AgentStatus, 'id' | 'role'>): AgentMeta {
  if (AGENT_META[a.id]) return AGENT_META[a.id];
  if (a.id.startsWith('worker-browser')) return BROWSER;
  return { desc: `${a.role} agent.`, reads: '—', writes: '—' };
}

/** Pseudo-agents shown in the flow strip and builder that are not in GET /agents. */
export const PSEUDO_AGENTS: AgentStatus[] = [
  { id: 'ledger', name: 'Ledger', role: 'orchestrator', container: 'redis', model_role: null },
  { id: 'playbook', name: 'Playbook', role: 'orchestrator', model_role: null },
];

export const TOOL_COLOR: Record<string, string> = {
  mcp: 'var(--s-input)', rest: 'var(--s-leased)', function: 'var(--fg2)', browser: 'var(--s-claimed)', llm: 'var(--s-committed)', smtp: 'var(--s-ready)',
};
export const TOOL_LABEL: Record<string, string> = { mcp: 'MCP', rest: 'REST', function: 'FUNC', browser: 'BROWSER', llm: 'LLM', smtp: 'SMTP' };

/** Channel each registered check reads through (plans/01 §8). */
export const CHECK_CHANNEL: Record<string, string> = {
  'file.parsed_rows': 'source file', 'crm.lookup_matches': 'CRM REST', 'crm.contact_exists': 'CRM REST', 'crm.no_duplicate': 'CRM REST',
  'crm.task_exists': 'CRM REST', 'email.draft_valid': 'rules + LLM judge', 'email.sent': 'Mailpit API', 'review.decided': 'ledger facts', 'run.criteria_met': 'ledger sweep',
};
