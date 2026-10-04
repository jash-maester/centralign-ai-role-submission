import { describe, expect, it } from 'vitest';
import schema from '../api/protocol.schema.json';
import { EVENT_TYPES, RUN_STATUSES, SKILLS, STEP_KINDS, STEP_STATUSES } from '../api/types';
import { DEFAULT_RUN_CONFIG } from '../lib/runConfig';
import { EVENT_STEP_STATUS } from '../store/reduce';

// The TS enums must match the generated contract (make schema).
describe('protocol contract', () => {
  it('enums match protocol.schema.json', () => {
    expect([...STEP_STATUSES]).toEqual(schema.enums.StepStatus);
    expect([...RUN_STATUSES]).toEqual(schema.enums.RunStatus);
    expect([...SKILLS]).toEqual(schema.enums.Skill);
    expect([...STEP_KINDS]).toEqual(schema.enums.StepKind);
    expect([...EVENT_TYPES]).toEqual(schema.enums.EventType);
  });

  it('RunConfig defaults match the schema defaults', () => {
    const props = (schema.models.RunConfig as { properties: Record<string, { default?: unknown }> }).properties;
    expect(Object.keys(DEFAULT_RUN_CONFIG).sort()).toEqual(Object.keys(props).sort());
    for (const [k, v] of Object.entries(props)) expect(DEFAULT_RUN_CONFIG[k as keyof typeof DEFAULT_RUN_CONFIG]).toEqual(v.default);
  });

  it('every event-implied step transition is legal from some state', () => {
    const legal = schema.legal_transitions as Record<string, string[]>;
    const reachable = new Set(Object.values(legal).flat());
    for (const target of Object.values(EVENT_STEP_STATUS)) expect(reachable.has(target!)).toBe(true);
  });
});
