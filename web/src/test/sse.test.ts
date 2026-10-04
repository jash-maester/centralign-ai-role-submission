import { describe, expect, it } from 'vitest';
import { SseParser } from '../api/sse';
import { asEvent, asFacts, asList, asRunConfig } from '../api/normalize';

describe('SseParser', () => {
  it('parses named events, ids and multi-line data across chunk boundaries', () => {
    const p = new SseParser();
    const a = p.feed('id: 1-0\nevent: step.leased\ndata: {"type":"step.le');
    expect(a).toEqual([]);
    const b = p.feed('ased","actor":"ledger"}\n\n: keepalive\n\ndata: line1\r\ndata: line2\r\n\r\n');
    expect(b).toEqual([
      { id: '1-0', event: 'step.leased', data: '{"type":"step.leased","actor":"ledger"}' },
      { id: '1-0', event: null, data: 'line1\nline2' },
    ]);
    expect(p.lastEventId).toBe('1-0');
  });

  it('builds a ledger event, falling back to the SSE id', () => {
    const e = asEvent({ type: 'step.claimed', actor: 'w', ts: 5, payload: { a: 1 } }, '9-1');
    expect(e).toMatchObject({ id: '9-1', type: 'step.claimed', actor: 'w', ts: 5, payload: { a: 1 }, step_id: null });
    expect(asEvent({ nope: true })).toBeNull();
  });
});

describe('lenient envelopes', () => {
  it('accepts bare lists and wrappers', () => {
    expect(asList([1, 2])).toEqual([1, 2]);
    expect(asList({ runs: [1] }, 'runs')).toEqual([1]);
    expect(asList({ items: [3] })).toEqual([3]);
    expect(asList(null)).toEqual([]);
  });
  it('accepts a facts hash with JSON strings', () => {
    const f = asFacts({ 'lead:1': JSON.stringify({ value: { name: 'P' }, source_step: 's1' }), x: 3 });
    expect(f).toEqual([{ key: 'lead:1', value: { name: 'P' }, source_step: 's1', committed_at: undefined }, { key: 'x', value: 3, source_step: '' }]);
  });
  it('accepts RunConfig bare or wrapped and fills defaults', () => {
    expect(asRunConfig({ lease_ttl_s: 30, config_hash: 'h' })).toMatchObject({ config: { lease_ttl_s: 30, max_attempts: 3 }, config_hash: 'h' });
    expect(asRunConfig({ config: { dry_run: true } }).config.dry_run).toBe(true);
  });
});
