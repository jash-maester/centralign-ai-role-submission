import { describe, expect, it } from 'vitest';
import { buildRunScript, samLaneBeats, toEvent } from '../api/mock/script';
import { emptySlice, reduceEvent } from '../store/reduce';
import { EVENT_TYPES } from '../api/types';

// The mock replays real event types and must leave the step graph in the
// state plans/02 "Demo data" describes when played through the store reducer.
describe('mock demo run script', () => {
  const { beats } = buildRunScript('run_t');
  const play = (bs: typeof beats, from = emptySlice()) => {
    let s = { ...from, run: from.run ?? { id: 'run_t', goal: 'g', status: 'created' as const, criteria: [], created_at: 0 } };
    bs.forEach((b, i) => { s = reduceEvent(s, toEvent('run_t', b, 1000 + i, i)).slice; });
    return s;
  };

  it('uses only contract event types', () => {
    for (const b of beats) expect(EVENT_TYPES as readonly string[]).toContain(b.type);
  });

  it('ends with every lane done except Sam Ito, who waits on a human', () => {
    const s = play(beats);
    expect(s.run?.status).toBe('completed_pending_input');
    const open = Object.values(s.steps).filter((x) => !['committed', 'replanned'].includes(x.status)).map((x) => `${x.id}:${x.status}`);
    expect(open.sort()).toEqual(['stp_91:planned', 'stp_92:planned', 'stp_93:planned', 'stp_94:planned', 'stp_95:input_required', 'stp_99:ready']);
    expect(s.steps.stp_41.history?.map((a) => a.outcome)).toEqual(['rejected', 'committed']);
    expect(s.steps.stp_51.history?.map((a) => [a.worker, a.outcome])).toEqual([['worker-browser-1', 'lease_expired'], ['worker-browser-2', 'committed']]);
    expect(s.facts.find((f) => f.key === 'lead:9')).toBeTruthy();
  });

  it('answering the escalation completes the run', () => {
    const s = play(samLaneBeats('acc/12', 'Lumen Inc'), play(beats));
    expect(s.run?.status).toBe('completed');
    expect(Object.values(s.steps).every((x) => ['committed', 'replanned'].includes(x.status))).toBe(true);
  });
});
