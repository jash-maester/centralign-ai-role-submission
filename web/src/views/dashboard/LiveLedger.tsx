/** Live ledger: virtualised, filterable, fault events marked and paired with recoveries. */
import { useEffect, useMemo, useRef, useState } from 'react';
import { useVirtualizer } from '@tanstack/react-virtual';
import { useLedger } from '../../store/store';
import { eventLine, fmtClock, tagEvents } from '../../lib/derive';
import { eventColor, FAULT_TYPES, RECOVERY_TYPES } from '../../lib/states';
import { Count, PillButton } from '../../components/ui';

const GROUPS: [string, string, (t: string) => boolean][] = [
  ['step', 'Steps', (t) => t.startsWith('step.') || t === 'fact.committed'],
  ['review', 'Reviews', (t) => /^(review|approval|input)\./.test(t)],
  ['run', 'Run + plan', (t) => /^(run|plan)\./.test(t)],
  ['fault', 'Faults + recovery', (t) => FAULT_TYPES.has(t) || RECOVERY_TYPES.has(t) || t.startsWith('agent.')],
  ['config', 'Config', (t) => /config|prompt\.|tool\.|shell\./.test(t)],
  ['llm', 'LLM', (t) => t.startsWith('llm.') || t === 'model.fallback' || t === 'spend.cap_reached'],
];

export function actorColor(actor: string): string {
  if (actor === 'chaos') return 'var(--s-rejected)';
  if (actor === 'human') return 'var(--s-input)';
  if (actor === 'verifier') return 'var(--s-committed)';
  if (actor === 'meta-reviewer') return 'var(--s-input)';
  if (actor === 'ledger' || actor === 'orchestrator') return 'var(--fg)';
  return 'var(--fg2)';
}

export function LiveLedger({ height = 360 }: { height?: number }) {
  const events = useLedger((s) => s.events);
  const stream = useLedger((s) => s.stream);
  const openStep = useLedger((s) => s.openStep);
  const [actor, setActor] = useState<string | null>(null);
  const [group, setGroup] = useState<string | null>(null);
  const [follow, setFollow] = useState(true);
  const parentRef = useRef<HTMLDivElement>(null);

  const tags = useMemo(() => tagEvents(events), [events]);
  const actors = useMemo(() => {
    const m = new Map<string, number>();
    events.forEach((e) => m.set(e.actor, (m.get(e.actor) ?? 0) + 1));
    return [...m.entries()].sort((a, b) => b[1] - a[1]).slice(0, 7);
  }, [events]);
  const rows = useMemo(() => {
    const fn = group ? GROUPS.find((g) => g[0] === group)![2] : null;
    return events.map((e, i) => ({ e, tag: tags[i] })).filter(({ e }) => (!actor || e.actor === actor) && (!fn || fn(e.type)));
  }, [events, tags, actor, group]);

  const v = useVirtualizer({ count: rows.length, getScrollElement: () => parentRef.current, estimateSize: () => 22, overscan: 12 });

  useEffect(() => {
    if (follow && rows.length) v.scrollToIndex(rows.length - 1, { align: 'end' });
  }, [rows.length, follow, v]);

  return (
    <div className="bg-panel border border-line rounded-[10px] px-[18px] py-4 flex flex-col gap-2 shadow-card min-w-0" data-testid="live-ledger">
      <div className="flex items-center gap-2 flex-wrap">
        <span className="text-base+ font-medium">Live ledger</span>
        <span className="w-1.5 h-1.5 rounded-full" style={{ background: stream === 'open' ? 'var(--s-committed)' : 'var(--s-claimed)' }} title={stream} />
        <span className="mono text-xs text-fg3">{events.length.toLocaleString()} events · append-only</span>
        <div className="flex-1" />
        <button type="button" onClick={() => setFollow(!follow)} className="h-[22px] px-2 border border-line2 rounded bg-transparent text-fg2 mono text-2xs">
          {follow ? 'pause' : 'follow'}
        </button>
      </div>
      <div className="flex gap-1 flex-wrap">
        <PillButton size="sm" on={!group} onClick={() => setGroup(null)}>All types</PillButton>
        {GROUPS.map(([k, label, fn]) => (
          <PillButton key={k} size="sm" on={group === k} onClick={() => setGroup(group === k ? null : k)}>{label} <Count>{events.filter((e) => fn(e.type)).length}</Count></PillButton>
        ))}
      </div>
      <div className="flex gap-1 flex-wrap">
        <PillButton size="sm" on={!actor} onClick={() => setActor(null)}>All actors</PillButton>
        {actors.map(([a, n]) => (
          <PillButton key={a} size="sm" on={actor === a} onClick={() => setActor(actor === a ? null : a)}>{a} <Count>{n}</Count></PillButton>
        ))}
      </div>
      <div
        ref={parentRef}
        style={{ height }}
        className="overflow-auto -mx-2"
        onWheel={(e) => { if (e.deltaY < 0 && follow) setFollow(false); }}
      >
        {rows.length === 0 ? (
          <div className="px-2 py-3 text-fg3 text-sm">No events yet. They stream in over SSE as the run progresses.</div>
        ) : (
          <div style={{ height: v.getTotalSize(), position: 'relative' }}>
            {v.getVirtualItems().map((vi) => {
              const { e, tag } = rows[vi.index];
              return (
                <div
                  key={vi.key}
                  onClick={() => e.step_id && openStep(e.step_id)}
                  className={`absolute left-0 right-0 grid gap-2 items-center px-2 mono text-xs leading-[22px] ${e.step_id ? 'cursor-pointer hover:bg-panel2' : ''}`}
                  style={{ transform: `translateY(${vi.start}px)`, height: 22, gridTemplateColumns: '58px 112px 160px minmax(0,1fr) 64px', background: tag === 'FAULT' ? 'color-mix(in oklch, var(--s-rejected) 9%, transparent)' : undefined }}
                >
                  <span className="text-fg3">{fmtClock(e.ts)}</span>
                  <span className="truncate" style={{ color: actorColor(e.actor) }}>{e.actor}</span>
                  <span className="truncate" style={{ color: eventColor(e.type) }}>{e.type}</span>
                  <span className="truncate text-fg2" title={eventLine(e)}>{eventLine(e)}</span>
                  <span className="text-right text-[9.5px] tracking-[0.06em]" style={{ color: tag === 'FAULT' ? 'var(--s-rejected)' : 'var(--s-committed)' }}>{tag}</span>
                </div>
              );
            })}
          </div>
        )}
      </div>
    </div>
  );
}
