/** Live sandboxed shell (H11): xterm.js over WS /agents/{id}/exec. Always dark, like the design. */
import { useEffect, useRef, useState } from 'react';
import { Terminal } from '@xterm/xterm';
import { FitAddon } from '@xterm/addon-fit';
import '@xterm/xterm/css/xterm.css';
import { api } from '../../api';
import type { AgentStatus } from '../../api/types';

export default function ShellTab({ agent }: { agent: AgentStatus }) {
  const host = useRef<HTMLDivElement>(null);
  const [state, setState] = useState<'connecting' | 'open' | 'closed'>('connecting');
  const [nonce, setNonce] = useState(0);
  const dead = agent.alive === false;

  useEffect(() => {
    if (!host.current) return;
    const term = new Terminal({
      fontFamily: "'IBM Plex Mono', ui-monospace, monospace",
      fontSize: 12,
      lineHeight: 1.35,
      cursorBlink: true,
      convertEol: false,
      theme: { background: '#0E0F11', foreground: '#C9CBCF', cursor: '#E7E8EA', black: '#16171A', green: '#7FD1A1', red: '#F08A7E', brightBlack: '#6E737C' },
    });
    const fit = new FitAddon();
    term.loadAddon(fit);
    term.open(host.current);
    try { fit.fit(); } catch { /* hidden */ }
    if (dead) term.write(`\x1b[31mheartbeat stale: ${agent.container ?? agent.id} may not be running\x1b[0m\r\n`);
    const conn = api().openShell(agent.id);
    setState('connecting');
    conn.onData((d) => { setState('open'); term.write(d); });
    conn.onClose((r) => { setState('closed'); term.write(`\r\n\x1b[90m[session closed: ${r}]\x1b[0m\r\n`); });
    const sub = term.onData((d) => conn.send(d));
    const ro = new ResizeObserver(() => {
      try {
        fit.fit();
        conn.resize?.(term.cols, term.rows);
      } catch { /* not visible */ }
    });
    ro.observe(host.current);
    term.focus();
    return () => {
      ro.disconnect();
      sub.dispose();
      conn.close();
      term.dispose();
    };
  }, [agent.id, agent.container, dead, nonce]);

  const dot = state === 'open' && !dead ? 'var(--s-committed)' : state === 'connecting' ? 'var(--s-claimed)' : 'var(--s-dead)';
  return (
    <>
      <div className="rounded-[10px] overflow-hidden border flex flex-col min-h-[420px]" style={{ borderColor: '#24262B', background: '#0E0F11' }} data-testid="shell">
        <div className="flex items-center gap-3 px-3.5 py-[9px] mono text-xs" style={{ background: '#16171A', borderBottom: '1px solid #24262B', color: '#A3A7AE' }}>
          <span className="w-[7px] h-[7px] rounded-full" style={{ background: dot }} />
          <span style={{ color: '#E7E8EA' }}>{agent.container ?? agent.id}</span>
          <span>{state}</span>
          <span className="flex-1" />
          <button type="button" onClick={() => setNonce((n) => n + 1)} className="px-2 py-0.5 rounded mono text-2xs" style={{ border: '1px solid #2E3036', color: '#E7E8EA', background: 'transparent' }}>reconnect</button>
        </div>
        <div ref={host} className="flex-1 min-h-[380px] px-2 py-2" />
      </div>
      <span className="text-xs+ text-fg3">
        Sandboxed <span className="mono">docker exec</span>: non-root, no host mounts, no Docker socket, compose network only. Opening a shell emits <span className="mono">shell.opened</span>.
      </span>
    </>
  );
}
