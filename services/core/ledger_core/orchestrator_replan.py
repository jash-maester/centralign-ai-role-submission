"""Replanning (B5, plans/01 §5 recovery layer 2). Deterministic, no LLM.

Triggers, per CRM step of a lane:
- the step is `dead` (max attempts used up), and no newer step of the same
  kind exists in the lane yet;
- the step was rejected `replan_after_rejections` times (RunConfig, 0 = never)
  and is now `rejected` (held by `reject_policy` below) or back in `ready`.

Action: if the route has another skill (RunConfig.crm_write_path "auto":
browser.espocrm -> api.espocrm; or the route was changed mid-run), the
remaining steps of the lane from the failing step on (those planned, ready or
rejected) become `replanned` and are re-created on the new skill with fresh
attempts; dependencies are remapped. One plan.revised event lists the steps
and `replaces` {old id: new id}. With no other skill, a held step is retried
(ready) until max_attempts, then dead; a dead step fails its lane and its
planned dependents are marked dead with the reason.

`reject_policy` is the verifier hook (Verifier(reject_policy=...)): it holds a
step in `rejected` for the orchestrator when a replan is due, and otherwise
behaves like the default policy (ready, or dead after max_attempts).
"""

from __future__ import annotations

from typing import Any

import redis.asyncio as aioredis

from . import ledger
from .config import RunConfig
from .keys import Keys
from .orchestrator_lanes import group_lanes, next_skill
from .protocol import CRM_KINDS, EventType, Step, StepStatus, Verdict

S = StepStatus
REPLANNABLE = frozenset({S.PLANNED, S.READY, S.REJECTED})


def replan_due(step: Step, cfg: RunConfig, *, pending_rejection: bool = False) -> bool:
    n = cfg.replan_after_rejections
    if n <= 0 or step.kind not in CRM_KINDS:
        return False
    return ledger.rejection_count(step) + (1 if pending_rejection else 0) >= n


async def reject_policy(step: Step, verdict: Verdict, cfg: RunConfig) -> StepStatus | None:
    """Verifier hook: hold for the orchestrator (None) when a replan to another
    skill is due, else retry (ready) or dead after max_attempts."""
    if replan_due(step, cfg, pending_rejection=True) and next_skill(step.kind, step.skill, cfg) is not None:
        return None
    if ledger.rejection_count(step) + 1 >= step.max_attempts:
        return S.DEAD
    return S.READY


def _dependents_closure(root: Step, lane_steps: list[Step]) -> list[Step]:
    """root and every lane step that (transitively) depends on it, in creation order."""
    ids = {root.id}
    changed = True
    while changed:
        changed = False
        for s in lane_steps:
            if s.id not in ids and any(d in ids for d in s.depends_on):
                ids.add(s.id)
                changed = True
    return [s for s in lane_steps if s.id in ids]


def _superseded(step: Step, lane_steps: list[Step]) -> bool:
    """A newer step of the same kind exists in the lane (already replanned)."""
    return any(s.kind == step.kind and s.created_at > step.created_at and s.id != step.id for s in lane_steps)


async def replan_run(r: aioredis.Redis, keys: Keys, run_id: str, steps: list[Step], cfg: RunConfig, *,
                     actor: str = "orchestrator") -> list[dict[str, Any]]:
    """Apply B5 to every lane of the run. Returns one record per revision."""
    revisions: list[dict[str, Any]] = []
    for lane_steps in group_lanes(steps).values():
        for s in lane_steps:
            if s.kind not in CRM_KINDS or _superseded(s, lane_steps):
                continue
            if s.status == S.DEAD:
                trigger = f"{s.kind.value} dead after {s.attempt} attempt(s)"
            elif s.status in (S.REJECTED, S.READY) and replan_due(s, cfg):
                trigger = f"{s.kind.value} rejected {ledger.rejection_count(s)} time(s)"
            else:
                continue
            target = next_skill(s.kind, s.skill, cfg)
            if target is None:
                await _no_alternative(r, keys, s, lane_steps, cfg, actor=actor)
                continue
            rev = await _move_lane(r, keys, s, lane_steps, target, trigger, actor=actor)
            if rev:
                revisions.append(rev)
            break  # lane changed; look again on the next reconcile
    return revisions


async def _no_alternative(r: aioredis.Redis, keys: Keys, s: Step, lane_steps: list[Step], cfg: RunConfig, *,
                          actor: str) -> None:
    if s.status == S.REJECTED:  # held by reject_policy but no other route: retry or give up
        then = S.DEAD if ledger.rejection_count(s) >= s.max_attempts else S.READY
        reason = s.verdict.reason if s.verdict else None
        try:
            await ledger.transition(r, keys, s.id, then, actor=actor, actor_role="orchestrator",
                                    reason=reason if then == S.DEAD else None,
                                    payload={"replan": "no alternative skill"})
        except ledger.IllegalTransition:
            pass
    elif s.status == S.DEAD:
        await fail_dependents(r, keys, s, lane_steps, actor=actor)


async def fail_dependents(r: aioredis.Redis, keys: Keys, dead: Step, lane_steps: list[Step], *, actor: str) -> None:
    for d in _dependents_closure(dead, lane_steps)[1:]:
        if d.status == S.PLANNED:
            try:
                await ledger.transition(r, keys, d.id, S.DEAD, actor=actor, actor_role="orchestrator",
                                        reason=f"dependency {dead.id} ({dead.kind.value}) is dead")
            except ledger.IllegalTransition:
                pass


async def _move_lane(r: aioredis.Redis, keys: Keys, root: Step, lane_steps: list[Step], target, trigger: str, *,
                     actor: str) -> dict[str, Any] | None:
    chain = [x for x in _dependents_closure(root, lane_steps) if x.status in REPLANNABLE or x.id == root.id]
    if any(x.status not in REPLANNABLE | {S.DEAD} for x in chain):
        return None
    reason = f"replan: {trigger}; moving the lane to {target.value}"
    superseded: list[Step] = []
    for x in chain:
        if x.status == S.DEAD:
            superseded.append(x)
            continue
        try:
            await ledger.transition(r, keys, x.id, S.REPLANNED, actor=actor, actor_role="orchestrator",
                                    reason=reason, payload={"to_skill": target.value})
            superseded.append(x)
        except ledger.IllegalTransition:
            if x.id == root.id:  # a worker leased it meanwhile: try again later
                return None
    mapping: dict[str, str] = {}
    copies: list[Step] = []
    for x in superseded:
        data = x.model_dump(include={"run_id", "kind", "skill", "title", "inputs", "postcondition", "depends_on",
                                     "side_effect", "idempotency_key", "lane", "max_attempts"})
        copy = Step.model_validate(data)
        if copy.kind in CRM_KINDS:
            copy.skill = target
        mapping[x.id] = copy.id
        copies.append(copy)
    for c in copies:
        c.depends_on = [mapping.get(d, d) for d in c.depends_on]
    await ledger.create_steps(r, keys, copies, actor=actor, event_type=EventType.PLAN_REVISED, payload={
        "reason": reason, "lane": root.lane, "trigger_step": root.id, "to_skill": target.value, "replaces": mapping,
        "last_rejection": root.verdict.reason if root.verdict else None,
    })
    return {"lane": root.lane, "trigger": trigger, "to_skill": target.value, "replaces": mapping}
