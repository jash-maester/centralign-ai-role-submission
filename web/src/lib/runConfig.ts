import type { RunConfig } from '../api/types';

/** Defaults from ledger_core/config.py (also the playbook front matter defaults). */
export const DEFAULT_RUN_CONFIG: RunConfig = {
  review_auto_threshold: 0.8,
  approval_auto_threshold: 0.9,
  always_ask_human_email: false,
  llm_judge_enabled: true,
  lease_ttl_s: 15,
  max_attempts: 3,
  check_then_act: true,
  replan_after_rejections: 2,
  model_fallback: true,
  determinism: 0.8,
  seed: 42,
  seed_pinned: true,
  browser_concurrency: 2,
  crm_write_path: 'browser',
  spend_cap_usd: 2.0,
  fuzzy_match_threshold: 0.85,
  dry_run: false,
};

/** Temperature multipliers per role at determinism 0 (plans/01 §6). */
export const ROLE_TEMPERATURE_SCALE = { orchestrator: 0.8, worker: 1.0, meta_reviewer: 0.5, verifier: 0 } as const;

export function temperature(role: keyof typeof ROLE_TEMPERATURE_SCALE, determinism: number): number {
  return Math.round(ROLE_TEMPERATURE_SCALE[role] * (1 - determinism) * 10000) / 10000;
}

export function heartbeatS(c: RunConfig): number {
  return c.lease_ttl_s / 3;
}

/** Keys that change through POST /runs/{id}/determinism rather than PUT config. */
export const DETERMINISM_KEYS: (keyof RunConfig)[] = ['determinism', 'seed', 'seed_pinned'];

export function configDiff(a: RunConfig, b: RunConfig): Partial<RunConfig> {
  const out: Partial<RunConfig> = {};
  (Object.keys(b) as (keyof RunConfig)[]).forEach((k) => {
    if (a[k] !== b[k]) (out as Record<string, unknown>)[k] = b[k];
  });
  return out;
}
