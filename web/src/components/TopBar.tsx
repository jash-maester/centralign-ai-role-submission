import { IS_MOCK } from '../api';
import { useLedger, type View } from '../store/store';

export function Logo() {
  return (
    <div className="flex items-center gap-[9px]">
      <div className="w-3 h-3 border-2 border-fg rotate-45" />
      <span className="mono font-medium tracking-[0.04em]">ledger</span>
    </div>
  );
}

export function NavTabs() {
  const view = useLedger((s) => s.view);
  const setView = useLedger((s) => s.setView);
  const tabs: [View, string][] = [['dashboard', 'Dashboard'], ['report', 'Report'], ['builder', 'Builder']];
  return (
    <nav className="flex p-[3px] rounded-lg bg-bg2 gap-0.5">
      {tabs.map(([k, label]) => (
        <button
          key={k}
          type="button"
          data-testid={`nav-${k}`}
          onClick={() => setView(k)}
          className="px-3 py-[5px] border-0 rounded-md text-sm+ font-medium"
          style={{ background: view === k ? 'var(--panel)' : 'transparent', boxShadow: view === k ? 'var(--shadow)' : 'none', color: view === k ? 'var(--fg)' : 'var(--fg2)' }}
        >
          {label}
        </button>
      ))}
    </nav>
  );
}

export function NeedsBadge({ onClick }: { onClick: () => void }) {
  const n = useLedger((s) => s.escalations.length);
  if (!n) return null;
  return (
    <button
      type="button"
      onClick={onClick}
      data-testid="needs-badge"
      className="h-8 px-3 border rounded-md text-sm+ font-medium flex items-center gap-[7px] whitespace-nowrap"
      style={{ borderColor: 'var(--s-input)', background: 'color-mix(in oklch, var(--s-input) 10%, transparent)', color: 'var(--s-input)' }}
    >
      <span className="w-[7px] h-[7px] rounded-full" style={{ background: 'var(--s-input)' }} />
      Needs you · {n}
    </button>
  );
}

export function ThemeButton() {
  const theme = useLedger((s) => s.theme);
  const toggle = useLedger((s) => s.toggleTheme);
  return (
    <button type="button" onClick={toggle} className="h-8 px-3 border border-line2 rounded-md bg-transparent text-fg2 text-sm+">
      {theme === 'light' ? 'Dark' : 'Light'}
    </button>
  );
}

export function StreamDot() {
  const stream = useLedger((s) => s.stream);
  const runId = useLedger((s) => s.runId);
  if (!runId) return null;
  const c = stream === 'open' ? 'var(--s-committed)' : stream === 'closed' ? 'var(--fg3)' : 'var(--s-claimed)';
  const label = stream === 'open' ? 'live' : stream;
  return (
    <span className="mono text-xs text-fg3 flex items-center gap-1.5" title={`SSE /stream?run_id=${runId}`}>
      <span className="w-1.5 h-1.5 rounded-full" style={{ background: c }} />
      {label}
      {IS_MOCK && <span className="ml-1 px-1.5 rounded border border-line2 text-2xs">mock data</span>}
    </span>
  );
}

export function TopBar({ onNeeds }: { onNeeds: () => void }) {
  return (
    <header data-noprint className="h-14 flex-none flex items-center gap-4 px-6 border-b border-line bg-panel sticky top-0 z-[5]">
      <Logo />
      <NavTabs />
      <div className="flex-1" />
      <StreamDot />
      <NeedsBadge onClick={onNeeds} />
      <ThemeButton />
    </header>
  );
}
