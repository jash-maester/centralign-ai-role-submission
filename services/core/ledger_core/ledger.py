"""The ledger: runs, steps, the step state machine and committed facts (plans/01 §3, A1-A5).

Public API (all coroutines take the redis client and a Keys namespace first):

Runs
    create_run(r, keys, run | goal=..., actor=...)          -> Run       (run.created)
    get_run / list_runs
    update_run(r, keys, run_id, actor=, event_type=, **fields) -> Run    (event_type you pick)
    set_run_status(r, keys, run_id, status, actor=, reason=)  -> Run      (run.status / run.completed / ...)
    get_run_config / set_run_config                                       (run:{id}:config, run.config_updated)

Steps
    create_steps(r, keys, steps, actor=, event_type=PLAN_CREATED) -> list[Step]
    create_step(...)                         one-step convenience
    get_step / list_steps(run_id)
    transition(r, keys, step_id, to, actor=, actor_role=, fence=None, **updates) -> Step
    observe(r, keys, step_id, observation, actor=, fence=)          (step.observation)
    claim(r, keys, step_id, claim, fence=)   leased -> claimed_done, enqueued on queue:verify
    commit(r, keys, step_id, verdict, facts, actor=)   claimed_done -> verified -> committed + facts
    reject(r, keys, step_id, verdict, actor=, then=READY|DEAD|None)
    release_dependents(r, keys, run_id, actor=)  planned steps whose deps are all committed -> ready

Facts (written only at commit; workers read only these)
    commit_fact / get_facts / get_fact_records / resolve_inputs

Audit
    rebuild_statuses_from_events(r, keys, run_id) -> {step_id: StepStatus}   (A1)

Rules enforced by transition() (one WATCH/MULTI/EXEC, retried on contention):
- protocol.LEGAL_TRANSITIONS; illegal moves raise IllegalTransition.
- Only actor_role="verifier" may enter VERIFIER_ONLY_STATUSES (verified,
  committed, rejected). actor_role="worker" may only enter leased,
  claimed_done and review_required, and must pass its fencing token.
- A write whose fence is lower than fence:{step} (or, for a leased step, not
  equal to the lease's fence) is stale: a step.stale_fence event is appended
  and StaleFenceError raised (A4).
- Every change appends its event in the same transaction (A1). Entering
  `ready` enqueues the step on queue:{skill}; entering `claimed_done`
  enqueues it on queue:verify; both in the same transaction too.
- history: entering `leased` opens a protocol.Attempt (attempt += 1);
  claimed_done stores the claim on it; verified/rejected store the verdict;
  committed / rejected / lease_expired / review_required / dead / replanned
  close it with that outcome.
"""

from __future__ import annotations

import json
from collections.abc import Iterable
from typing import Any

import redis.asyncio as aioredis
from redis.exceptions import WatchError

from . import bus
from .config import RunConfig
from .events import append_event, encode_event, read_events
from .keys import Keys
from .protocol import (
    LEGAL_TRANSITIONS,
    TERMINAL_STATUSES,
    VERIFIER_ONLY_STATUSES,
    Attempt,
    Claim,
    Event,
    EventType,
    Fact,
    Run,
    RunStatus,
    Step,
    StepStatus,
    Verdict,
    now_ms,
)

S = StepStatus


class LedgerError(Exception):
    pass


class NotFound(LedgerError):
    pass


class IllegalTransition(LedgerError):
    pass


class ForbiddenTransition(IllegalTransition):
    """Legal edge, but this actor_role may not take it (e.g. a worker committing)."""


class StaleFenceError(LedgerError):
    pass


class MissingFact(LedgerError):
    pass


TRANSITION_EVENT: dict[StepStatus, EventType] = {
    S.READY: EventType.STEP_READY,
    S.LEASED: EventType.STEP_LEASED,
    S.CLAIMED_DONE: EventType.STEP_CLAIMED,
    S.VERIFIED: EventType.STEP_VERIFIED,
    S.COMMITTED: EventType.STEP_COMMITTED,
    S.REJECTED: EventType.STEP_REJECTED,
    S.REPLANNED: EventType.STEP_REPLANNED,
    S.LEASE_EXPIRED: EventType.STEP_LEASE_EXPIRED,
    S.REVIEW_REQUIRED: EventType.REVIEW_REQUESTED,
    S.INPUT_REQUIRED: EventType.INPUT_REQUESTED,
    S.DEAD: EventType.STEP_DEAD,
}

WORKER_TARGETS: frozenset[StepStatus] = frozenset({S.LEASED, S.CLAIMED_DONE, S.REVIEW_REQUIRED})

# Outcome recorded on the open Attempt when a step enters these statuses.
_CLOSES_ATTEMPT: dict[StepStatus, str] = {
    S.COMMITTED: "committed",
    S.REJECTED: "rejected",
    S.LEASE_EXPIRED: "lease_expired",
    S.REVIEW_REQUIRED: "review_required",
    S.DEAD: "dead",
    S.REPLANNED: "replanned",
}

# Step fields transition(**updates) may not touch (the engine owns them).
_PROTECTED = {"id", "run_id", "status", "history", "fence", "lease_owner", "attempt", "created_at", "updated_at"}

_RUN_TERMINAL_EVENT = {
    RunStatus.COMPLETED: EventType.RUN_COMPLETED,
    RunStatus.COMPLETED_PENDING_INPUT: EventType.RUN_COMPLETED_PENDING_INPUT,
    RunStatus.FAILED: EventType.RUN_FAILED,
}

_MAX_RETRIES = 50


# ---------------------------------------------------------------------------
# storage helpers
# ---------------------------------------------------------------------------


def _step_fields(step: Step) -> dict[str, str]:
    """Full record in `json`, plus a few raw fields for redis-cli and scans."""
    return {
        "json": step.model_dump_json(),
        "status": step.status.value,
        "run_id": step.run_id,
        "skill": step.skill.value,
        "attempt": str(step.attempt),
        "fence": str(step.fence),
        "lease_owner": step.lease_owner or "",
    }


def _run_fields(run: Run) -> dict[str, str]:
    return {"json": run.model_dump_json(), "status": run.status.value, "goal": run.goal}


async def get_step(r: aioredis.Redis, keys: Keys, step_id: str) -> Step | None:
    raw = await r.hget(keys.step(step_id), "json")
    return Step.model_validate_json(raw) if raw else None


async def _must_step(r: aioredis.Redis, keys: Keys, step_id: str) -> Step:
    step = await get_step(r, keys, step_id)
    if step is None:
        raise NotFound(f"step {step_id} not found")
    return step


async def list_steps(r: aioredis.Redis, keys: Keys, run_id: str) -> list[Step]:
    ids = await r.lrange(keys.run_steps(run_id), 0, -1)
    if not ids:
        return []
    pipe = r.pipeline(transaction=False)
    for sid in ids:
        pipe.hget(keys.step(sid), "json")
    raws = await pipe.execute()
    return [Step.model_validate_json(x) for x in raws if x]


# ---------------------------------------------------------------------------
# runs
# ---------------------------------------------------------------------------


async def create_run(
    r: aioredis.Redis, keys: Keys, run: Run | None = None, *, goal: str | None = None,
    actor: str = "api", **fields: Any,
) -> Run:
    """Store a new run and append run.created. Pass a Run, or goal=... (+ Run fields)."""
    if run is None:
        if goal is None:
            raise ValueError("create_run needs a Run or goal=")
        run = Run(goal=goal, **fields)
    ev = Event(run_id=run.id, actor=actor, type=EventType.RUN_CREATED,
               payload={"goal": run.goal, "input_file": run.input_file, "playbook": run.playbook,
                        "replay_of": run.replay_of})
    pipe = r.pipeline(transaction=True)
    pipe.hset(keys.run(run.id), mapping=_run_fields(run))
    pipe.zadd(keys.runs, {run.id: run.created_at})
    pipe.xadd(keys.events, encode_event(ev))
    await pipe.execute()
    return run


async def get_run(r: aioredis.Redis, keys: Keys, run_id: str) -> Run | None:
    raw = await r.hget(keys.run(run_id), "json")
    return Run.model_validate_json(raw) if raw else None


async def list_runs(r: aioredis.Redis, keys: Keys, limit: int = 50) -> list[Run]:
    """Newest first."""
    ids = await r.zrevrange(keys.runs, 0, limit - 1)
    runs = [await get_run(r, keys, i) for i in ids]
    return [x for x in runs if x]


async def update_run(
    r: aioredis.Redis, keys: Keys, run_id: str, *, actor: str, event_type: EventType,
    payload: dict[str, Any] | None = None, **updates: Any,
) -> Run:
    """Set Run fields (e.g. criteria, input_sha256) and append `event_type` with
    the changed field names (+ payload). Atomic."""
    bad = set(updates) - set(Run.model_fields) | ({"id"} & set(updates))
    if bad:
        raise ValueError(f"cannot update run fields {sorted(bad)}")
    async with r.pipeline(transaction=True) as pipe:
        for _ in range(_MAX_RETRIES):
            try:
                await pipe.watch(keys.run(run_id))
                raw = await pipe.hget(keys.run(run_id), "json")
                if not raw:
                    raise NotFound(f"run {run_id} not found")
                run = Run.model_validate_json(raw)
                run = Run.model_validate({**run.model_dump(), **updates})
                ev = Event(run_id=run_id, actor=actor, type=event_type,
                           payload={"fields": sorted(updates), **(payload or {})})
                pipe.multi()
                pipe.hset(keys.run(run_id), mapping=_run_fields(run))
                pipe.xadd(keys.events, encode_event(ev))
                await pipe.execute()
                return run
            except WatchError:
                continue
    raise LedgerError(f"update_run {run_id}: too much contention")


async def set_run_status(
    r: aioredis.Redis, keys: Keys, run_id: str, status: RunStatus, *, actor: str,
    reason: str | None = None,
) -> Run:
    """Terminal statuses emit run.completed / run.completed_pending_input /
    run.failed (and set finished_at); others emit run.status."""
    status = RunStatus(status)
    run = await get_run(r, keys, run_id)
    if run is None:
        raise NotFound(f"run {run_id} not found")
    updates: dict[str, Any] = {"status": status}
    if status in _RUN_TERMINAL_EVENT:
        updates["finished_at"] = now_ms()
    payload = {"from": run.status.value, "to": status.value}
    if reason:
        payload["reason"] = reason
    return await update_run(
        r, keys, run_id, actor=actor, event_type=_RUN_TERMINAL_EVENT.get(status, EventType.RUN_STATUS),
        payload=payload, **updates,
    )


async def get_run_config(r: aioredis.Redis, keys: Keys, run_id: str) -> RunConfig:
    """run:{id}:config is a Hash of RunConfig field -> JSON value (+ config_hash).
    Missing hash -> defaults. Values that are not JSON are taken as raw strings."""
    raw = await r.hgetall(keys.run_config(run_id))
    if not raw:
        return RunConfig()
    if "json" in raw:
        return RunConfig.model_validate_json(raw["json"])
    vals: dict[str, Any] = {}
    for k, v in raw.items():
        if k in RunConfig.model_fields:
            try:
                vals[k] = json.loads(v)
            except (TypeError, ValueError):
                vals[k] = v
    return RunConfig(**vals)


async def set_run_config(
    r: aioredis.Redis, keys: Keys, run_id: str, config: RunConfig, *, actor: str,
) -> RunConfig:
    """Replace the run's config; append run.config_updated with the diff and hash."""
    old = await get_run_config(r, keys, run_id)
    diff = {k: {"from": getattr(old, k), "to": getattr(config, k)}
            for k in RunConfig.model_fields if getattr(old, k) != getattr(config, k)}
    fields = {k: json.dumps(v) for k, v in config.model_dump(mode="json").items()}
    fields["config_hash"] = config.config_hash()
    ev = Event(run_id=run_id, actor=actor, type=EventType.RUN_CONFIG_UPDATED,
               payload={"diff": diff, "config_hash": fields["config_hash"]})
    pipe = r.pipeline(transaction=True)
    pipe.delete(keys.run_config(run_id))
    pipe.hset(keys.run_config(run_id), mapping=fields)
    pipe.xadd(keys.events, encode_event(ev))
    await pipe.execute()
    return config


# ---------------------------------------------------------------------------
# steps: creation
# ---------------------------------------------------------------------------


async def create_steps(
    r: aioredis.Redis, keys: Keys, steps: Iterable[Step], *, actor: str,
    event_type: EventType = EventType.PLAN_CREATED, payload: dict[str, Any] | None = None,
) -> list[Step]:
    """Store steps (normally status=planned) and append one event per run whose
    payload["steps"] lists {id, kind, skill, status, title, depends_on, lane}.
    Use event_type=PLAN_REVISED for fan-out/replans. Steps created directly in
    `ready` are enqueued."""
    steps = list(steps)
    by_run: dict[str, list[Step]] = {}
    for s in steps:
        by_run.setdefault(s.run_id, []).append(s)
    pipe = r.pipeline(transaction=True)
    for run_id, group in by_run.items():
        for s in group:
            pipe.hset(keys.step(s.id), mapping=_step_fields(s))
            pipe.rpush(keys.run_steps(run_id), s.id)
            if s.status == S.READY:
                env = bus.task_envelope(s.id, s.run_id, s.skill, sender=actor)
                pipe.xadd(keys.queue(s.skill.value), bus.envelope_fields(env))
        summary = [{"id": s.id, "kind": s.kind.value, "skill": s.skill.value, "status": s.status.value,
                    "title": s.title, "depends_on": s.depends_on, "lane": s.lane} for s in group]
        ev = Event(run_id=run_id, actor=actor, type=event_type, payload={**(payload or {}), "steps": summary})
        pipe.xadd(keys.events, encode_event(ev))
    await pipe.execute()
    return steps


async def create_step(r: aioredis.Redis, keys: Keys, step: Step, *, actor: str, **kw: Any) -> Step:
    return (await create_steps(r, keys, [step], actor=actor, **kw))[0]


# ---------------------------------------------------------------------------
# steps: the state machine
# ---------------------------------------------------------------------------


def _close_attempt(step: Step, outcome: str) -> None:
    if step.history and step.history[-1].ended_at is None:
        step.history[-1].outcome = outcome
        step.history[-1].ended_at = now_ms()


def _apply(
    step: Step, to: StepStatus, *, actor: str, actor_role: str, fence: int | None,
    claim: Claim | None, verdict: Verdict | None, model: str | None, reason: str | None,
    payload: dict[str, Any] | None, updates: dict[str, Any],
) -> Event:
    """Validate and apply one transition in memory; return its event."""
    src = step.status
    if to not in LEGAL_TRANSITIONS[src]:
        raise IllegalTransition(f"{step.id}: {src.value} -> {to.value} is not a legal transition")
    if to in VERIFIER_ONLY_STATUSES and actor_role != "verifier":
        raise ForbiddenTransition(f"{step.id}: only the verifier may move a step to {to.value}")
    if actor_role == "worker" and to not in WORKER_TARGETS:
        raise ForbiddenTransition(f"{step.id}: workers may not move a step to {to.value}")
    bad = set(updates) & _PROTECTED | (set(updates) - set(Step.model_fields))
    if bad:
        raise ValueError(f"cannot set step fields {sorted(bad)} via transition")

    for k, v in updates.items():
        setattr(step, k, v)
    ev_payload: dict[str, Any] = {"from": src.value, "to": to.value}

    if to == S.LEASED:
        step.attempt += 1
        step.fence = fence or 0
        step.lease_owner = actor
        step.claim = None
        step.verdict = None
        step.history.append(Attempt(attempt=step.attempt, worker=actor, fence=step.fence, model=model))
    if src == S.LEASED and to != S.LEASED:
        if to != S.CLAIMED_DONE:
            ev_payload["worker"] = step.lease_owner
        step.lease_owner = None
    if to == S.CLAIMED_DONE:
        if claim is None:
            raise ValueError("claimed_done needs a claim")
        step.claim = claim
        if step.history:
            step.history[-1].claim = claim
            if model:
                step.history[-1].model = model
        ev_payload.update(summary=claim.summary, acted=claim.acted, evidence=claim.evidence)
    if verdict is not None and to in (S.VERIFIED, S.REJECTED):
        step.verdict = verdict
        if step.history:
            step.history[-1].verdict = verdict
        ev_payload.update(check=verdict.check, reason=verdict.reason, ok=verdict.ok)
    if to in _CLOSES_ATTEMPT:
        _close_attempt(step, _CLOSES_ATTEMPT[to])
    if reason:
        ev_payload["reason"] = reason
    ev_payload.update(attempt=step.attempt, fence=step.fence, **(payload or {}))
    step.status = to
    step.updated_at = now_ms()
    return Event(run_id=step.run_id, step_id=step.id, actor=actor, type=TRANSITION_EVENT[to], payload=ev_payload)


async def _stale(r: aioredis.Redis, keys: Keys, step: Step, actor: str, fence: int, current: int, op: str) -> None:
    await append_event(r, keys, Event(
        run_id=step.run_id, step_id=step.id, actor=actor, type=EventType.STEP_STALE_FENCE,
        payload={"fence": fence, "current_fence": current, "step_fence": step.fence,
                 "status": step.status.value, "op": op},
    ))
    raise StaleFenceError(f"{step.id}: stale fence {fence} (current {current}, step {step.fence}) for {op}")


async def _mutate(
    r: aioredis.Redis, keys: Keys, step_id: str, chain: list[tuple[StepStatus, dict[str, Any]]], *,
    actor: str, actor_role: str, fence: int | None, facts: dict[str, Any] | None = None,
    enqueue: bool = True, observation: dict[str, Any] | None = None,
) -> Step:
    """WATCH step + fence, validate, then write step, index, events, queue
    entries and facts in one MULTI/EXEC. Retries on contention."""
    if actor_role == "worker" and fence is None:
        raise ForbiddenTransition("worker writes must carry their fencing token")
    async with r.pipeline(transaction=True) as pipe:
        for _ in range(_MAX_RETRIES):
            try:
                await pipe.watch(keys.step(step_id), keys.fence(step_id))
                raw = await pipe.hget(keys.step(step_id), "json")
                if not raw:
                    raise NotFound(f"step {step_id} not found")
                step = Step.model_validate_json(raw)
                src = step.status
                stale_at: int | None = None
                if fence is not None:
                    current = int(await pipe.get(keys.fence(step_id)) or 0)
                    leased_mismatch = src == S.LEASED and fence != step.fence
                    if fence < current or leased_mismatch or (observation is not None and src != S.LEASED):
                        stale_at = current
                if stale_at is not None:
                    await pipe.reset()
                    op = "observe" if observation is not None else "->".join(t.value for t, _ in chain)
                    await _stale(r, keys, step, actor, fence or 0, stale_at, op)

                events: list[Event] = []
                if observation is not None:
                    if step.history:
                        step.history[-1].observations.append(observation)
                    step.updated_at = now_ms()
                    events.append(Event(run_id=step.run_id, step_id=step.id, actor=actor,
                                        type=EventType.STEP_OBSERVATION,
                                        payload={**observation, "attempt": step.attempt, "fence": step.fence}))
                for to, kw in chain:
                    events.append(_apply(step, S(to), actor=actor, actor_role=actor_role, fence=fence, **kw))

                pipe.multi()
                pipe.hset(keys.step(step_id), mapping=_step_fields(step))
                if step.status == S.LEASED:
                    pipe.sadd(keys.leased_steps, step_id)
                elif src == S.LEASED:
                    pipe.srem(keys.leased_steps, step_id)
                for ev in events:
                    pipe.xadd(keys.events, encode_event(ev))
                for key, value in (facts or {}).items():
                    fact = Fact(key=key, value=value, source_step=step_id)
                    pipe.hset(keys.facts(step.run_id), key, fact.model_dump_json())
                    pipe.xadd(keys.events, encode_event(Event(
                        run_id=step.run_id, step_id=step_id, actor=actor, type=EventType.FACT_COMMITTED,
                        payload={"key": key, "value": value})))
                if enqueue and step.status == S.READY and src != S.READY:
                    env = bus.task_envelope(step.id, step.run_id, step.skill, sender=actor)
                    pipe.xadd(keys.queue(step.skill.value), bus.envelope_fields(env))
                if step.status == S.CLAIMED_DONE and src != S.CLAIMED_DONE:
                    env = bus.artifact_envelope(step.id, step.run_id, step.skill, sender=actor, fence=step.fence)
                    pipe.xadd(keys.verify_queue, bus.envelope_fields(env))
                await pipe.execute()
                return step
            except WatchError:
                continue
    raise LedgerError(f"step {step_id}: too much contention")


async def transition(
    r: aioredis.Redis, keys: Keys, step_id: str, to: StepStatus | str, *, actor: str, actor_role: str,
    fence: int | None = None, claim: Claim | None = None, verdict: Verdict | None = None,
    model: str | None = None, reason: str | None = None, payload: dict[str, Any] | None = None,
    enqueue: bool = True, **updates: Any,
) -> Step:
    """Move one step to `to` atomically. `updates` sets other Step fields
    (e.g. skill=..., inputs=... on a replan-to-ready). See module docstring."""
    kw = dict(claim=claim, verdict=verdict, model=model, reason=reason, payload=payload, updates=updates)
    return await _mutate(r, keys, step_id, [(S(to), kw)], actor=actor, actor_role=actor_role,
                         fence=fence, enqueue=enqueue)


def _kw(**kw: Any) -> dict[str, Any]:
    base = dict(claim=None, verdict=None, model=None, reason=None, payload=None, updates={})
    base.update(kw)
    return base


async def observe(
    r: aioredis.Redis, keys: Keys, step_id: str, observation: dict[str, Any], *, actor: str,
    fence: int, actor_role: str = "worker",
) -> Step:
    """Record what a worker saw (on the open attempt) and emit step.observation.
    Only the current lease holder (matching fence, step leased) may observe."""
    obs = {"ts": now_ms(), **observation}
    return await _mutate(r, keys, step_id, [], actor=actor, actor_role=actor_role, fence=fence, observation=obs)


async def claim(
    r: aioredis.Redis, keys: Keys, step_id: str, claim: Claim, *, fence: int | None = None,
    model: str | None = None,
) -> Step:
    """leased -> claimed_done with the worker's claim; enqueued for the verifier."""
    return await transition(r, keys, step_id, S.CLAIMED_DONE, actor=claim.worker, actor_role="worker",
                            fence=claim.fence if fence is None else fence, claim=claim, model=model)


async def commit(
    r: aioredis.Redis, keys: Keys, step_id: str, verdict: Verdict, facts: dict[str, Any] | None = None, *,
    actor: str = "verifier",
) -> Step:
    """Verifier only: claimed_done -> verified -> committed, writing `facts`
    (key -> value) in the same transaction. This is the only place step
    results become facts."""
    chain = [(S.VERIFIED, _kw(verdict=verdict)),
             (S.COMMITTED, _kw(payload={"facts": sorted(facts or {})}))]
    return await _mutate(r, keys, step_id, chain, actor=actor, actor_role="verifier", fence=None, facts=facts)


async def reject(
    r: aioredis.Redis, keys: Keys, step_id: str, verdict: Verdict, *, actor: str = "verifier",
    then: StepStatus | None = S.READY,
) -> Step:
    """Verifier only: claimed_done -> rejected, then (same transaction) `then`:
    READY (requeued for a retry; the next attempt sees verdict.reason),
    DEAD, or None to leave it `rejected` for the orchestrator to replan."""
    chain = [(S.REJECTED, _kw(verdict=verdict, reason=verdict.reason))]
    if then is not None:
        chain.append((S(then), _kw(reason=verdict.reason if then == S.DEAD else None)))
    return await _mutate(r, keys, step_id, chain, actor=actor, actor_role="verifier", fence=None)


async def expire_lease(r: aioredis.Redis, keys: Keys, step_id: str, *, actor: str = "reaper") -> Step:
    """Reaper: leased -> lease_expired -> ready (requeued) in one transaction, or
    -> dead once the step's leases have expired max_attempts times."""
    step = await _must_step(r, keys, step_id)
    chain = [(S.LEASE_EXPIRED, _kw(reason="lease key expired"))]
    if lease_expiry_count(step) + 1 >= step.max_attempts:
        chain.append((S.DEAD, _kw(reason=f"lease expired {step.max_attempts} times")))
    else:
        chain.append((S.READY, _kw(payload={"requeued": True, "after": "lease_expired"})))
    return await _mutate(r, keys, step_id, chain, actor=actor, actor_role="reaper", fence=None)


def rejection_count(step: Step) -> int:
    return sum(1 for a in step.history if a.outcome == "rejected")


def lease_expiry_count(step: Step) -> int:
    return sum(1 for a in step.history if a.outcome == "lease_expired")


def rejection_reasons(step: Step) -> list[str]:
    return [a.verdict.reason for a in step.history if a.outcome == "rejected" and a.verdict]


async def release_dependents(r: aioredis.Redis, keys: Keys, run_id: str, *, actor: str = "orchestrator") -> list[str]:
    """Move every `planned` step whose depends_on are all committed to `ready`
    (which enqueues it). Deterministic dependency release; returns step ids."""
    steps = await list_steps(r, keys, run_id)
    status = {s.id: s.status for s in steps}
    released = []
    for s in steps:
        if s.status == S.PLANNED and all(status.get(d) == S.COMMITTED for d in s.depends_on):
            try:
                await transition(r, keys, s.id, S.READY, actor=actor, actor_role="orchestrator")
                released.append(s.id)
            except IllegalTransition:
                pass
    return released


# ---------------------------------------------------------------------------
# facts
# ---------------------------------------------------------------------------


async def commit_fact(
    r: aioredis.Redis, keys: Keys, run_id: str, key: str, value: Any, *, source_step: str, actor: str,
) -> Fact:
    """Write one fact + fact.committed. Callers: the verifier (via commit()),
    and review decisions (meta-reviewer at/above threshold, human answers).
    Workers never call this."""
    fact = Fact(key=key, value=value, source_step=source_step)
    pipe = r.pipeline(transaction=True)
    pipe.hset(keys.facts(run_id), key, fact.model_dump_json())
    pipe.xadd(keys.events, encode_event(Event(run_id=run_id, step_id=source_step, actor=actor,
                                              type=EventType.FACT_COMMITTED, payload={"key": key, "value": value})))
    await pipe.execute()
    return fact


async def get_fact_records(r: aioredis.Redis, keys: Keys, run_id: str) -> dict[str, Fact]:
    raw = await r.hgetall(keys.facts(run_id))
    return {k: Fact.model_validate_json(v) for k, v in raw.items()}


async def get_facts(r: aioredis.Redis, keys: Keys, run_id: str) -> dict[str, Any]:
    return {k: f.value for k, f in (await get_fact_records(r, keys, run_id)).items()}


FACT_PREFIX = "fact:"


def _resolve(value: Any, facts: dict[str, Any], missing: list[str]) -> Any:
    if isinstance(value, str) and value.startswith(FACT_PREFIX):
        key = value[len(FACT_PREFIX):]
        if key not in facts:
            missing.append(key)
            return None
        return facts[key]
    if isinstance(value, dict):
        return {k: _resolve(v, facts, missing) for k, v in value.items()}
    if isinstance(value, list):
        return [_resolve(v, facts, missing) for v in value]
    return value


async def resolve_inputs(
    r: aioredis.Redis, keys: Keys, step: Step, values: dict[str, Any] | None = None, *, strict: bool = True,
) -> dict[str, Any]:
    """Replace every "fact:<key>" string (at any depth) in step.inputs (or
    `values`) with the committed fact's value. strict: raise MissingFact if a
    referenced fact is not committed; otherwise it resolves to None."""
    facts = await get_facts(r, keys, step.run_id)
    missing: list[str] = []
    out = _resolve(step.inputs if values is None else values, facts, missing)
    if missing and strict:
        raise MissingFact(f"{step.id}: facts not committed: {', '.join(missing)}")
    return out


# ---------------------------------------------------------------------------
# audit
# ---------------------------------------------------------------------------


async def rebuild_statuses_from_events(r: aioredis.Redis, keys: Keys, run_id: str) -> dict[str, StepStatus]:
    """Replay ledger:events for a run: step creation events (payload.steps)
    give initial statuses, every transition event carries payload.to (A1)."""
    statuses: dict[str, StepStatus] = {}
    after = "-"
    while True:
        rows = await r.xrange(keys.events, min=after if after == "-" else f"({after}", count=1000)
        if not rows:
            break
        after = rows[-1][0]
        for _sid, fields in rows:
            ev = Event.model_validate_json(fields["json"])
            if ev.run_id != run_id:
                continue
            for s in ev.payload.get("steps") or []:
                if isinstance(s, dict) and "id" in s and "status" in s:
                    statuses[s["id"]] = S(s["status"])
            if ev.step_id and ev.type in TRANSITION_EVENT.values() and "to" in ev.payload:
                statuses[ev.step_id] = S(ev.payload["to"])
    return statuses


async def run_events(r: aioredis.Redis, keys: Keys, run_id: str) -> list[Event]:
    """Every event of a run, oldest first (pages through the stream)."""
    out: list[Event] = []
    after = "-"
    while True:
        batch = await read_events(r, keys, after=after, count=1000)
        if not batch:
            return out
        after = batch[-1].id or after
        out.extend(e for e in batch if e.run_id == run_id)


def is_terminal(status: StepStatus) -> bool:
    return status in TERMINAL_STATUSES
