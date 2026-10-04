/**
 * Server-sent events over fetch, so we can send Last-Event-ID ourselves on the
 * first connect (resume after a reload or a run switch) and receive named
 * events (`event: step.leased`) without registering a listener per type.
 */
import type { StreamHandlers } from './client';
import { asEvent } from './normalize';

export interface SseMessage {
  id: string | null;
  event: string | null;
  data: string;
}

/** Incremental text/event-stream parser (WHATWG rules, minus `retry`). */
export class SseParser {
  private buf = '';
  private id: string | null = null;
  private event: string | null = null;
  private data: string[] = [];
  lastEventId: string | null = null;

  feed(chunk: string): SseMessage[] {
    this.buf += chunk;
    const out: SseMessage[] = [];
    let nl: number;
    while ((nl = this.buf.search(/\r\n|\r|\n/)) >= 0) {
      const line = this.buf.slice(0, nl);
      const sepLen = this.buf.startsWith('\r\n', nl) ? 2 : 1;
      this.buf = this.buf.slice(nl + sepLen);
      if (line === '') {
        if (this.data.length) {
          if (this.id !== null) this.lastEventId = this.id;
          out.push({ id: this.id ?? this.lastEventId, event: this.event, data: this.data.join('\n') });
        }
        this.id = null;
        this.event = null;
        this.data = [];
        continue;
      }
      if (line.startsWith(':')) continue;
      const colon = line.indexOf(':');
      const field = colon < 0 ? line : line.slice(0, colon);
      let value = colon < 0 ? '' : line.slice(colon + 1);
      if (value.startsWith(' ')) value = value.slice(1);
      if (field === 'data') this.data.push(value);
      else if (field === 'event') this.event = value;
      else if (field === 'id') this.id = value;
    }
    return out;
  }
}

export function openEventStream(url: string, lastEventId: string | null, h: StreamHandlers): () => void {
  let closed = false;
  let last = lastEventId;
  let ctrl: AbortController | null = null;
  let attempt = 0;
  let timer: ReturnType<typeof setTimeout> | null = null;

  const connect = async () => {
    if (closed) return;
    ctrl = new AbortController();
    h.onStatus?.(attempt === 0 ? 'connecting' : 'reconnecting');
    try {
      const headers: Record<string, string> = { Accept: 'text/event-stream' };
      if (last) headers['Last-Event-ID'] = last;
      const res = await fetch(url, { headers, signal: ctrl.signal, cache: 'no-store' });
      if (!res.ok || !res.body) throw new Error(`stream ${res.status}`);
      attempt = 0;
      h.onStatus?.('open');
      const parser = new SseParser();
      const reader = res.body.pipeThrough(new TextDecoderStream()).getReader();
      for (;;) {
        const { value, done } = await reader.read();
        if (done) break;
        for (const m of parser.feed(value)) {
          if (m.id) last = m.id;
          let parsed: unknown;
          try { parsed = JSON.parse(m.data); } catch { continue; }
          const ev = asEvent(parsed, m.id);
          if (ev) h.onEvent(ev);
        }
      }
    } catch (err) {
      if (closed || (err as Error).name === 'AbortError') return;
    }
    if (closed) return;
    attempt += 1;
    h.onStatus?.('reconnecting');
    timer = setTimeout(connect, Math.min(10_000, 500 * 2 ** Math.min(attempt, 5)));
  };

  void connect();
  return () => {
    closed = true;
    if (timer) clearTimeout(timer);
    ctrl?.abort();
    h.onStatus?.('closed');
  };
}
