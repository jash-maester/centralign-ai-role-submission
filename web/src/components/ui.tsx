/** Small shared primitives styled after the designs. */
import { useEffect, useState, type CSSProperties, type ReactNode } from 'react';

export function useNow(ms = 1000): number {
  const [now, setNow] = useState(() => Date.now());
  useEffect(() => {
    const t = setInterval(() => setNow(Date.now()), ms);
    return () => clearInterval(t);
  }, [ms]);
  return now;
}

const cv = (c: string) => ({ ['--c' as string]: c }) as CSSProperties;

export function Panel({ children, className = '', style, id }: { children: ReactNode; className?: string; style?: CSSProperties; id?: string }) {
  return (
    <section id={id} style={style} className={`bg-panel border border-line rounded-[10px] shadow-card ${className}`}>
      {children}
    </section>
  );
}

export function Chip({ c, label, dot = 'round', size = 'sm', className = '' }: { c: string; label: ReactNode; dot?: 'round' | 'square' | 'none'; size?: 'sm' | 'md'; className?: string }) {
  return (
    <span
      style={cv(c)}
      className={`tint inline-flex items-center gap-1.5 rounded-[10px] font-medium whitespace-nowrap ${size === 'md' ? 'px-[9px] py-[2px] text-xs+' : 'px-2 py-[2px] text-xs'} ${className}`}
    >
      {dot !== 'none' && <span className={`w-1.5 h-1.5 flex-none ${dot === 'round' ? 'rounded-full' : 'rounded-[2px]'}`} style={{ background: c }} />}
      {label}
    </span>
  );
}

export function Dot({ c, square = false, size = 7, className = '' }: { c: string; square?: boolean; size?: number; className?: string }) {
  return <span className={`flex-none inline-block ${square ? 'rounded-[2px]' : 'rounded-full'} ${className}`} style={{ width: size, height: size, background: c }} />;
}

export function Kicker({ children, className = '' }: { children: ReactNode; className?: string }) {
  return <span className={`mono text-2xs text-fg3 uppercase tracking-[0.06em] ${className}`}>{children}</span>;
}

export function Toggle({ on, onChange, disabled = false, title }: { on: boolean; onChange?: (v: boolean) => void; disabled?: boolean; title?: string }) {
  return (
    <button
      type="button"
      role="switch"
      aria-checked={on}
      title={title}
      disabled={disabled}
      onClick={(e) => {
        e.stopPropagation();
        if (!disabled) onChange?.(!on);
      }}
      className="relative flex-none w-[30px] h-[18px] rounded-[9px] border-0 p-0 disabled:cursor-not-allowed disabled:opacity-50"
      style={{ background: on ? 'var(--s-committed)' : 'var(--line2)' }}
    >
      <span className="absolute top-[2px] w-[14px] h-[14px] rounded-full bg-panel transition-[left] duration-150" style={{ left: on ? 14 : 2 }} />
    </button>
  );
}

export function PillButton({ on, onClick, children, size = 'md' }: { on: boolean; onClick: () => void; children: ReactNode; size?: 'sm' | 'md' }) {
  return (
    <button
      type="button"
      onClick={onClick}
      className={`${size === 'md' ? 'h-7 rounded-[14px] text-sm' : 'h-[26px] rounded-[13px] text-xs+'} px-2.5 border whitespace-nowrap`}
      style={{ background: on ? 'var(--fg)' : 'transparent', color: on ? 'var(--bg)' : 'var(--fg2)', borderColor: on ? 'var(--fg)' : 'var(--line2)' }}
    >
      {children}
    </button>
  );
}

export function Count({ children }: { children: ReactNode }) {
  return <span className="opacity-70 mono text-2xs">{children}</span>;
}

/** Lease TTL ring: drains as the lease ages, refills on heartbeat. */
export function TtlRing({ frac, c = 'var(--s-leased)', size = 14, title }: { frac: number | null; c?: string; size?: number; title?: string }) {
  if (frac == null) return null;
  const r = size / 2 - 1.5;
  const len = 2 * Math.PI * r;
  return (
    <svg width={size} height={size} viewBox={`0 0 ${size} ${size}`} className="flex-none" aria-label={title}>
      {title && <title>{title}</title>}
      <circle cx={size / 2} cy={size / 2} r={r} fill="none" stroke="var(--line2)" strokeWidth={2} />
      <circle
        cx={size / 2}
        cy={size / 2}
        r={r}
        fill="none"
        stroke={c}
        strokeWidth={2}
        strokeDasharray={`${len * frac} ${len}`}
        transform={`rotate(-90 ${size / 2} ${size / 2})`}
        style={{ transition: 'stroke-dasharray 0.9s linear' }}
      />
    </svg>
  );
}

export function Sheet({ open, onClose, width, accent, children }: { open: boolean; onClose: () => void; width: number; accent?: string; children: ReactNode }) {
  useEffect(() => {
    if (!open) return;
    const onKey = (e: KeyboardEvent) => e.key === 'Escape' && onClose();
    window.addEventListener('keydown', onKey);
    return () => window.removeEventListener('keydown', onKey);
  }, [open, onClose]);
  if (!open) return null;
  return (
    <>
      <div data-noprint onClick={onClose} className="fixed inset-0 z-10" style={{ background: 'color-mix(in oklch, var(--bg) 40%, rgba(0,0,0,0.35))' }} />
      <aside
        data-noprint
        className="fixed top-0 right-0 bottom-0 max-w-[94vw] bg-panel border-l border-line z-[11] flex flex-col shadow-card"
        style={{ width, borderTop: accent ? `3px solid ${accent}` : undefined }}
      >
        {children}
      </aside>
    </>
  );
}

export function EscButton({ onClick }: { onClick: () => void }) {
  return (
    <button type="button" onClick={onClick} className="border border-line2 bg-transparent text-fg2 rounded-[5px] px-2.5 py-1">
      Esc
    </button>
  );
}

export function KV({ k, v, kw = 130 }: { k: ReactNode; v: ReactNode; kw?: number }) {
  return (
    <div className="grid gap-3 py-1.5 border-b border-line mono text-xs+" style={{ gridTemplateColumns: `${kw}px minmax(0,1fr)` }}>
      <span className="text-fg3">{k}</span>
      <span className="[overflow-wrap:anywhere]">{v}</span>
    </div>
  );
}

export function Empty({ children }: { children: ReactNode }) {
  return <div className="px-[18px] py-7 text-center text-fg3 text-sm+">{children}</div>;
}
