import { describe, expect, it } from 'vitest';
import type { AgentStatus, LedgerEvent } from '../api/types';
import { buildNodes, defaultWires, eventToWire, pinPos } from '../lib/builderGraph';
import { tagEvents } from '../lib/derive';
import { reportToMarkdown } from '../lib/reportMd';
import { buildReport } from '../api/mock/fixtures';

const ev = (type: string, actor: string, step_id: string | null = null, payload: Record<string, unknown> = {}): LedgerEvent => ({ id: '1', ts: 1, run_id: 'r', step_id, actor, type, payload });
const agents: AgentStatus[] = [
  { id: 'worker-parser', name: 'Parser', role: 'worker', skills: [{ id: 'file.parse', kinds: ['file.parse'] }] },
  { id: 'worker-browser-1', name: 'Browser operator 1', role: 'worker', skills: [{ id: 'browser.espocrm', kinds: ['crm.create_contact'] }] },
  { id: 'worker-mailer', name: 'Mailer', role: 'worker', skills: [{ id: 'email.send', kinds: ['email.send'] }] },
];

describe('builder graph', () => {
  const nodes = buildNodes(agents);
  const wires = defaultWires(nodes);

  it('builds one node per worker around the ledger, with resolvable default wires', () => {
    expect(nodes.map((n) => n.id)).toEqual(expect.arrayContaining(['goal', 'ledger', 'worker-browser-1', 'worker-mailer', 'verifier', 'meta-reviewer', 'human', 'crm']));
    for (const w of wires) {
      const [a, z] = w.split('>');
      expect(pinPos(a, nodes), a).not.toBeNull();
      expect(pinPos(z, nodes), z).not.toBeNull();
    }
    expect(wires).toContain('ledger.q_browser>worker-browser-1.lease');
    expect(wires).toContain('worker-mailer.act>mailpit.smtp');
  });

  it('maps ledger events to the wire their message travels on', () => {
    const skill = (id: string) => ({ stp_41: 'browser.espocrm', stp_95: 'review' } as Record<string, string>)[id];
    const hop = (e: LedgerEvent) => eventToWire(e, skill)?.wire;
    expect(hop(ev('step.leased', 'ledger', 'stp_41', { worker: 'worker-browser-1', fence: 2 }))).toBe('ledger.q_browser>worker-browser-1.lease');
    expect(eventToWire(ev('step.leased', 'ledger', 'stp_41', { worker: 'worker-browser-1', fence: 2 }), skill)?.label).toBe('stp_41 · token 2');
    expect(hop(ev('step.leased', 'ledger', 'stp_95', {}))).toBe('ledger.q_review>meta-reviewer.lease');
    expect(hop(ev('step.claimed', 'worker-browser-1', 'stp_41'))).toBe('worker-browser-1.claim>verifier.claims');
    expect(hop(ev('step.observation', 'worker-browser-1', 'stp_41'))).toBe('worker-browser-1.act>crm.ui');
    expect(hop(ev('step.observation', 'verifier', 'stp_41'))).toBe('verifier.rest>crm.rest');
    expect(hop(ev('step.committed', 'verifier', 'stp_41'))).toBe('verifier.verdict>ledger.verdicts');
    expect(hop(ev('review.escalated', 'meta-reviewer', 'stp_95'))).toBe('ledger.q_human>human.q');
    expect(hop(ev('input.answered', 'human', 'stp_95'))).toBe('human.answer>ledger.answers');
    expect(hop(ev('config.updated', 'api'))).toBeUndefined();
    // every mapped wire exists in the default wiring
    for (const e of [ev('run.created', 'o'), ev('plan.revised', 'o'), ev('fact.committed', 'verifier'), ev('approval.auto', 'meta-reviewer')]) {
      expect(wires).toContain(hop(e));
    }
  });
});

describe('fault pairing and report markdown', () => {
  it('tags each injected fault and the first recovery after it', () => {
    const tags = tagEvents([ev('step.leased', 'l'), ev('fault.injected', 'chaos'), ev('step.claimed', 'w'), ev('step.rejected', 'verifier'), ev('step.rejected', 'verifier')]);
    expect(tags).toEqual(['', 'FAULT', '', 'RECOVERY', '']);
  });

  it('renders every report section to markdown', () => {
    const md = reportToMarkdown(buildReport('run_x', null, true, 286));
    for (const h of ['# Evidence report · run_x', '## Success criteria', '## Every row in the file', '## Decisions', '## Verification coverage', '## Follow-up emails', '## What went wrong and how it recovered', '## Agents and cost', '## Input file', '## Reproduce this run']) {
      expect(md).toContain(h);
    }
    expect(md).toContain('| 9 | Sam Ito |');
  });
});
