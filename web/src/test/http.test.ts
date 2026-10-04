import { afterEach, describe, expect, it, vi } from 'vitest';
import { EVENTS_PAGE, HttpClient } from '../api/http';

const ev = (i: number) => ({ id: `${i}-0`, ts: i, run_id: 'run_1', step_id: null, actor: 'a', type: 'step.ready', payload: {} });

afterEach(() => vi.unstubAllGlobals());

describe('HttpClient.getEvents', () => {
  it('never asks for more than the API page cap and follows `next`', async () => {
    const urls: string[] = [];
    const total = 2300;
    vi.stubGlobal('fetch', async (url: string) => {
      urls.push(url);
      const q = new URL(url, 'http://x').searchParams;
      const limit = Number(q.get('limit'));
      const start = q.get('after') ? Number(q.get('after')!.split('-')[0]) + 1 : 0;
      const events = Array.from({ length: Math.max(0, Math.min(limit, total - start)) }, (_, k) => ev(start + k));
      const next = start + events.length < total && events.length ? events[events.length - 1].id : null;
      return new Response(JSON.stringify({ events, next }), { status: 200 });
    });
    const out = await new HttpClient('/api').getEvents('run_1', { limit: 5000 });
    expect(out).toHaveLength(total);
    expect(urls.every((u) => Number(new URL(u, 'http://x').searchParams.get('limit')) <= EVENTS_PAGE)).toBe(true);
    expect(urls).toHaveLength(3);
  });
});
