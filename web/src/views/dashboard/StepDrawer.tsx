/** Step drawer (H8): inputs, postcondition text + JSON, every attempt with verdict + screenshots. */
import { useEffect, useMemo, useState } from 'react';
import { api } from '../../api';
import type { Attempt, Step } from '../../api/types';
import { useLedger } from '../../store/store';
import { buildLanes, fmtClock, shortModel } from '../../lib/derive';
import { stepVisual } from '../../lib/states';
import { Chip, EscButton, KV, Kicker, Sheet } from '../../components/ui';

/** Readable postconditions per registered check (plans/01 §8). */
export const POSTCONDITION_TEXT: Record<string, string> = {
  'file.parsed_rows': 'Normalised rows recorded; row count and required columns match the source file; unusable rows flagged with a reason.',
  'crm.lookup_matches': 'The claimed match (or no match) agrees with a REST query against the CRM.',
  'crm.contact_exists': 'Contact exists in the CRM with this email exactly once, with the expected fields and routed owner (REST).',
  'crm.no_duplicate': 'Exactly one contact per normalised email; existing contact has the new fields (REST).',
  'crm.task_exists': 'Follow-up task linked to the contact, due per playbook (2 business days), owner per routing (REST).',
  'email.draft_valid': 'Recipient and merge fields valid, no placeholders; then the LLM judge checks playbook tone rules.',
  'email.sent': 'Mailpit shows exactly one message to this recipient, sent after an approval fact.',
  'review.decided': 'A decision fact exists: the meta-reviewer at or above the threshold, or a human answer.',
  'run.criteria_met': 'Every success criterion is verified or waived.',
};

const isBrowserKind = (k: string) => k.startsWith('crm.');

function obsText(a: Attempt): string {
  const obs = a.observations ?? [];
  if (!obs.length) return '—';
  return obs.map((o) => String(o.summary ?? o.message ?? JSON.stringify(o))).join(' · ');
}

function AttemptCard({ a, step, isLast }: { a: Attempt; step: Step; isLast: boolean }) {
  const evidenceUrl = api().evidenceUrl;
  const expired = a.outcome === 'lease_expired';
  let verdict: string, vc: string;
  if (a.verdict) {
    verdict = a.verdict.ok ? (step.status === 'committed' || a.outcome === 'committed' ? 'verified · committed' : 'verified') : 'rejected';
    vc = a.verdict.ok ? 'var(--s-committed)' : 'var(--s-rejected)';
  } else if (expired) {
    verdict = 'lease expired';
    vc = 'var(--s-dead)';
  } else if (isLast && step.status === 'claimed_done') {
    verdict = 'verifying…';
    vc = 'var(--s-claimed)';
  } else if (isLast && step.status === 'input_required') {
    verdict = 'escalated';
    vc = 'var(--s-input)';
  } else {
    verdict = 'pending';
    vc = 'var(--s-leased)';
  }
  const reason = a.verdict?.reason || (expired ? `Token ${a.fence ?? '?'} fenced out; any late write is rejected.` : isLast && step.status === 'claimed_done' ? 'Verifier is reading the world through its own channel' : isLast && step.status === 'leased' ? 'Worker holds the lease' : '—');
  const shots = a.claim?.evidence ?? [];
  return (
    <div className="border border-line rounded-lg px-3.5 py-3 flex flex-col gap-2.5" data-testid="attempt">
      <div className="flex justify-between gap-2.5 mono text-xs+">
        <span>attempt {a.attempt} · <span style={{ textDecoration: expired ? 'line-through' : 'none' }}>{a.worker ?? '—'}</span>{a.fence ? <span className="text-fg3"> · token {a.fence}</span> : null}</span>
        <span className="text-fg3">{a.model ? shortModel(a.model) : '— (no LLM)'}{a.started_at ? ` · ${fmtClock(a.started_at)}` : ''}</span>
      </div>
      <div className="grid grid-cols-[96px_minmax(0,1fr)] gap-x-3 gap-y-1.5 text-sm+ leading-[1.45]">
        <span className="mono text-xs text-fg3">observation</span><span className="text-fg2">{obsText(a)}</span>
        <span className="mono text-xs text-fg3">claim</span><span style={{ color: 'var(--s-claimed)' }}>{a.claim ? `${a.claim.acted === false ? 'already done (check-then-act) · ' : ''}"${a.claim.summary || 'done'}"` : expired ? '— (heartbeat lost)' : '— (in progress)'}</span>
        <span className="mono text-xs text-fg3">verdict</span><span className="font-medium" style={{ color: vc }}>{verdict}{a.verdict?.check ? <span className="mono text-xs text-fg3 font-normal"> · {a.verdict.check}</span> : null}</span>
        <span className="mono text-xs text-fg3">reason</span><span className="text-fg2">{reason}</span>
      </div>
      {(shots.length > 0 || (isBrowserKind(step.kind) && a.claim)) && (
        <div className="grid grid-cols-2 gap-2">
          {(shots.length ? shots.slice(0, 2) : ['before.png', 'after.png']).map((p, i) => (
            <a key={i} href={shots.length ? evidenceUrl(p) : undefined} target="_blank" rel="noreferrer" className="block">
              {shots.length ? (
                <img src={evidenceUrl(p)} alt={p} className="w-full aspect-[16/10] object-cover rounded-md border border-line bg-panel2" />
              ) : (
                <div className="aspect-[16/10] rounded-md border border-line grid place-items-center mono text-2xs text-fg3" style={{ background: 'repeating-linear-gradient(135deg, var(--panel2) 0 5px, var(--bg2) 5px 10px)' }}>{p}</div>
              )}
              <span className="mono text-2xs text-fg3">{i === 0 ? 'before' : 'after'} · {p.split('/').pop()}</span>
            </a>
          ))}
        </div>
      )}
    </div>
  );
}

export function StepDrawer() {
  const id = useLedger((s) => s.openStepId);
  const runId = useLedger((s) => s.runId);
  const live = useLedger((s) => (id ? s.steps[id] : undefined));
  const steps = useLedger((s) => s.steps);
  const order = useLedger((s) => s.stepOrder);
  const facts = useLedger((s) => s.facts);
  const openStep = useLedger((s) => s.openStep);
  const [detail, setDetail] = useState<Step | null>(null);

  useEffect(() => {
    setDetail(null);
    if (!id || !runId) return;
    let alive = true;
    api().getStep(runId, id).then((s) => alive && setDetail(s)).catch(() => undefined);
    return () => { alive = false; };
  }, [id, runId, live?.updated_at, live?.status]);

  const lane = useMemo(() => (live?.lane ? buildLanes(steps, order, facts).find((l) => l.lane === live.lane) : undefined), [live?.lane, steps, order, facts]);
  const step = detail && live && (detail.updated_at ?? 0) >= (live.updated_at ?? 0) ? detail : live ?? detail;
  const close = () => openStep(null);
  if (!id) return null;
  const vis = step ? stepVisual(step.status, { skipped: step.status === 'replanned' }) : stepVisual('planned');
  const hist = step?.history ?? [];
  const pc = step?.postcondition;
  const leadLabel = lane ? `${lane.lead.name}${lane.lead.company ? ` · ${lane.lead.company}` : ''}` : step?.lane ?? 'run';
  return (
    <Sheet open onClose={close} width={600} accent={vis.c}>
      <div className="px-[22px] pt-[18px] pb-4 border-b border-line flex justify-between items-start gap-3" data-testid="step-drawer">
        <div className="flex flex-col gap-[5px] min-w-0">
          <span className="mono text-xs text-fg3 truncate">{id} · {leadLabel}</span>
          <span className="mono text-xl">{step?.kind ?? '…'}</span>
          <span className="self-start"><Chip c={vis.c} label={step?.status === 'claimed_done' ? 'claimed · verifying' : vis.label} dot="square" size="md" /></span>
        </div>
        <EscButton onClick={close} />
      </div>
      <div className="flex-1 min-h-0 overflow-auto px-[22px] pt-5 pb-8 flex flex-col gap-[22px]">
        {!step ? (
          <span className="text-fg3">Loading step…</span>
        ) : (
          <>
            <div className="flex flex-col">
              <Kicker className="pb-1.5">Inputs (from committed facts)</Kicker>
              {Object.entries(step.inputs ?? {}).map(([k, v]) => <KV key={k} k={k} v={typeof v === 'string' ? v : JSON.stringify(v)} />)}
              <KV k="skill" v={step.skill} />
              {step.idempotency_key && <KV k="idempotency_key" v={step.idempotency_key} />}
              {step.depends_on && step.depends_on.length > 0 && <KV k="depends_on" v={step.depends_on.join(', ')} />}
              <KV k="attempts" v={`${step.attempt} of ${step.max_attempts ?? 3} · fence ${step.fence ?? 0}${step.side_effect ? ' · side effect' : ''}`} />
            </div>
            <div className="flex flex-col gap-2">
              <Kicker>Postcondition</Kicker>
              <span className="text-base leading-normal text-pretty">{(pc && POSTCONDITION_TEXT[pc.check]) ?? 'Registered check.'}</span>
              <pre className="m-0 px-3 py-2.5 rounded-lg bg-panel2 border border-line mono text-xs text-fg2 whitespace-pre-wrap">{JSON.stringify(pc ?? {}, null, 2)}</pre>
            </div>
            <div className="flex flex-col gap-2.5">
              <Kicker>Attempts</Kicker>
              {hist.length === 0 && (
                <span className="text-sm+ text-fg3">
                  {step.status === 'ready' ? 'Ready. Waiting for a worker to lease it.' : step.status === 'input_required' ? 'Escalated: waiting on your decision in Needs your attention.' : step.status === 'review_required' ? 'With the meta-reviewer.' : step.status === 'replanned' ? 'Superseded by a replan or a skip decision.' : 'Not started. Waiting on earlier steps.'}
                </span>
              )}
              {hist.map((a, i) => <AttemptCard key={`${a.attempt}-${i}`} a={a} step={step} isLast={i === hist.length - 1} />)}
            </div>
          </>
        )}
      </div>
    </Sheet>
  );
}
