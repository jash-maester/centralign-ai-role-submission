import { describe, expect, it } from 'vitest';
import type { LedgerEvent, Run, Step } from '../api/types';
import { emptySlice, reduceEvent, type RunSlice } from '../store/reduce';

const run: Run = { id: 'run_1', goal: 'g', status: 'running', criteria: [], created_at: 0 };
const step = (id: string, extra: Partial<Step> = {}): Step => ({
  id, run_id: 'run_1', kind: 'crm.create_contact', skill: 'browser.espocrm', postcondition: { check: 'crm.contact_exists' },
  status: 'ready', attempt: 0, fence: 0, history: [], lane: 'lead:4', ...extra,
});
const ev = (type: string, step_id: string | null, payload: Record<string, unknown> = {}, actor = 'ledger', ts = 1000): LedgerEvent =>
  ({ id: `${ts}-0`, ts, run_id: 'run_1', step_id, actor, type, payload });

const slice = (...steps: Step[]): RunSlice => ({ ...emptySlice(), run, steps: Object.fromEntries(steps.map((s) => [s.id, s])), stepOrder: steps.map((s) => s.id) });

describe('reduceEvent: step state mapping', () => {
  it('walks lease -> claim -> commit and records the attempt', () => {
    let s = slice(step('stp_41'));
    s = reduceEvent(s, ev('step.leased', 'stp_41', { worker: 'worker-browser-2', fence: 1 })).slice;
    expect(s.steps.stp_41).toMatchObject({ status: 'leased', lease_owner: 'worker-browser-2', attempt: 1, fence: 1 });
    s = reduceEvent(s, ev('step.observation', 'stp_41', { summary: 'form saved' }, 'worker-browser-2')).slice;
    s = reduceEvent(s, ev('step.claimed', 'stp_41', { summary: 'created' }, 'worker-browser-2')).slice;
    expect(s.steps.stp_41.status).toBe('claimed_done');
    expect(s.steps.stp_41.claim?.summary).toBe('created');
    s = reduceEvent(s, ev('step.committed', 'stp_41', { check: 'crm.contact_exists', reason: 'ok' }, 'verifier')).slice;
    const st = s.steps.stp_41;
    expect(st.status).toBe('committed');
    expect(st.lease_owner).toBeNull();
    expect(st.history).toHaveLength(1);
    expect(st.history![0]).toMatchObject({ attempt: 1, worker: 'worker-browser-2', outcome: 'committed' });
    expect(st.history![0].observations).toHaveLength(1);
    expect(st.history![0].verdict?.ok).toBe(true);
  });

  it('a rejection then retry keeps both attempts (false claim)', () => {
    let s = slice(step('stp_4'));
    s = reduceEvent(s, ev('step.leased', 'stp_4', { worker: 'b2' })).slice;
    s = reduceEvent(s, ev('step.claimed', 'stp_4', {}, 'b2')).slice;
    s = reduceEvent(s, ev('step.rejected', 'stp_4', { reason: 'REST: 0 contacts' }, 'verifier')).slice;
    expect(s.steps.stp_4.status).toBe('rejected');
    expect(s.steps.stp_4.verdict).toMatchObject({ ok: false, reason: 'REST: 0 contacts' });
    s = reduceEvent(s, ev('step.ready', 'stp_4')).slice;
    s = reduceEvent(s, ev('step.leased', 'stp_4', { worker: 'b2' })).slice;
    expect(s.steps.stp_4.attempt).toBe(2);
    expect(s.steps.stp_4.history!.map((a) => a.outcome)).toEqual(['rejected', undefined]);
  });

  it('lease expiry then takeover bumps the fence', () => {
    let s = slice(step('stp_5'));
    s = reduceEvent(s, ev('step.leased', 'stp_5', { worker: 'b1', fence: 1 })).slice;
    s = reduceEvent(s, ev('step.lease_expired', 'stp_5', {}, 'orchestrator')).slice;
    expect(s.steps.stp_5).toMatchObject({ status: 'lease_expired', lease_owner: null });
    s = reduceEvent(s, ev('step.ready', 'stp_5')).slice;
    s = reduceEvent(s, ev('step.leased', 'stp_5', { worker: 'b2', fence: 2 })).slice;
    expect(s.steps.stp_5).toMatchObject({ status: 'leased', lease_owner: 'b2', fence: 2, attempt: 2 });
    expect(s.steps.stp_5.history!.map((a) => [a.worker, a.outcome])).toEqual([['b1', 'lease_expired'], ['b2', undefined]]);
  });

  it('review escalation -> input_required -> answered -> ready, and asks for escalations', () => {
    let s = slice(step('stp_95', { kind: 'review.ambiguity', skill: 'review', status: 'review_required' }));
    const r1 = reduceEvent(s, ev('review.escalated', 'stp_95', { confidence: 0.52 }, 'meta-reviewer'));
    expect(r1.slice.steps.stp_95.status).toBe('input_required');
    expect(r1.refetch.escalations).toBe(true);
    s = reduceEvent(r1.slice, ev('input.answered', 'stp_95', { answer: 'acc/12' }, 'human')).slice;
    expect(s.steps.stp_95.status).toBe('ready');
  });

  it('an explicit payload.status wins over the type mapping', () => {
    const s = reduceEvent(slice(step('x')), ev('step.observation', 'x', { status: 'dead' })).slice;
    expect(s.steps.x.status).toBe('dead');
  });

  it('unknown steps ask for a refetch instead of guessing', () => {
    const r = reduceEvent(slice(), ev('step.leased', 'stp_new'));
    expect(r.refetch.steps).toBe(true);
    expect(r.slice.steps).toEqual({});
  });

  it('plan events upsert step bodies or ask for a refetch', () => {
    const r = reduceEvent(slice(), ev('plan.revised', null, { steps: [step('a'), step('b')] }));
    expect(r.slice.stepOrder).toEqual(['a', 'b']);
    expect(reduceEvent(slice(), ev('plan.revised', null, { summary: 'x' })).refetch.steps).toBe(true);
  });

  it('run status and committed facts', () => {
    let s = slice();
    s = reduceEvent(s, ev('fact.committed', 'stp_1', { key: 'lead:1', value: { name: 'Priya' } }, 'verifier')).slice;
    s = reduceEvent(s, ev('fact.committed', 'stp_1', { key: 'lead:1', value: { name: 'Priya Raman' } }, 'verifier')).slice;
    expect(s.facts).toEqual([{ key: 'lead:1', value: { name: 'Priya Raman' }, source_step: 'stp_1', committed_at: 1000 }]);
    s = reduceEvent(s, ev('run.completed_pending_input', null, {}, 'orchestrator', 5000)).slice;
    expect(s.run).toMatchObject({ status: 'completed_pending_input', finished_at: 5000 });
  });
});
