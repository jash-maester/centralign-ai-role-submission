/** Fake sandboxed shell for the mock adapter (the design's command set). */
import type { ShellConnection } from '../client';
import { AGENTS, MODELS } from './fixtures';

const DIM = '\x1b[90m', ERR = '\x1b[31m', RESET = '\x1b[0m';

export class MockShell implements ShellConnection {
  private dataCbs: ((d: string) => void)[] = [];
  private closeCbs: ((r: string) => void)[] = [];
  private line = '';
  private prompt: string;
  private timer: ReturnType<typeof setInterval> | null = null;

  constructor(private agentId: string, private alive: boolean) {
    this.prompt = `\x1b[32mledger@${agentId}:/app$${RESET} `;
    setTimeout(() => {
      const a = AGENTS.find((x) => x.id === agentId);
      if (!alive) {
        this.out(`${ERR}Error response from daemon: container ${a?.container ?? agentId} is not running${RESET}\r\n${DIM}exit code 137 (SIGKILL) · restart policy: on-failure${RESET}\r\n`);
        return;
      }
      this.out(`${DIM}attached to ${a?.container ?? agentId} (ledger image) · sandboxed: non-root, no host mounts, compose network only${RESET}\r\n${DIM}type "help" for commands${RESET}\r\n${this.prompt}`);
    }, 50);
  }

  private out(s: string) {
    this.dataCbs.forEach((cb) => cb(s));
  }

  onData(cb: (d: string) => void) { this.dataCbs.push(cb); }
  onClose(cb: (r: string) => void) { this.closeCbs.push(cb); }
  close() {
    if (this.timer) clearInterval(this.timer);
    this.closeCbs.forEach((cb) => cb('closed'));
  }

  send(data: string) {
    if (!this.alive) return;
    if (data.startsWith('{')) return; // resize message
    for (const ch of data) {
      if (ch === '\r') {
        this.out('\r\n');
        this.run(this.line.trim());
        this.line = '';
      } else if (ch === '\x7f') {
        if (this.line) { this.line = this.line.slice(0, -1); this.out('\b \b'); }
      } else if (ch === '\x03') {
        if (this.timer) { clearInterval(this.timer); this.timer = null; }
        this.line = '';
        this.out(`^C\r\n${this.prompt}`);
      } else if (ch >= ' ') {
        this.line += ch;
        this.out(ch);
      }
    }
  }

  private run(cmd: string) {
    const [c0, ...args] = cmd.split(/\s+/);
    const a = AGENTS.find((x) => x.id === this.agentId);
    const lines: string[] = [];
    if (!cmd) { /* empty */ }
    else if (c0 === 'help') lines.push('help  ps  env  ls  tail  tools  whoami  clear' + (this.agentId === 'ledger' ? '  redis-cli <cmd>' : ''));
    else if (c0 === 'clear') { this.out('\x1b[2J\x1b[H' + this.prompt); return; }
    else if (c0 === 'whoami') lines.push('ledger (uid 1000)');
    else if (c0 === 'ps') lines.push(`${DIM}PID  CMD${RESET}`, `1    python -m ledger_core.services.${this.agentId.replace(/-/g, '_')}`, '14   heartbeat --every 5s', ...(this.agentId.startsWith('worker-browser') ? ['22   chromium --headless'] : []));
    else if (c0 === 'env') lines.push(`AGENT_ID=${this.agentId}`, 'REDIS_URL=redis://redis:6379/0', 'OPENROUTER_API_KEY=sk-or-••••••••', `MODEL_${(a?.model_role ?? 'worker').toUpperCase()}=${(MODELS[a?.model_role ?? 'worker'] ?? []).join(',')}`);
    else if (c0 === 'ls') lines.push(this.agentId.startsWith('worker-browser') ? 'browser_worker/  evidence/  storage_state.json' : 'ledger_core/  prompts/  playbooks/  pyproject.toml');
    else if (c0 === 'tools') (a?.tools ?? []).forEach((t) => lines.push(`${t.type.toUpperCase().padEnd(8)} ${t.name.padEnd(22)} ${t.detail ?? ''}`));
    else if (c0 === 'tail') {
      const pool = ['heartbeat ok', 'lease loop: waiting on queue', 'claim written', 'lease released'];
      this.out(`${DIM}following logs · ctrl-c to stop${RESET}\r\n`);
      this.timer = setInterval(() => this.out(`${DIM}${new Date().toTimeString().slice(0, 8)} ${pool[Math.floor(Math.random() * pool.length)]}${RESET}\r\n`), 1200);
      return;
    } else if (c0 === 'redis-cli') lines.push(args[0]?.toUpperCase() === 'PING' ? 'PONG' : 'OK');
    else lines.push(`${ERR}sh: ${c0}: command not found${RESET}`);
    this.out(lines.map((l) => l + '\r\n').join('') + this.prompt);
  }
}
