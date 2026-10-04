/**
 * Builder canvas graph (web/design/Ledger Canvas.dc.html): agents around the
 * ledger, typed pins, default wiring, and the mapping from a real ledger event
 * to the wire its message travels on (the packet animation).
 */
import type { AgentStatus, LedgerEvent, Skill } from '../api/types';

export type PinType = 'exec' | 'task' | 'claim' | 'fact' | 'human' | 'io';
export interface Pin { id: string; label: string; t: PinType }
export type Role = 'input' | 'orch' | 'ledger' | 'worker' | 'verifier' | 'human' | 'meta' | 'ext';
export interface GNode { id: string; role: Role; title: string; x: number; y: number; w: number; ins: Pin[]; outs: Pin[]; extra?: boolean }

export const PIN_COLOR: Record<PinType, string> = {
  exec: 'var(--fg)', task: 'var(--s-leased)', claim: 'var(--s-claimed)', fact: 'var(--s-committed)', human: 'var(--s-input)', io: 'var(--s-ready)',
};
export const ROLE_STYLE: Record<Role, { c: string; label: string }> = {
  input: { c: 'var(--fg2)', label: 'input' },
  orch: { c: 'var(--fg)', label: 'orchestrator' },
  ledger: { c: 'var(--fg)', label: 'redis · source of truth' },
  worker: { c: 'var(--s-leased)', label: 'worker' },
  verifier: { c: 'var(--s-committed)', label: 'verifier' },
  human: { c: 'var(--s-input)', label: 'agent · human' },
  meta: { c: 'var(--s-input)', label: 'meta-reviewer' },
  ext: { c: 'var(--fg3)', label: 'external' },
};

const P = (id: string, label: string, t: PinType): Pin => ({ id, label, t });
const WORKER_PINS: Record<string, { outs: Pin[] }> = {
  browser: { outs: [P('claim', 'claim', 'claim'), P('act', 'act · UI', 'io')] },
  mailer: { outs: [P('claim', 'claim', 'claim'), P('act', 'act · SMTP', 'io')] },
  plain: { outs: [P('claim', 'claim', 'claim')] },
};

/** Which worker family an agent belongs to, by its skills or id. */
export function workerKind(a: Pick<AgentStatus, 'id' | 'skills'>): 'browser' | 'mailer' | 'plain' {
  const skills = (a.skills ?? []).map((s) => s.id);
  if (skills.includes('browser.espocrm') || skills.includes('api.espocrm') || a.id.includes('browser')) return 'browser';
  if (skills.includes('email.send') || a.id.includes('mailer')) return 'mailer';
  return 'plain';
}

export const QUEUE_PIN: Record<string, string> = {
  'file.parse': 'q_parse', 'browser.espocrm': 'q_browser', 'api.espocrm': 'q_browser', 'email.draft': 'q_draft', 'email.send': 'q_send', review: 'q_review', human: 'q_human',
};

const DEFAULT_WORKERS = ['worker-parser', 'worker-browser-1', 'worker-browser-2', 'worker-drafter', 'worker-mailer'];

/** Build the canvas nodes from GET /agents (falls back to the design's agent set). */
export function buildNodes(agents: AgentStatus[]): GNode[] {
  const workers = agents.filter((a) => a.role === 'worker');
  const list: Pick<AgentStatus, 'id' | 'name' | 'skills'>[] = workers.length ? workers : DEFAULT_WORKERS.map((id) => ({ id, name: id.replace('worker-', ''), skills: [] }));
  const nodes: GNode[] = [
    { id: 'goal', role: 'input', title: 'Goal', x: 0, y: 200, w: 230, ins: [], outs: [P('goal', 'goal', 'exec')] },
    { id: 'playbook', role: 'input', title: 'Playbook', x: 0, y: 360, w: 230, ins: [], outs: [P('pb', 'sections', 'exec')] },
    { id: 'orchestrator', role: 'orch', title: 'Orchestrator', x: 290, y: 250, w: 230, ins: [P('goal', 'goal', 'exec'), P('playbook', 'playbook', 'exec'), P('facts', 'facts', 'fact')], outs: [P('tasks', 'task.submit', 'task')] },
    { id: 'ledger', role: 'ledger', title: 'Ledger', x: 580, y: 150, w: 290,
      ins: [P('tasks', 'task.submit', 'task'), P('verdicts', 'commit/reject', 'fact'), P('answers', 'input.answered', 'human')],
      outs: [P('q_parse', 'queue:file.parse', 'task'), P('q_browser', 'queue:browser.espocrm', 'task'), P('q_draft', 'queue:email.draft', 'task'), P('q_send', 'queue:email.send', 'task'), P('q_review', 'queue:review', 'task'), P('q_human', 'queue:human', 'human'), P('facts', 'facts', 'fact')] },
  ];
  let y = 0;
  for (const a of list) {
    const kind = workerKind(a);
    const outs = WORKER_PINS[kind].outs;
    nodes.push({ id: a.id, role: 'worker', title: a.name, x: 940, y, w: 230, ins: [P('lease', 'lease', 'task')], outs });
    y += 1 + 36 + 12 + Math.max(outs.length, 1) * 24 + 31 + 24;
  }
  nodes.push({ id: 'human', role: 'human', title: 'Human (you)', x: 940, y, w: 230, ins: [P('q', 'review.escalated', 'human')], outs: [P('answer', 'answer', 'human')] });
  nodes.push(
    { id: 'verifier', role: 'verifier', title: 'Verifier', x: 1250, y: 230, w: 250, ins: [P('claims', 'claims', 'claim')], outs: [P('verdict', 'commit/reject', 'fact'), P('rest', 'read · REST', 'io'), P('mail', 'read · Mailpit', 'io')] },
    { id: 'meta-reviewer', role: 'meta', title: 'Meta-reviewer', x: 1250, y: 450, w: 250, ins: [P('lease', 'queue:review', 'task')], outs: [P('decision', 'decide / escalate', 'fact'), P('rest', 'read · REST', 'io')] },
    { id: 'crm', role: 'ext', title: 'EspoCRM', x: 1580, y: 150, w: 190, ins: [P('ui', 'browser UI', 'io'), P('rest', 'REST API', 'io')], outs: [] },
    { id: 'mailpit', role: 'ext', title: 'Mailpit', x: 1580, y: 470, w: 190, ins: [P('smtp', 'SMTP', 'io'), P('api', 'API', 'io')], outs: [] },
  );
  return nodes;
}

/** Default wiring. Wire id = "node.pin>node.pin". */
export function defaultWires(nodes: GNode[]): string[] {
  const w = [
    'goal.goal>orchestrator.goal', 'playbook.pb>orchestrator.playbook', 'orchestrator.tasks>ledger.tasks', 'ledger.facts>orchestrator.facts',
    'ledger.q_human>human.q', 'human.answer>ledger.answers', 'verifier.verdict>ledger.verdicts', 'verifier.rest>crm.rest', 'verifier.mail>mailpit.api',
    'ledger.q_review>meta-reviewer.lease', 'meta-reviewer.decision>ledger.verdicts', 'meta-reviewer.rest>crm.rest',
  ];
  for (const n of nodes.filter((x) => x.role === 'worker')) {
    const kind = n.outs.some((p) => p.label === 'act · UI') ? 'browser' : n.outs.some((p) => p.label === 'act · SMTP') ? 'mailer' : n.id.includes('draft') ? 'draft' : 'parse';
    const q = kind === 'browser' ? 'q_browser' : kind === 'mailer' ? 'q_send' : kind === 'draft' ? 'q_draft' : 'q_parse';
    w.push(`ledger.${q}>${n.id}.lease`, `${n.id}.claim>verifier.claims`);
    if (kind === 'browser') w.push(`${n.id}.act>crm.ui`);
    if (kind === 'mailer') w.push(`${n.id}.act>mailpit.smtp`);
  }
  return w;
}

// ---- geometry (must match the node template: 1px border + 36px header + 6px padding, 24px rows) ----

export const pinY = (n: { y: number }, i: number) => n.y + 1 + 36 + 6 + i * 24 + 12;
export const nodeH = (n: GNode) => 1 + 36 + 12 + Math.max(n.ins.length, n.outs.length, 1) * 24 + 31;

export function pinPos(ref: string, nodes: GNode[]): { x: number; y: number; t: PinType } | null {
  const dot = ref.lastIndexOf('.');
  const nid = ref.slice(0, dot), pid = ref.slice(dot + 1);
  const n = nodes.find((x) => x.id === nid);
  if (!n) return null;
  let i = n.outs.findIndex((p) => p.id === pid);
  if (i >= 0) return { x: n.x + n.w, y: pinY(n, i), t: n.outs[i].t };
  i = n.ins.findIndex((p) => p.id === pid);
  return i >= 0 ? { x: n.x, y: pinY(n, i), t: n.ins[i].t } : null;
}

export const wireEnds = (w: string): [string, string] => w.split('>') as [string, string];
export const nodeOf = (ref: string) => ref.slice(0, ref.lastIndexOf('.'));

/** Cubic bezier wire; backward wires get wider handles so they loop cleanly. */
export function wirePath(a: { x: number; y: number }, z: { x: number; y: number }): string {
  const back = z.x < a.x + 40;
  const dx = back ? Math.max(160, (a.x - z.x) * 0.22) : Math.max(50, (z.x - a.x) * 0.45);
  return `M ${a.x} ${a.y} C ${a.x + dx} ${a.y}, ${z.x - dx} ${z.y}, ${z.x} ${z.y}`;
}

// ---- events -> packets ---------------------------------------------------------

const short = (id?: string | null) => id ?? '';

/**
 * The wire a ledger event's message travels on, or null when it does not map
 * to a hop on the canvas. `skillOf` resolves a step's skill (routing is by skill).
 */
export function eventToWire(ev: LedgerEvent, skillOf: (stepId: string) => Skill | string | undefined): { wire: string; label: string } | null {
  const p = ev.payload ?? {};
  const worker = String(p.worker ?? p.agent_id ?? ev.actor);
  const sid = short(ev.step_id);
  switch (ev.type) {
    case 'run.created': return { wire: 'goal.goal>orchestrator.goal', label: 'goal' };
    case 'run.understood': return { wire: 'playbook.pb>orchestrator.playbook', label: 'playbook' };
    case 'plan.created':
    case 'plan.revised': return { wire: 'orchestrator.tasks>ledger.tasks', label: Array.isArray(p.steps) ? `${p.steps.length} × task.submit` : ev.type };
    case 'step.leased': {
      const skill = ev.step_id ? skillOf(ev.step_id) : undefined;
      const q = skill ? QUEUE_PIN[skill] : undefined;
      if (!q) return null;
      const target = q === 'q_review' ? 'meta-reviewer.lease' : q === 'q_human' ? 'human.q' : `${worker}.lease`;
      return { wire: `ledger.${q}>${target}`, label: `${sid}${p.fence && Number(p.fence) > 1 ? ` · token ${p.fence}` : ''}` };
    }
    case 'step.observation': {
      if (ev.actor === 'verifier') return { wire: 'verifier.rest>crm.rest', label: 'GET · REST' };
      if (ev.actor === 'meta-reviewer') return { wire: 'meta-reviewer.rest>crm.rest', label: 'read-only lookup' };
      if (ev.actor.includes('mailer')) return { wire: `${ev.actor}.act>mailpit.smtp`, label: 'SMTP' };
      if (ev.actor.includes('browser')) return { wire: `${ev.actor}.act>crm.ui`, label: String(p.summary ?? 'act').slice(0, 28) };
      return null;
    }
    case 'step.claimed': return { wire: `${ev.actor}.claim>verifier.claims`, label: `claim · ${sid}` };
    case 'step.committed': return { wire: 'verifier.verdict>ledger.verdicts', label: `commit ${sid}` };
    case 'step.rejected': return { wire: 'verifier.verdict>ledger.verdicts', label: `reject ${sid}` };
    case 'fact.committed': return { wire: 'ledger.facts>orchestrator.facts', label: `fact · ${String(p.key ?? '')}` };
    case 'review.resolved': return { wire: 'meta-reviewer.decision>ledger.verdicts', label: `decided ${typeof p.confidence === 'number' ? p.confidence.toFixed(2) : ''}`.trim() };
    case 'approval.auto': return { wire: 'meta-reviewer.decision>ledger.verdicts', label: 'auto-approve' };
    case 'review.escalated':
    case 'input.requested': return { wire: 'ledger.q_human>human.q', label: 'escalation' };
    case 'input.answered': return { wire: 'human.answer>ledger.answers', label: 'answer' };
    default: return null;
  }
}
