/** Client-side markdown for the evidence report (used when the API sends JSON only). */
import type { Report } from '../api/types';

const dur = (s: number | null | undefined) => (s == null ? '—' : `${Math.floor(s / 60)}:${String(Math.round(s % 60)).padStart(2, '0')}`);
const cell = (x: unknown) => String(x ?? '—').replace(/\|/g, '\\|').replace(/\n/g, ' ');
const table = (head: string[], rows: unknown[][]) =>
  [`| ${head.join(' | ')} |`, `| ${head.map(() => '---').join(' | ')} |`, ...rows.map((r) => `| ${r.map(cell).join(' | ')} |`)].join('\n');

export function reportToMarkdown(r: Report): string {
  const out: string[] = [];
  out.push(`# Evidence report · ${r.run_id}`, '', `**Status:** ${r.status} · **Duration:** ${dur(r.duration_s)}`, '', r.summary, '', `**Goal:** ${r.goal}${r.playbook ? `  \n**Playbook:** ${r.playbook}` : ''}`);
  out.push('', '## Success criteria', '', table(['Verdict', 'Criterion', 'How verified'], r.criteria.map((c) => [c.status, c.text, c.evidence ?? c.check])));
  out.push('', '## Every row in the file', '', table(['#', 'Lead', 'Company', 'Outcome', 'Owner', 'Task', 'Email', 'Decided / verified', 'Check', 'CRM'],
    r.leads.map((l) => [l.n, l.name, l.company, l.outcome_detail ?? l.outcome, l.owner, l.task_due, l.email_status, l.decided_by, l.check, l.crm_url])));
  out.push('', '## Decisions', '', table(['Decision', 'Confidence', 'Threshold', 'Result', 'By', 'Evidence'],
    r.decisions.map((d) => [d.title, d.confidence?.toFixed(2) ?? '—', d.threshold?.toFixed(2) ?? '—', d.result, d.decided_by, d.evidence.join('; ')])));
  out.push('', '## Verification coverage', '', table(['Check', 'Channel', 'Runs', 'Pass', 'Reject', 'p50'], r.coverage.map((c) => [c.check, c.channel, c.runs, c.pass, c.reject, c.p50_ms != null ? `${c.p50_ms}ms` : '—'])));
  out.push('', '## Follow-up emails', '', table(['Recipient', 'Subject', 'Judge', 'Approval', 'Delivery'], r.emails.map((e) => [e.to, e.subject, e.judge?.toFixed(2), e.approval, e.delivery])));
  out.push('', '## What went wrong and how it recovered', '', table(['At', 'Fault', 'What happened', 'Recovery', 'Time lost'], r.faults.map((f) => [dur(f.at_s), f.fault, f.what, f.recovery, dur(f.lost_s)])));
  out.push('', '## Agents and cost', '', table(['Agent', 'Model', 'Steps', 'Rejections', 'Retries', 'p50', 'Tokens', 'Cost', 'Requests'],
    r.agents.map((a) => [a.agent, a.model, a.steps, a.rejections, a.retries, a.p50_ms != null ? `${a.p50_ms}ms` : '—', a.tokens, a.cost_usd != null ? `$${a.cost_usd.toFixed(2)}` : '—', a.requests])));
  if (r.input) out.push('', '## Input file', '', `${r.input.file ?? '—'} · ${r.input.rows ?? '—'} rows · sha256 ${r.input.sha256 ?? '—'}`, '', ...(r.input.quality ?? []).map((q) => `- ${q.n} ${q.label}${q.example ? ` (${q.example})` : ''}`));
  if (r.reproduce) {
    out.push('', '## Reproduce this run', '');
    if (r.reproduce.config_hash) out.push(`- config_hash: \`${r.reproduce.config_hash}\``);
    if (r.reproduce.config) out.push(`- config: \`${JSON.stringify(r.reproduce.config)}\``);
    if (r.reproduce.prompts) out.push(`- prompts: ${Object.entries(r.reproduce.prompts).map(([k, v]) => `${k} v${v}`).join(', ')}`);
    if (r.reproduce.models) out.push(`- models: ${Object.entries(r.reproduce.models).map(([k, v]) => `${k} ${v.join(' → ')}`).join('; ')}`);
    out.push('', '```', `$ ${r.reproduce.command ?? `make replay RUN=${r.run_id}`}`, '```');
  }
  return out.join('\n') + '\n';
}
