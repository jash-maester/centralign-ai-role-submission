/**
 * Mock fixtures transcribed from web/design/Ledger Dashboard.dc.html and
 * Ledger Canvas.dc.html (MOCK_* blocks). Used only when VITE_MOCK=1.
 * Model names are the free-phase role config from plans/01 §6, never the
 * design's placeholder names.
 */
import type {
  AgentConfig, AgentStatus, InjectionLayer, LlmBudget, ModelRole, Report, ReportAgentCost, ReportCoverage,
  ReportDataQuality, ReportPhase, Run, ToolSpec,
} from '../types';

export const MODELS: Record<ModelRole, string[]> = {
  orchestrator: ['nvidia/nemotron-3-super-120b-a12b:free', 'qwen/qwen3.8-27b:free'],
  meta_reviewer: ['nvidia/nemotron-3-super-120b-a12b:free', 'qwen/qwen3.8-27b:free'],
  worker: ['qwen/qwen3.8-27b:free', 'google/gemma-4-31b-it:free'],
  verifier: ['nvidia/nemotron-3-super-120b-a12b:free', 'dots-studio/dots-3-note-preview:free'],
};

export const GOAL = "Add the leads from yesterday's event to the CRM and set up follow-ups.";

export interface LeadRow {
  n: number;
  name: string;
  company: string;
  email: string;
  phone?: string;
  owner: string;
  write: 'create' | 'update' | null; // null = no CRM write
  email_out: boolean; // gets a follow-up email
  special?: 'false_claim' | 'takeover' | 'session' | 'fuzzy' | 'escalated' | 'skip' | 'dup_in_file' | 'outage';
}

export const LEADS: LeadRow[] = [
  { n: 1, name: 'Priya Raman', company: 'Northwind', email: 'priya@northwind.com', owner: 'a.chen', write: 'create', email_out: true, special: 'outage' },
  { n: 2, name: 'Marcus Lee', company: 'Acme Corp', email: 'marcus.lee@acme.com', owner: 'r.silva', write: 'update', email_out: true },
  { n: 3, name: 'Lena Fischer', company: 'Kestrel Labs', email: 'lena@kestrel-labs.io', owner: 'r.silva', write: 'create', email_out: true },
  { n: 4, name: 'Dana Okafor', company: 'Helix Bio', email: 'dana@helixbio.com', owner: 'a.chen', write: 'create', email_out: true, special: 'false_claim' },
  { n: 5, name: 'Tom Becker', company: 'Orbital Freight', email: 'tom.becker@orbitalfreight.com', owner: 'r.silva', write: 'create', email_out: true, special: 'takeover' },
  { n: 6, name: 'Lena Fischer', company: 'Kestrel Labs', email: 'lena@kestrel-labs.io', owner: '—', write: null, email_out: false, special: 'dup_in_file' },
  { n: 7, name: 'Ben Ortiz', company: 'Quarry Data', email: 'ben.ortiz@gmail.com', phone: '+1 415 555 0119', owner: 'a.chen', write: 'update', email_out: false, special: 'fuzzy' },
  { n: 8, name: 'Hannah Cole', company: 'Fieldstone', email: 'hannah@fieldstone.dev', owner: 'a.chen', write: 'update', email_out: true },
  { n: 9, name: 'Sam Ito', company: 'Lumen', email: 'sam@lumen.io', owner: 'a.chen', write: 'create', email_out: true, special: 'escalated' },
  { n: 10, name: 'Omar Haddad', company: 'Brightline', email: 'omar@brightline.co', owner: 'a.chen', write: 'create', email_out: true },
  { n: 11, name: 'Jo Park', company: '', email: '', phone: '+1 415 555 0182', owner: '—', write: null, email_out: false, special: 'skip' },
  { n: 12, name: 'Grace Wu', company: 'Tallgrass', email: 'grace.wu@tallgrass.com', owner: 'r.silva', write: 'create', email_out: true, special: 'session' },
];

export const CRITERIA = [
  { id: 'c1', text: 'Every usable row is a CRM contact exactly once', check: 'crm.no_duplicate' },
  { id: 'c2', text: 'Existing contacts enriched, not duplicated', check: 'crm.no_duplicate' },
  { id: 'c3', text: 'Each contact has a follow-up task due in 2 business days', check: 'crm.task_exists' },
  { id: 'c4', text: 'Owner assigned per routing rules', check: 'crm.contact_exists' },
  { id: 'c5', text: 'Rows without email skipped with a reason', check: 'review.decided' },
  { id: 'c6', text: 'Emails sent only with an approval fact', check: 'email.sent' },
];

export const PAST_RUNS: Run[] = [
  { id: 'run_6c21', goal: 'Import webinar signups and assign owners by region.', status: 'completed', criteria: [], created_at: Date.now() - 86_400_000, finished_at: Date.now() - 86_400_000 + 408_000, steps_total: 28, steps_committed: 28 },
  { id: 'run_5b90', goal: 'Add booth leads from the summit to the CRM.', status: 'completed', criteria: [], created_at: Date.now() - 2 * 86_400_000, finished_at: Date.now() - 2 * 86_400_000 + 542_000, steps_total: 38, steps_committed: 38 },
  { id: 'run_5a11', goal: 'Update renewal dates from the finance sheet.', status: 'failed', criteria: [], created_at: Date.now() - 3 * 86_400_000, finished_at: Date.now() - 3 * 86_400_000 + 135_000, steps_total: 19, steps_committed: 7 },
];

// ---- agents (GET /agents) ----------------------------------------------------

const tool = (id: string, type: ToolSpec['type'], name: string, detail: string, extra: Partial<ToolSpec> = {}): ToolSpec => ({ id, type, name, detail, enabled: true, ...extra });
const CRM_REST_LOCKED = (id: string) => tool(id, 'rest', 'espocrm REST', 'blocked for workers', { enabled: false, locked_reason: 'Verifier-only channel. Workers act through the UI.' });

export const AGENTS: AgentStatus[] = [
  { id: 'orchestrator', name: 'Orchestrator', role: 'orchestrator', model_role: 'orchestrator', container: 'orchestrator', skills: [], models: MODELS.orchestrator,
    tools: [tool('plan.create', 'function', 'plan.create', 'ledger_core.orchestrator'), tool('route.by_skill', 'function', 'route.by_skill', 'queue:{skill}'), tool('ledger-mcp', 'mcp', 'ledger-mcp', 'stdio · read_facts, list_steps'), tool('ledger-api', 'rest', 'ledger api', 'http://api:8000')] },
  { id: 'meta-reviewer', name: 'Meta-reviewer', role: 'meta_reviewer', model_role: 'meta_reviewer', container: 'meta-reviewer', skills: [{ id: 'review', kinds: ['review.ambiguity', 'review.approval'] }], models: MODELS.meta_reviewer,
    tools: [tool('crm-rest', 'rest', 'espocrm REST', 'read-only · accounts, contacts, deals'), tool('playbook.rules', 'function', 'playbook.rules', 'match escalation + approval rules'), tool('judge', 'llm', 'judge', 'role=meta_reviewer · email tone/policy'), tool('enrichment-mcp', 'mcp', 'enrichment-mcp', 'stdio · company_lookup', { enabled: false })] },
  { id: 'verifier', name: 'Verifier', role: 'verifier', model_role: 'verifier', container: 'verifier', skills: [], models: MODELS.verifier,
    tools: [tool('crm-rest', 'rest', 'espocrm REST', 'http://espocrm/api/v1 · X-Api-Key (read-only)'), tool('mailpit-api', 'rest', 'mailpit API', 'http://mailpit:8025/api/v1'), tool('postconditions', 'function', 'postconditions.*', 'registry · 9 checks'), tool('judge', 'llm', 'judge', 'role=verifier · soft checks')] },
  { id: 'worker-parser', name: 'Parser', role: 'worker', model_role: 'worker', container: 'worker-parser', skills: [{ id: 'file.parse', kinds: ['file.parse'] }], models: MODELS.worker,
    tools: [tool('csv.read', 'function', 'csv.read', 'ledger_core.workers.parser'), tool('pdf.extract', 'function', 'pdf.extract', 'pdfplumber'), tool('phone.normalise', 'function', 'phone.normalise', 'phonenumbers')] },
  { id: 'worker-browser-1', name: 'Browser operator 1', role: 'worker', model_role: 'worker', side_effects: true, container: 'worker-browser-1', skills: [{ id: 'browser.espocrm', kinds: ['crm.search_contact', 'crm.create_contact', 'crm.update_contact', 'crm.create_task'] }], models: MODELS.worker,
    tools: [tool('playwright', 'browser', 'playwright.chromium', 'headless · espocrm:80'), tool('playwright-mcp', 'mcp', 'playwright-mcp', 'stdio · navigate, click, fill, snapshot'), tool('screenshot.save', 'function', 'screenshot.save', 'volume: evidence/'), CRM_REST_LOCKED('crm-rest')] },
  { id: 'worker-browser-2', name: 'Browser operator 2', role: 'worker', model_role: 'worker', side_effects: true, container: 'worker-browser-2', skills: [{ id: 'browser.espocrm', kinds: ['crm.search_contact', 'crm.create_contact', 'crm.update_contact', 'crm.create_task'] }], models: MODELS.worker,
    tools: [tool('playwright', 'browser', 'playwright.chromium', 'headless · espocrm:80'), tool('playwright-mcp', 'mcp', 'playwright-mcp', 'stdio · navigate, click, fill, snapshot'), tool('screenshot.save', 'function', 'screenshot.save', 'volume: evidence/'), CRM_REST_LOCKED('crm-rest')] },
  { id: 'worker-drafter', name: 'Drafter', role: 'worker', model_role: 'worker', container: 'worker-drafter', skills: [{ id: 'email.draft', kinds: ['email.draft'] }], models: MODELS.worker,
    tools: [tool('facts.read', 'function', 'facts.read', 'ledger (read-only)'), tool('templates-mcp', 'mcp', 'templates-mcp', 'http://templates:7010 · get_template'), tool('openrouter', 'llm', 'openrouter', 'role=worker')] },
  { id: 'worker-mailer', name: 'Mailer', role: 'worker', model_role: null, side_effects: true, container: 'worker-mailer', skills: [{ id: 'email.send', kinds: ['email.send'] }], models: [],
    tools: [tool('mailpit', 'smtp', 'mailpit', 'mailpit:1025'), tool('approval.check', 'function', 'approval.check', 'facts:approval:*')] },
  { id: 'human', name: 'Human (you)', role: 'human', model_role: null, skills: [{ id: 'human', kinds: ['human.decide'] }], models: [] },
];

// ---- agent sheets (GET /agents/{id}/config) ---------------------------------

const layers = (sections: string, x: { role?: string; step?: string; hist?: string; schema?: string; pb?: string } = {}): InjectionLayer[] => [
  { id: 'role', name: 'Role + skill instructions', source: 'static', tokens: 310, enabled: true, locked: true, preview: x.role ?? 'Role, allowed skills, output contract.' },
  { id: 'playbook', name: 'Playbook sections', source: `playbook.md ${sections}`, tokens: 540, enabled: true, preview: x.pb ?? 'Only the sections relevant to this step kind.' },
  { id: 'step', name: 'Step + inputs from committed facts', source: 'facts:{run}', tokens: 180, enabled: true, preview: x.step ?? 'kind, inputs, postcondition' },
  { id: 'history', name: 'Prior attempts + rejection reasons', source: 'step.history', tokens: 220, enabled: true, locked: true, preview: x.hist ?? 'attempt 1 rejected: "no record via REST"' },
  { id: 'schema', name: 'Output schema', source: 'claim.schema.json', tokens: 120, enabled: true, preview: x.schema ?? '{ status, observation, evidence[] }' },
];

const BROWSER_PROMPT = 'You operate EspoCRM through the browser.\nBefore any write, check whether the postcondition already holds; if it does, claim done without acting.\nAttach a screenshot to every claim. Never report a success you did not observe.';

const ago = (s: number) => Date.now() - s * 1000;

export const AGENT_CONFIGS: Record<string, AgentConfig> = {
  orchestrator: { agent_id: 'orchestrator', prompt_version: 1, prompt: 'You are the orchestrator for Ledger.\n\nExpand the goal into explicit, checkable success criteria using the playbook.\nEmit a step graph. Every step names a skill and a registered postcondition.\nNever name a specific worker. Read only committed facts.\nBatch all ambiguous rows into one review step.',
    layers: layers('§ all', { step: 'goal, file ref, criteria so far', hist: 'plan.revised ×1 (UI path blocked)', schema: '{ criteria[], steps[] }' }), tools: AGENTS[0].tools!,
    recent_calls: [{ ts: ago(62), call: 'route.by_skill("review", stp_95)', code: '200', ms: 4 }, { ts: ago(130), call: 'reap_leases() → stp_51', code: '200', ms: 2 }, { ts: ago(230), call: 'plan.create(fanout=11)', code: '200', ms: 1800 }] },
  'meta-reviewer': { agent_id: 'meta-reviewer', prompt_version: 1, prompt: 'You resolve ambiguity so humans do not have to.\nUse committed facts, the playbook and read-only CRM lookups.\nReturn a decision with a confidence and your evidence.\nIf confidence is below the threshold, escalate with the options and what you tried.',
    layers: layers('§ Dedupe · § Approval · § Escalation', { role: 'Decide or escalate; never act on external systems.', step: 'ambiguous: lead:9 Sam Ito → 2 accounts', hist: 'domain check inconclusive', schema: '{ decision, confidence, evidence[], options[] }' }), tools: AGENTS[1].tools!,
    recent_calls: [{ ts: ago(70), call: 'GET /Account?where[name]=Lumen', code: '200 · 2', ms: 88 }, { ts: ago(69), call: 'GET /Contact?where[emailAddress]=*@lumen.io', code: '200 · 0', ms: 74 }, { ts: ago(40), call: 'judge.email ×8 → min 0.94', code: '200', ms: 6100 }] },
  verifier: { agent_id: 'verifier', prompt_version: 1, prompt: 'You are the verifier. You never trust a claim.\nRun the registered deterministic check first, via the REST API.\nUse the LLM judge only where no hard check exists, and record your reasoning.\nOnly you may commit a fact.',
    layers: layers('§ Definitions of done', { role: 'Verify postconditions; commit or reject with a reason.', step: 'claim stp_23 + postcondition crm.task_exists', hist: '—', schema: '{ verdict, reason, evidence }' }), tools: AGENTS[2].tools!, temperature: 0,
    recent_calls: [{ ts: ago(20), call: 'GET /Contact?where[emailAddress]=marcus.lee@acme.com', code: '200', ms: 82 }, { ts: ago(115), call: 'GET /Contact?where[emailAddress]=dana@helixbio.com', code: '200 · 0', ms: 77 }, { ts: ago(8), call: 'GET /Task?where[parentId]=3b07', code: '200', ms: 91 }] },
  'worker-parser': { agent_id: 'worker-parser', prompt_version: 1, prompt: 'You normalise attendee files into rows that match the schema.\nTrim whitespace, normalise phones to E.164, lowercase emails.\nFlag unusable rows with a reason. Never invent an email.',
    layers: layers('§ Dedupe rules', { step: 'file: event_attendees.csv (12 rows)', hist: '—', schema: '{ rows[], flagged[] }' }), tools: AGENTS[3].tools!,
    recent_calls: [{ ts: ago(232), call: 'csv.read("event_attendees.csv")', code: '200', ms: 38 }, { ts: ago(231), call: 'phone.normalise ×12', code: '200', ms: 6 }] },
  'worker-browser-1': { agent_id: 'worker-browser-1', prompt_version: 1, prompt: BROWSER_PROMPT,
    layers: layers('§ Dedupe · § Owner routing', { step: 'crm.create_contact · lead:5 Tom Becker', hist: '—', schema: '{ status, observation, screenshot }' }), tools: AGENTS[4].tools!,
    recent_calls: [{ ts: ago(150), call: 'fill(#name, "Tom Becker")', code: '200', ms: 120 }, { ts: ago(149), call: 'click(button.save)', code: '—', ms: null }] },
  'worker-browser-2': { agent_id: 'worker-browser-2', prompt_version: 1, prompt: BROWSER_PROMPT,
    layers: layers('§ Dedupe · § Owner routing', { step: 'crm.create_contact · lead:5 Tom Becker', hist: 'attempt 1 by worker-browser-1: lease expired', schema: '{ status, observation, screenshot }' }), tools: AGENTS[5].tools!,
    recent_calls: [{ ts: ago(30), call: 'search("tom.becker@orbitalfreight.com")', code: '200', ms: 840 }, { ts: ago(28), call: 'fill(form.contact, 5 fields)', code: '200', ms: 310 }, { ts: ago(27), call: 'screenshot.save(stp_51_after.png)', code: '200', ms: 90 }] },
  'worker-drafter': { agent_id: 'worker-drafter', prompt_version: 1, prompt: 'Write one follow-up email per lead using committed facts only.\nFollow the playbook tone rules: warm, specific, under 150 words, one clear ask.\nNever mention facts that are not committed.',
    layers: layers('§ Follow-up policy', { step: 'lead:1 Priya Raman · Northwind · owner a.chen', hist: 'llm.error 503 → fallback', schema: '{ to, subject, body }' }), tools: AGENTS[6].tools!,
    recent_calls: [{ ts: ago(90), call: `openrouter.complete(${MODELS.worker[0]})`, code: '503', ms: 2000 }, { ts: ago(88), call: `openrouter.complete(${MODELS.worker[1]})`, code: '200', ms: 3900 }] },
  'worker-mailer': { agent_id: 'worker-mailer', prompt_version: 1, prompt: '', layers: [], tools: AGENTS[7].tools!,
    recent_calls: [{ ts: ago(10), call: 'approval.check(stp_14)', code: '200', ms: 1 }] },
};

export const AGENT_DESC: Record<string, string> = {
  orchestrator: 'Turns goal + playbook into checkable success criteria and a step graph. Routes by skill, never by worker. Replans after repeated rejections and reaps expired leases every 2s.',
  'meta-reviewer': 'Reviews anything a worker or the verifier flags as ambiguous, and every step the playbook marks as needing approval. It decides automatically when its confidence clears the threshold; only below that does a step go to queue:human.',
  verifier: 'Never trusts a claim. Reads the world through a different channel from the one used to act (REST for the CRM, the API for Mailpit). Deterministic checks run first; the LLM judge only where no hard check exists.',
  'worker-parser': 'Normalises messy attendee files: names, emails, phones, companies. Flags unusable rows with a reason instead of guessing.',
  'worker-browser-1': 'Drives EspoCRM through the browser. Check-then-act: if the postcondition already holds it claims done without acting, so takeovers never duplicate.',
  'worker-browser-2': 'Drives EspoCRM through the browser. Check-then-act: if the postcondition already holds it claims done without acting, so takeovers never duplicate.',
  'worker-drafter': 'Writes one follow-up email per lead from committed facts only. Falls back to the secondary model on an LLM error.',
  'worker-mailer': 'Sends only emails that have a committed approval fact. Deterministic, no model.',
  human: 'You are just another agent on the bus, consulted last. Steps only reach queue:human when the meta-reviewer cannot decide with enough confidence. Your answer is committed as a fact and the waiting lane continues.',
};

export const PLAYBOOK_SECTIONS: { h: string; body: string; used: string }[] = [
  { h: 'Dedupe rules', body: 'Match on email (case-insensitive), then fuzzy name + company ≥ 0.85. Never create a second contact for a match; enrich instead.', used: 'browser, parser' },
  { h: 'Owner routing', body: 'EMEA or companies under 200 people → r.silva. Everyone else → a.chen.', used: 'browser' },
  { h: 'Follow-up policy', body: 'Task due in 2 business days. One email per lead: warm, under 150 words, one clear ask. No automated email to a contact with an open deal; the owner follows up.', used: 'drafter' },
  { h: 'Approval policy', body: 'External emails need an approval fact. The meta-reviewer may approve when the judge scores ≥ 0.90 and no policy flags.', used: 'meta-reviewer' },
  { h: 'Escalation rules', body: 'Ambiguous account matches below the threshold go to a human, batched per run.', used: 'meta-reviewer' },
  { h: 'Definitions of done', body: 'Every usable row is a contact with an owner and a task, verified through the CRM API.', used: 'verifier' },
];

export const budgetFixture = (used: number): LlmBudget => ({
  date: new Date().toISOString().slice(0, 10), used, limit: 45, remaining: Math.max(0, 45 - used), spent_usd: 0, cache_hits: 9, models: MODELS,
});

// ---- report (GET /runs/{id}/report) ---------------------------------------------

const PHASES: ReportPhase[] = [
  { label: 'Understand + plan', agent: 'orchestrator', start_s: 0, end_s: 19 },
  { label: 'Parse file', agent: 'worker-parser', start_s: 19, end_s: 24 },
  { label: 'Search contacts ×11', agent: 'worker-browser-1, -2', start_s: 24, end_s: 92 },
  { label: 'Create / update ×9', agent: 'worker-browser-1, -2', start_s: 40, end_s: 178 },
  { label: 'Follow-up tasks ×9', agent: 'worker-browser-2', start_s: 120, end_s: 236 },
  { label: 'Review ambiguity ×3', agent: 'meta-reviewer', start_s: 178, end_s: 186, kind: 'review' },
  { label: 'Waiting on you', agent: 'human', start_s: 186, end_s: null, kind: 'human' },
  { label: 'Draft emails ×8', agent: 'worker-drafter', start_s: 190, end_s: 244 },
  { label: 'Approve emails', agent: 'meta-reviewer', start_s: 244, end_s: 250, kind: 'review' },
  { label: 'Send emails', agent: 'worker-mailer', start_s: 250, end_s: 268 },
  { label: 'Final verification', agent: 'verifier', start_s: 268, end_s: 286, kind: 'verify' },
];

const COVERAGE: ReportCoverage[] = [
  { check: 'file.parsed_rows', channel: 'source file', runs: 1, pass: 1, reject: 0, p50_ms: 12 },
  { check: 'crm.lookup_matches', channel: 'CRM REST', runs: 11, pass: 11, reject: 0, p50_ms: 70 },
  { check: 'crm.contact_exists', channel: 'CRM REST', runs: 10, pass: 9, reject: 1, p50_ms: 81 },
  { check: 'crm.no_duplicate', channel: 'CRM REST', runs: 9, pass: 9, reject: 0, p50_ms: 77 },
  { check: 'crm.task_exists', channel: 'CRM REST', runs: 9, pass: 9, reject: 0, p50_ms: 91 },
  { check: 'review.decided', channel: 'ledger facts', runs: 4, pass: 4, reject: 0, p50_ms: 3 },
  { check: 'email.draft_valid', channel: 'rules + LLM judge', runs: 8, pass: 8, reject: 0, p50_ms: 1200 },
  { check: 'email.sent', channel: 'Mailpit API', runs: 8, pass: 8, reject: 0, p50_ms: 40 },
  { check: 'run.criteria_met', channel: 'ledger sweep', runs: 1, pass: 1, reject: 0, p50_ms: null },
];

const AGENT_COST: ReportAgentCost[] = [
  { agent: 'Orchestrator', model: MODELS.orchestrator[0], steps: 2, rejections: 0, retries: 0, p50_ms: 1800, tokens: 96_000, cost_usd: 0, requests: 2 },
  { agent: 'Parser', model: MODELS.worker[0], steps: 1, rejections: 0, retries: 0, p50_ms: 2100, tokens: 8_000, cost_usd: 0, requests: 1 },
  { agent: 'Browser operator 1', model: `${MODELS.worker[0]} · lost`, steps: 7, rejections: 0, retries: 0, p50_ms: 3100, tokens: 64_000, cost_usd: 0, requests: 3 },
  { agent: 'Browser operator 2', model: MODELS.worker[0], steps: 12, rejections: 1, retries: 1, p50_ms: 3400, tokens: 88_000, cost_usd: 0, requests: 4 },
  { agent: 'Drafter', model: `${MODELS.worker[0]} → ${MODELS.worker[1]}`, steps: 8, rejections: 0, retries: 0, p50_ms: 4200, tokens: 22_000, cost_usd: 0, requests: 9 },
  { agent: 'Mailer', model: 'no LLM', steps: 8, rejections: 0, retries: 0, p50_ms: 200, tokens: null, cost_usd: null, requests: 0 },
  { agent: 'Meta-reviewer', model: MODELS.meta_reviewer[0], steps: 4, rejections: 0, retries: 0, p50_ms: 2400, tokens: 61_000, cost_usd: 0, requests: 4 },
  { agent: 'Verifier', model: MODELS.verifier[0], steps: 47, rejections: 2, retries: 0, p50_ms: 600, tokens: 73_000, cost_usd: 0, requests: 1 },
];

const DATA_Q: ReportDataQuality[] = [
  { n: 12, label: 'rows in file', example: '4 header names mapped' }, { n: 10, label: 'usable rows', example: '2 flagged with reason' },
  { n: 3, label: 'emails lowercased', example: 'MARCUS.LEE@ACME.COM' }, { n: 7, label: 'phones → E.164', example: '(415) 555-0119 → +14155550119' },
  { n: 5, label: 'fields trimmed', example: 'stray whitespace' }, { n: 1, label: 'duplicate in file', example: 'row 6 = row 3' },
  { n: 1, label: 'no email', example: 'row 11 · phone only' }, { n: 3, label: 'matched existing', example: '2 exact · 1 fuzzy' },
];

const JUDGE = [0.96, 0.95, 0.94, 0.97, 0.95, 0.96, 0.94, 0.95];

export function buildReport(runId: string, samAnswer: string | null, done: boolean, durationS: number): Report {
  const samDecided = samAnswer != null;
  const samSkipped = samAnswer === 'skip';
  const leads = LEADS.map((l) => {
    const base = { n: l.n, name: l.name, company: l.company || '—', email: l.email || l.phone || '—', owner: l.owner === '—' ? null : l.owner, task_due: l.write ? 'in 2 business days' : null, tries: l.special === 'false_claim' || l.special === 'takeover' ? 2 : 1, crm_url: l.write ? `http://localhost:8080/#Contact/view/${(4000 + l.n * 37).toString(16)}` : null, screenshot: l.write ? `${runId}/lead-${l.n}-after.png` : null };
    switch (l.special) {
      case 'dup_in_file': return { ...base, outcome: 'skipped' as const, outcome_detail: 'skipped', decided_by: 'worker-parser', check: 'duplicate of row 3 in the file', email_status: '—', task_due: null, owner: null };
      case 'skip': return { ...base, outcome: 'skipped' as const, outcome_detail: 'skipped', decided_by: 'Meta-reviewer: skip', check: 'no email · § Dedupe (0.88)', email_status: '—', task_due: null, owner: null };
      case 'fuzzy': return { ...base, outcome: 'updated' as const, outcome_detail: 'updated (fuzzy)', decided_by: 'Meta-reviewer: same person', check: 'fuzzy match 0.91 · no_duplicate ✓', email_status: 'none · open deal' };
      case 'escalated':
        if (!samDecided) return { ...base, outcome: 'waiting' as const, outcome_detail: 'waiting on you', decided_by: 'Waiting on you', check: 'confidence 0.52 < 0.80', email_status: 'waiting', owner: null, task_due: null, crm_url: null, screenshot: null };
        if (samSkipped) return { ...base, outcome: 'skipped' as const, outcome_detail: 'skipped', decided_by: 'You: skip', check: 'decided by you', email_status: '—', owner: null, task_due: null, crm_url: null, screenshot: null };
        return { ...base, outcome: 'created' as const, outcome_detail: 'created', decided_by: `You: ${samAnswer === 'acc/31' ? 'Lumen Health' : 'Lumen Inc'}`, check: 'crm.contact_exists ✓', email_status: done ? 'sent' : 'queued' };
      default: {
        const exact = l.write === 'update';
        return { ...base, outcome: (exact ? 'updated' : 'created') as 'updated' | 'created', outcome_detail: `${exact ? 'updated' : 'created'}${base.tries > 1 ? ` · ${base.tries} tries` : ''}`,
          decided_by: l.special === 'takeover' ? 'worker-browser-2 (takeover)' : l.n % 2 ? 'worker-browser-1' : 'worker-browser-2',
          check: l.special === 'false_claim' ? 'rejected once (false claim) → ✓ on attempt 2' : l.special === 'takeover' ? 'lease takeover · no_duplicate ✓' : exact ? 'exact match (email casing) · no_duplicate ✓' : 'crm.contact_exists ✓',
          email_status: 'sent' };
      }
    }
  });
  const cnt = (o: string) => leads.filter((x) => x.outcome === o).length;
  const waiting = cnt('waiting');
  const emails = LEADS.filter((l) => l.email_out && (l.special !== 'escalated' || (samDecided && !samSkipped))).map((l, i) => ({
    to: l.email, subject: `Good to meet you at Signal Summit, ${l.name.split(' ')[0]}`, judge: JUDGE[i % JUDGE.length], approval: 'auto (0.94)', delivery: `sent · mp_${(4810 + i * 7).toString(16)}`,
  }));
  const summary = `12 rows processed: ${cnt('created')} created, ${cnt('updated')} updated, ${cnt('skipped')} skipped with a reason${waiting ? `, ${waiting} waiting on you` : ''}. ${samDecided ? '3 of 4 decisions made automatically, 1 by you' : '3 of 4 decisions made automatically'}. ${emails.length} follow-ups sent.`;
  return {
    run_id: runId,
    status: done ? (waiting ? 'completed_pending_input' : 'completed') : 'running',
    summary,
    goal: GOAL,
    playbook: 'event-leads.md v3 · a91c',
    duration_s: durationS,
    criteria: CRITERIA.map((c, i) => ({ ...c, status: i === 4 ? 'waived' : i === 2 && waiting ? 'pending' : done ? 'verified' : i < 2 ? 'verified' : 'pending',
      evidence: ['crm.contact_exists + crm.no_duplicate via REST, 9 records', 'crm.no_duplicate via REST · 3 matched', 'crm.task_exists via REST', 'assignedUser vs Playbook § Owner routing', 'meta-reviewer decision logged (0.88)', 'approval.auto (judge 0.94) → email.sent via Mailpit API'][i] })),
    leads,
    phases: PHASES.map((p) => (p.kind === "human" ? { ...p, end_s: samDecided ? 262 : null } : p)),
    faults: [
      { at_s: 52, fault: 'false claim', what: 'crm.create_contact (Dana) reported done without submitting.', recovery: 'Verifier found 0 records via REST and rejected; attempt 2 included the reason.', lost_s: 38 },
      { at_s: 90, fault: 'worker died', what: 'Browser operator 1 killed while holding crm.create_contact (Tom).', recovery: 'Lease expired at 15s; operator 2 took over with token 2; no duplicate.', lost_s: 24 },
      { at_s: 150, fault: 'session expired', what: 'CRM login page observed mid-step.', recovery: 'Operator re-authenticated and resumed the same step.', lost_s: 11 },
      { at_s: 214, fault: 'model outage', what: 'Primary worker model returned 503.', recovery: `model.fallback to ${MODELS.worker[1]}; step committed.`, lost_s: 6 },
    ],
    decisions: [
      { title: 'Sam Ito: which Lumen account?', evidence: ['Email domain lumen.io is used by both accounts', 'No prior contact, deal or owner history for sam@lumen.io', 'Event badge lists "Lumen", no division'], confidence: 0.52, threshold: 0.8, result: samDecided ? `You: ${samSkipped ? 'Skip' : samAnswer === 'acc/31' ? 'Lumen Health' : 'Lumen Inc'}` : 'Escalated, waiting on you', decided_by: samDecided ? 'you' : '—', escalated: !samDecided },
      { title: 'Jo Park: phone-only lead', evidence: ['No email in the row', 'No company match in the CRM', 'Playbook § Dedupe: skip phone-only unless strategic'], confidence: 0.88, threshold: 0.8, result: 'Auto: Skip', decided_by: 'meta-reviewer' },
      { title: 'Ben Ortiz: same person as Benjamin Ortiz?', evidence: ['Name similarity 0.86', 'Phone matches +1 415 555 0119', 'Company Quarry Data matches'], confidence: 0.91, threshold: 0.8, result: 'Auto: Same person', decided_by: 'meta-reviewer' },
      { title: '8 follow-up emails ready to send', evidence: ['Merge fields complete, no placeholders', 'Judge min 0.94 across 8 drafts', 'No policy flags (pricing, promises, attachments)'], confidence: 0.94, threshold: 0.9, result: 'Auto: Approve all 8', decided_by: 'meta-reviewer' },
    ],
    coverage: COVERAGE,
    emails,
    agents: AGENT_COST,
    input: { file: 'event_attendees.csv', rows: 12, sha256: '3f9e0c2b77d41a8e5f6a1b9c0d3e2f4a5b6c7d8e9f0a1b2c3d4e5f60718293a1', quality: DATA_Q },
    reproduce: {
      config_hash: '9b1d4c7e2a0f8e6d5c4b3a291807f6e5d4c3b2a1908f7e6d5c4b3a2918070f6e',
      prompts: { orchestrator: 1, 'meta-reviewer': 1, verifier: 1, 'worker-browser-2': 1, 'worker-drafter': 1 },
      models: MODELS,
      faults: ['false_claim', 'kill_worker', 'expire_session', 'model_outage'],
      command: `make replay RUN=${runId}`,
    },
    totals: { tokens: 412_000, cost_usd: 0, requests: 24, duplicates: 0, lost_s: 79 },
  };
}
