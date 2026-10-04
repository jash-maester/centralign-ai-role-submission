import { describe, expect, it } from 'vitest';
import type { AgentStatus, LedgerEvent, Step } from '../api/types';
import { agentView, autoAt, buildLanes, coreActivity, counters, decisions, handledAutomatically, progressCounts } from '../lib/derive';
import { stepVisual } from '../lib/states';
import { temperature } from '../lib/runConfig';

const st = (id: string, kind: Step['kind'], status: Step['status'], lane = 'lead:4', extra: Partial<Step> = {}): Step =>
  ({ id, run_id: 'r', kind, skill: 'browser.espocrm', postcondition: { check: 'c' }, status, attempt: 1, lane, ...extra });
const ev = (type: string, payload: Record<string, unknown> = {}, step_id: string | null = null, actor = 'meta-reviewer', ts = 1): LedgerEvent =>
  ({ id: String(ts), ts, run_id: 'r', step_id, actor, type, payload });

describe('state mapping', () => {
  it('a claim stays amber until the verifier commits', () => {
    expect(stepVisual('claimed_done')).toMatchObject({ v: 'claimed', label: 'verifying' });
    expect(stepVisual('verified').v).toBe('claimed');
    expect(stepVisual('committed').v).toBe('committed');
    expect(stepVisual('input_required')).toMatchObject({ v: 'input', label: 'escalated' });
    expect(stepVisual('lease_expired').v).toBe('dead');
    expect(stepVisual('replanned', { skipped: true }).label).toBe('skipped');
  });

  it('derived temperatures follow plans/01 §6 (verifier always 0)', () => {
    expect(temperature('worker', 0.8)).toBeCloseTo(0.2);
    expect(temperature('orchestrator', 0.8)).toBeCloseTo(0.16);
    expect(temperature('meta_reviewer', 0)).toBe(0.5);
    expect(temperature('verifier', 0)).toBe(0);
  });
});

describe('lanes', () => {
  it('groups by lane, picks the open review for the search cell, and names leads from facts', () => {
    const steps = [
      st('s1', 'crm.search_contact', 'committed', 'lead:9'),
      st('r1', 'review.ambiguity', 'input_required', 'lead:9'),
      st('w1', 'crm.create_contact', 'planned', 'lead:9'),
      st('s2', 'crm.search_contact', 'committed', 'lead:2'),
      st('w2', 'crm.update_contact', 'leased', 'lead:2', { lease_owner: 'worker-browser-2', attempt: 2 }),
      st('p', 'file.parse', 'committed', null as unknown as string),
    ];
    const map = Object.fromEntries(steps.map((s) => [s.id, s]));
    const lanes = buildLanes(map, steps.map((s) => s.id), [{ key: 'lead:9', value: { name: 'Sam Ito', company: 'Lumen', email: 'sam@lumen.io' }, source_step: 'p' }]);
    expect(lanes.map((l) => l.lane)).toEqual(['lead:2', 'lead:9']);
    const sam = lanes[1];
    expect(sam.lead).toMatchObject({ n: 9, name: 'Sam Ito', company: 'Lumen' });
    expect(sam.cells.search.label).toBe('escalated');
    expect(sam.category).toBe('input');
    expect(sam.holder).toBe('human');
    expect(sam.cells.draft.label).toBe('—');
    const marcus = lanes[0];
    expect(marcus.cells.write.label).toBe('leased');
    expect(marcus.tries).toBe(2);
    expect(marcus.holder).toBe('worker-browser-2');
    expect(marcus.category).toBe('active');
  });

  it('progress counts by visual state', () => {
    const m = { a: st('a', 'crm.create_task', 'committed'), b: st('b', 'crm.create_task', 'claimed_done'), c: st('c', 'crm.create_task', 'committed') };
    expect(progressCounts(m).map((x) => [x.v, x.n])).toEqual([['committed', 2], ['claimed', 1]]);
  });
});

describe('events -> decisions, counters, audit trail', () => {
  const events = [
    ev('review.resolved', { title: 'Ben Ortiz matched', confidence: 0.91, threshold: 0.8, label: 'Same person', evidence: ['phone'] }, 'stp_75'),
    ev('review.escalated', { title: 'Sam Ito: which Lumen account?', confidence: 0.52, threshold: 0.8 }, 'stp_95', 'meta-reviewer', 2),
    ev('approval.auto', { title: '8 emails approved', confidence: 0.94, threshold: 0.9 }, 'stp_90', 'meta-reviewer', 3),
    ev('step.rejected', {}, 'stp_41', 'verifier', 4),
    ev('step.lease_expired', { worker: 'worker-browser-1' }, 'stp_51', 'orchestrator', 5),
    ev('model.fallback', { role: 'worker', from: 'a', to: 'b' }, 'stp_13', 'worker-drafter', 6),
    ev('input.answered', { answer: 'acc/12', label: 'Lumen Inc' }, 'stp_95', 'human', 7),
  ];

  it('tracks each review step to its latest outcome', () => {
    const ds = decisions(events, []);
    expect(ds.map((d) => [d.key, d.kind, d.by, d.escalated])).toEqual([
      ['stp_75', 'ambiguity', 'meta-reviewer', false],
      ['stp_95', 'ambiguity', 'you', false],
      ['stp_90', 'approval', 'meta-reviewer', false],
    ]);
    expect(ds[1].choice).toBe('Lumen Inc');
  });

  it('live threshold hints', () => {
    const ds = decisions(events.slice(0, 3), []);
    expect(autoAt(ds, { review_auto_threshold: 0.8, approval_auto_threshold: 0.9, always_ask_human_email: false, llm_judge_enabled: true })).toMatchObject({ ambTotal: 2, ambAuto: 1, apprAuto: 1, minJudge: 0.94 });
    expect(autoAt(ds, { review_auto_threshold: 0.95, approval_auto_threshold: 0.9, always_ask_human_email: true, llm_judge_enabled: true })).toMatchObject({ ambAuto: 0, apprAuto: 0, apprForced: true });
  });

  it('counts retries, takeovers, fallbacks', () => {
    expect(counters(events)).toMatchObject({ retries: 1, takeovers: 1, fallbacks: 1 });
  });

  it('handled automatically lists auto decisions and recoveries, newest first', () => {
    const items = handledAutomatically(events);
    expect(items.map((i) => i.title)).toEqual(['Model fallback for worker', 'Lease on stp_51 expired, work taken over', '8 emails approved', 'Ben Ortiz matched']);
  });
});

describe('agent view', () => {
  const base: AgentStatus = { id: 'worker-browser-2', name: 'Browser operator 2', role: 'worker', models: ['qwen/q:free', 'google/g:free'] };
  it('marks a lost agent and reads the lease ring', () => {
    const now = 100_000;
    expect(agentView({ ...base, alive: false, last_heartbeat_ms: now - 92_000 }, now, now, 15, 0)).toMatchObject({ state: 'lost', stale: true, hbAge: 92 });
    const v = agentView({ ...base, alive: true, last_heartbeat_ms: now - 1000, current_step: 'stp_41', lease_ttl_ms: 9000, lease_total_ms: 15000, lease_fence: 2 }, now + 1500, now, 15, 0);
    expect(v.state).toBe('active');
    expect(v.step).toBe('stp_41 · t2');
    expect(v.ttlFrac).toBeCloseTo(0.5);
    expect(agentView({ ...base, model: 'google/g:free' }, now, now, 15, 0).model).toBe('google/g:free (fallback)');
  });
});

describe('core agent activity', () => {
  it('verifier shows the claim it is checking; meta-reviewer its escalation', () => {
    const now = 1000;
    const vv = agentView({ id: 'verifier', name: 'Verifier', role: 'verifier', alive: true, last_heartbeat_ms: now }, now, now, 15, 0);
    const steps = [st('stp_22', 'crm.create_task', 'claimed_done', 'lead:2', { postcondition: { check: 'crm.task_exists' } })];
    expect(coreActivity(vv, { role: 'verifier' }, steps, [], null)).toMatchObject({ state: 'verifying', step: 'stp_22 crm.task_exists' });
    const mv = agentView({ id: 'meta-reviewer', name: 'M', role: 'meta_reviewer', alive: true, last_heartbeat_ms: now }, now, now, 15, 1);
    const esc = [{ id: 'e', run_id: 'r', step_id: 'stp_95', lane: 'lead:9', question: 'q', options: [], confidence: 0.5, threshold: 0.8 }];
    expect(coreActivity(mv, { role: 'meta_reviewer' }, [], esc, null).step).toBe('escalated lead:9 → human');
  });
});
