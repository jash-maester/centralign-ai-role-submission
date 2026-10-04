"""The orchestrator (plans/03 Phase 3; B1-B6, D5; plans/01 §5, §6a, §9).

    orch = Orchestrator(r, keys)          # python -m ledger_core.services.orchestrator
    await orch.run()                      # tail ledger events + periodic sweep; runs the reaper
    await orch.reconcile(run_id)          # one idempotent pass over a run (tests, CLI)

    run = await submit_goal(r, keys, "Add the leads from yesterday's event ...",
                            input_file="event_attendees.csv", config={"crm_write_path": "api"})

It plans and routes; it never executes work and never names a worker, only
skills. Every pass over a run is idempotent and built from ledger state only,
so a restarted orchestrator simply picks up where the ledger says the run is.

Run lifecycle (one reconcile pass does whatever the run needs next):

1. created      -> run config stored (playbook front matter over env, plus the
                   submit-time overrides; run_config.init), input sha256 and
                   playbook hash pinned.
2. understanding: goal + playbook -> success criteria (LLM, orchestrator role),
                   validated against the check registry, re-asked when invalid
                   -> run.understood {criteria, event}.
3. planning:      criteria -> initial steps (normally one file.parse), each with a
                   registry postcondition, validated (skills, kinds, checks,
                   dependencies), re-asked when invalid -> plan.created.
4. running:       fan-out when the parse step commits (orchestrator_lanes: one lane
                   per lead, plan.revised), lane progression as lookups and review
                   decisions commit, replanning (orchestrator_replan, B5),
                   dependency release planned -> ready with bindings resolved.
5. finish:        when nothing can progress without a review or a human, the
                   run.criteria_met sweep (checks/run.py) evaluates every criterion
                   through REST / facts -> run.completed (all verified or waived),
                   run.completed_pending_input (some criteria wait on reviews,
                   escalations or not-yet-built work) or run.failed (a criterion
                   failed, the parse died, or the LLM stage could not produce a
                   valid plan / hit the budget).

A run in completed_pending_input is still watched: when an answer arrives and
its lane resumes, the run goes back to running and is swept again.
"""

from __future__ import annotations

import asyncio
import contextlib
import hashlib
import json
import logging
import signal
from collections.abc import Awaitable, Callable
from pathlib import Path
from typing import Any

import redis.asyncio as aioredis
from redis.exceptions import RedisError

from . import agents, ledger, llm, playbook as playbook_mod, run_config
from .checks import run as run_checks
from .config import RunConfig
from .events import append_event
from .keys import Keys
from .orchestrator_lanes import (
    REVIEW_KINDS,
    BindError,
    LaneContext,
    after_lookup,
    after_review,
    decision_for,
    event_info,
    fan_out,
    group_lanes,
    has_binds,
    lane_outcomes,
    lane_row,
    latest,
    live,
    lookup_for,
    resolve_binds,
    route,
)
from .orchestrator_llm import InvalidOutput, criteria_from, make_plan, understand
from .orchestrator_replan import fail_dependents, replan_run
from .postconditions import CheckContext
from .protocol import (
    TERMINAL_STATUSES,
    AgentCard,
    Criterion,
    Event,
    EventType,
    Postcondition,
    Run,
    RunStatus,
    Skill,
    Step,
    StepKind,
    StepStatus,
    now_ms,
)
from .settings import get_settings

log = logging.getLogger("ledger.orchestrator")
S = StepStatus
ACTOR = "orchestrator"

ACTIVE_RUN_STATUSES = frozenset({RunStatus.CREATED, RunStatus.UNDERSTANDING, RunStatus.PLANNING, RunStatus.RUNNING,
                                 RunStatus.COMPLETED_PENDING_INPUT})
# Events that may let a run progress.
TRIGGERS = frozenset({
    EventType.RUN_CREATED, EventType.STEP_COMMITTED, EventType.STEP_DEAD, EventType.STEP_REJECTED,
    EventType.STEP_READY, EventType.FACT_COMMITTED, EventType.REVIEW_RESOLVED, EventType.INPUT_ANSWERED,
    EventType.RUN_CONFIG_UPDATED, EventType.STEP_REPLANNED,
})
# Steps in these statuses are moving (a worker, the verifier or the reaper will act).
MOVING = frozenset({S.READY, S.LEASED, S.CLAIMED_DONE, S.VERIFIED, S.LEASE_EXPIRED, S.REJECTED})
HUMAN_WAIT = frozenset({S.REVIEW_REQUIRED, S.INPUT_REQUIRED})

ContextFactory = Callable[[Run, dict[str, Any]], Awaitable[CheckContext]]


def orchestrator_card(agent_id: str = ACTOR) -> AgentCard:
    return AgentCard.model_validate({
        "id": agent_id, "name": "Orchestrator", "role": "orchestrator", "model_role": "orchestrator",
        "side_effects": False, "container": "orchestrator",
        "tools": [
            {"id": "llm", "type": "llm", "name": "LLM (orchestrator role)", "detail": "understand + plan"},
            {"id": "planner", "type": "function", "name": "Deterministic fan-out, release, replan",
             "detail": "per-lead lanes; routes by skill, never by worker"},
            {"id": "criteria-sweep", "type": "function", "name": "run.criteria_met sweep",
             "detail": "CRM REST (read-only key) + committed facts"},
            {"id": "reaper", "type": "function", "name": "Lease reaper", "detail": "expired leases -> ready"},
        ],
    })


# ---------------------------------------------------------------------------
# submit
# ---------------------------------------------------------------------------


def data_path(name: str | None, data_dir: str | None = None) -> Path | None:
    if not name:
        return None
    p = Path(name)
    if p.is_absolute():
        return p if p.is_file() else None
    for base in (data_dir or get_settings().data_dir, "/repo/data", "data"):
        if (Path(base) / p).is_file():
            return Path(base) / p
    return None


async def submit_goal(
    r: aioredis.Redis, keys: Keys, goal: str, *, input_file: str | None = None,
    config: dict[str, Any] | None = None, playbook: str = playbook_mod.DEFAULT_PLAYBOOK, actor: str = "cli",
    playbook_dir: str | None = None, orchestrator: str | None = None,
) -> Run:
    """Create a run for the orchestrator: run record (run.created) and its
    starting config (playbook defaults + `config` overrides, run.config_updated).
    `orchestrator` pins the run to one orchestrator agent id (hash field
    `orchestrator`); every other orchestrator leaves it alone."""
    pb = playbook_mod.load(playbook, playbook_dir)
    cfg = RunConfig.model_validate({**pb.run_defaults().model_dump(), **(config or {})})
    run = Run(goal=goal, input_file=input_file, playbook=pb.name, playbook_hash=pb.content_hash,
              config_hash=cfg.config_hash())
    if (f := data_path(input_file)) is not None:
        run.input_sha256 = hashlib.sha256(f.read_bytes()).hexdigest()
    await run_config.init(r, keys, run.id, cfg, actor=actor)
    if orchestrator:
        # written before run.created, so no other orchestrator can see the run unowned
        await r.hset(keys.run(run.id), "orchestrator", orchestrator)
    await ledger.create_run(r, keys, run, actor=actor)
    return run


# ---------------------------------------------------------------------------
# the orchestrator
# ---------------------------------------------------------------------------


class Orchestrator:
    def __init__(
        self, r: aioredis.Redis, keys: Keys, *, agent_id: str = ACTOR, playbook_dir: str | None = None,
        context_factory: ContextFactory | None = None, run_reaper: bool = True, sweep_interval_s: float = 5.0,
        reaper_interval_s: float = 2.0, block_ms: int = 1000, owned_only: bool = False,
    ) -> None:
        self.r, self.keys, self.agent_id = r, keys, agent_id
        # owned_only: reconcile only runs pinned to this agent id (submit_goal(orchestrator=...))
        self.owned_only = owned_only
        self.playbook_dir = playbook_dir
        self.context_factory = context_factory or self._default_context
        self.run_reaper, self.reaper_interval_s = run_reaper, reaper_interval_s
        self.sweep_interval_s, self.block_ms = sweep_interval_s, block_ms
        self.card = orchestrator_card(agent_id)
        self._locks: dict[str, asyncio.Lock] = {}
        self._stop = asyncio.Event()
        self._last_finish: dict[str, str] = {}  # run_id -> fingerprint of the state last swept
        self._crm = None

    # -- lifecycle -----------------------------------------------------------

    def stop(self) -> None:
        self._stop.set()

    def install_signal_handlers(self) -> None:
        loop = asyncio.get_running_loop()
        for sig in (signal.SIGTERM, signal.SIGINT):
            loop.add_signal_handler(sig, self.stop)

    async def start(self) -> None:
        await agents.register_agent(self.r, self.keys, self.card)
        await agents.set_alive(self.r, self.keys, self.agent_id)

    async def run(self) -> None:
        """Serve forever: react to ledger events, sweep active runs every
        sweep_interval_s, run the reaper (plans/01 §5)."""
        await self.start()
        tasks = [asyncio.create_task(self._liveness())]
        if self.run_reaper:
            from .reaper import run_reaper

            tasks.append(asyncio.create_task(run_reaper(self.r, self.keys, self.reaper_interval_s)))
        last = "$"
        next_sweep = 0.0
        loop = asyncio.get_running_loop()
        try:
            while not self._stop.is_set():
                try:
                    if loop.time() >= next_sweep:
                        await self.sweep_active()
                        next_sweep = loop.time() + self.sweep_interval_s
                    rows = await self.r.xread({self.keys.events: last}, block=self.block_ms, count=500)
                except RedisError:
                    log.exception("orchestrator: redis error; retrying")
                    await asyncio.sleep(1)
                    continue
                touched: list[str] = []
                for _stream, entries in rows or []:
                    for sid, fields in entries:
                        last = sid
                        try:
                            ev = Event.model_validate_json(fields["json"])
                        except Exception:  # noqa: BLE001 - foreign entry
                            continue
                        if ev.run_id and ev.type in TRIGGERS and ev.actor != self.agent_id and ev.run_id not in touched:
                            touched.append(ev.run_id)
                for run_id in touched:
                    await self.safe_reconcile(run_id)
        finally:
            for t in tasks:
                t.cancel()
            await agents.clear_alive(self.r, self.keys, self.agent_id)
            if self._crm is not None:
                with contextlib.suppress(Exception):
                    await self._crm.aclose()

    async def _liveness(self) -> None:
        while True:
            await asyncio.sleep(agents.DEFAULT_LIVENESS_TTL_S / 3)
            with contextlib.suppress(Exception):
                await agents.set_alive(self.r, self.keys, self.agent_id)

    async def sweep_active(self) -> None:
        for run in await ledger.list_runs(self.r, self.keys, limit=200):
            if run.status in ACTIVE_RUN_STATUSES:
                await self.safe_reconcile(run.id)

    async def safe_reconcile(self, run_id: str) -> None:
        try:
            await self.reconcile(run_id)
        except asyncio.CancelledError:
            raise
        except Exception:  # noqa: BLE001 - one bad run must not stop the orchestrator
            log.exception("reconcile %s failed", run_id)

    # -- one pass --------------------------------------------------------------

    async def reconcile(self, run_id: str) -> Run | None:
        lock = self._locks.setdefault(run_id, asyncio.Lock())
        async with lock:
            run = await ledger.get_run(self.r, self.keys, run_id)
            if run is None or run.status not in ACTIVE_RUN_STATUSES:
                return run
            owner = await self.r.hget(self.keys.run(run_id), "orchestrator")
            if isinstance(owner, bytes):
                owner = owner.decode()
            if (owner and owner != self.agent_id) or (self.owned_only and owner != self.agent_id):
                return run  # pinned to another orchestrator
            try:
                if run.status in (RunStatus.CREATED, RunStatus.UNDERSTANDING) and not run.criteria:
                    run = await self._understand(run)
                if run.status in (RunStatus.UNDERSTANDING, RunStatus.PLANNING, RunStatus.CREATED):
                    run = await self._plan(run)
            except (llm.LLMBudgetExhausted, llm.LLMSpendCapReached, InvalidOutput, llm.LLMError) as exc:
                return await self._fail(run, f"{type(exc).__name__}: {exc}")
            if run.status in (RunStatus.RUNNING, RunStatus.COMPLETED_PENDING_INPUT):
                if not run.criteria:
                    # hand-written plan (CLI spec / POST /runs with steps): no
                    # understand, plan, fan-out or replan; only close it out.
                    return await self._finish_hand_planned(run)
                run = await self._progress(run)
            return run

    async def _finish_hand_planned(self, run: Run) -> Run:
        steps = await ledger.list_steps(self.r, self.keys, run.id)
        if not steps:
            return run
        waiting, moving = await self.waiting_and_moving(steps)
        if moving:
            return run
        dead = [s.id for s in steps if s.status == S.DEAD]
        if dead:
            status, reason = RunStatus.FAILED, f"dead steps: {', '.join(dead)}"
        elif all(s.status == S.COMMITTED for s in steps):
            status, reason = RunStatus.COMPLETED, f"all {len(steps)} hand-planned steps committed"
        else:
            status, reason = RunStatus.COMPLETED_PENDING_INPUT, f"waiting: {', '.join(sorted(waiting))}"
        if status == run.status:
            return run
        return await ledger.set_run_status(self.r, self.keys, run.id, status, actor=self.agent_id, reason=reason)

    async def _fail(self, run: Run, reason: str) -> Run:
        log.warning("run %s failed: %s", run.id, reason)
        return await ledger.set_run_status(self.r, self.keys, run.id, RunStatus.FAILED, actor=self.agent_id,
                                           reason=reason)

    async def _config(self, run: Run) -> RunConfig:
        pb = self._playbook(run)
        return await run_config.init(self.r, self.keys, run.id, playbook=pb, actor=self.agent_id)

    def _playbook(self, run: Run):
        return playbook_mod.load(run.playbook, self.playbook_dir)

    def _input_context(self, run: Run) -> dict[str, Any]:
        """The input file's sidecar metadata (<name>.meta.json), if any."""
        f = data_path(run.input_file)
        if f is None:
            return {}
        meta = f.with_name(f.stem + ".meta.json")
        if not meta.is_file():
            return {}
        try:
            data = json.loads(meta.read_text())
        except ValueError:
            return {}
        return {k: v for k, v in data.items() if not k.startswith("_")}

    async def _on_invalid(self, run_id: str):
        async def note(stage: str, attempt: int, problems: list[str]) -> None:
            await append_event(self.r, self.keys, Event(
                run_id=run_id, actor=self.agent_id, type=EventType.RUN_STATUS,
                payload={"stage": stage, "attempt": attempt, "invalid": problems[:10], "action": "re-ask"}))
        return note

    async def _understand(self, run: Run) -> Run:
        cfg = await self._config(run)
        if run.status == RunStatus.CREATED:
            run = await ledger.set_run_status(self.r, self.keys, run.id, RunStatus.UNDERSTANDING, actor=self.agent_id)
        pb = self._playbook(run)
        ctx = self._input_context(run)
        from . import agent_config

        instructions = await agent_config.prompt_text(self.r, self.keys, self.agent_id, "orchestrator")
        u, asks = await understand(run.goal, pb, input_file=run.input_file, context=ctx, run_id=run.id, config=cfg,
                                   instructions=instructions, on_invalid=await self._on_invalid(run.id))
        model = getattr(llm.last_call(), "model", None)
        ev = event_info(ctx.get("event_name") or u.event_name, ctx.get("event_date") or u.event_date, run.created_at)
        criteria = criteria_from(u)
        run = await ledger.update_run(
            self.r, self.keys, run.id, actor=self.agent_id, event_type=EventType.RUN_UNDERSTOOD,
            payload={"criteria": [c.model_dump() for c in criteria], "summary": u.summary, "event": ev.as_dict(),
                     "model": model, "asks": asks, "playbook_version": pb.version, "playbook_hash": pb.content_hash},
            criteria=criteria, playbook_hash=pb.content_hash, config_hash=cfg.config_hash(),
        )
        await self.r.hset(self.keys.run(run.id), "event", json.dumps(ev.as_dict()))
        return await ledger.set_run_status(self.r, self.keys, run.id, RunStatus.PLANNING, actor=self.agent_id)

    async def _event(self, run: Run):
        raw = await self.r.hget(self.keys.run(run.id), "event")
        if raw:
            d = json.loads(raw)
            return event_info(d.get("name"), d.get("date"), run.created_at)
        ctx = self._input_context(run)
        return event_info(ctx.get("event_name"), ctx.get("event_date"), run.created_at)

    async def _plan(self, run: Run) -> Run:
        cfg = await self._config(run)
        if run.status != RunStatus.PLANNING:
            run = await ledger.set_run_status(self.r, self.keys, run.id, RunStatus.PLANNING, actor=self.agent_id)
        if not await ledger.list_steps(self.r, self.keys, run.id):
            pb = self._playbook(run)
            from . import agent_config

            instructions = await agent_config.prompt_text(self.r, self.keys, self.agent_id, "orchestrator")
            plan, asks = await make_plan(run.goal, run.criteria, pb, input_file=run.input_file, run_id=run.id,
                                         config=cfg, instructions=instructions,
                                         on_invalid=await self._on_invalid(run.id))
            model = getattr(llm.last_call(), "model", None)
            refs: dict[str, str] = {}
            built: list[Step] = []
            for d in plan.steps:
                kind = StepKind(d.kind)
                skill = Skill(d.skill) if d.skill else route(kind, cfg)
                st = Step(run_id=run.id, kind=kind, skill=skill, title=d.title or kind.value, inputs=d.inputs,
                          postcondition=Postcondition(**d.postcondition.model_dump()), lane=None,
                          max_attempts=cfg.max_attempts, idempotency_key=f"{kind.value}:{d.ref}")
                refs[d.ref] = st.id
                built.append(st)
            for d, st in zip(plan.steps, built, strict=True):
                st.depends_on = [refs[x] for x in d.depends_on]
            await ledger.create_steps(self.r, self.keys, built, actor=self.agent_id, event_type=EventType.PLAN_CREATED,
                                      payload={"rationale": plan.rationale, "per_lead": plan.per_lead, "model": model,
                                               "asks": asks, "criteria": [c.id for c in run.criteria]})
        run = await ledger.set_run_status(self.r, self.keys, run.id, RunStatus.RUNNING, actor=self.agent_id)
        await self._release(run, await ledger.list_steps(self.r, self.keys, run.id))
        return run

    # -- running -----------------------------------------------------------------

    async def _progress(self, run: Run) -> Run:
        cfg = await self._config(run)
        for _ in range(6):  # each round may unlock the next; bounded
            steps = await ledger.list_steps(self.r, self.keys, run.id)
            facts = await ledger.get_facts(self.r, self.keys, run.id)
            changed = await self._fan_out(run, steps, facts, cfg)
            changed |= await self._advance_lanes(run, steps, facts, cfg)
            if not changed:
                revisions = await replan_run(self.r, self.keys, run.id, steps, cfg, actor=self.agent_id)
                changed |= bool(revisions)
                await self._fail_orphans(steps)
            steps = await ledger.list_steps(self.r, self.keys, run.id)
            changed |= bool(await self._release(run, steps))
            if not changed:
                break
        return await self._maybe_finish(run, cfg)

    async def _fan_out(self, run: Run, steps: list[Step], facts: dict[str, Any], cfg: RunConfig) -> bool:
        parse = latest(steps, StepKind.FILE_PARSE, status=S.COMMITTED)
        if parse is None or "parse.summary" not in facts or any(s.lane for s in steps):
            return False
        new = fan_out(run.id, parse, facts, cfg)
        if not new:
            return False
        lanes = sorted({s.lane for s in new if s.lane}, key=lane_row)
        await ledger.create_steps(self.r, self.keys, new, actor=self.agent_id, event_type=EventType.PLAN_REVISED,
                                  payload={"reason": "fan-out: parse committed", "lanes": lanes,
                                           "usable_rows": facts["parse.summary"].get("usable_rows"),
                                           "flagged": [{"row": f.get("row"), "reason": f.get("reason")}
                                                       for f in facts["parse.summary"].get("flagged") or []]})
        return True

    async def _advance_lanes(self, run: Run, steps: list[Step], facts: dict[str, Any], cfg: RunConfig) -> bool:
        event = None
        new: list[Step] = []
        reasons: list[str] = []
        for lane, lane_steps in group_lanes(steps).items():
            cur = live(lane_steps)
            search, lookup = lookup_for(lane_steps, facts)
            reviews = [s for s in cur if s.kind in REVIEW_KINDS]
            contact_like = [s for s in cur if s.kind in (StepKind.CRM_CREATE_CONTACT, StepKind.CRM_UPDATE_CONTACT)]
            event = event or await self._event(run)
            row = lane_row(lane)
            lead = facts.get(lane)
            lead_ref = f"fact:{lane}" if isinstance(lead, dict) else None
            if lead is None:  # phone-only rows: the parse summary's record
                first = lane_steps[0]
                lead = first.inputs.get("lead") if isinstance(first.inputs.get("lead"), dict) else {}
            ctx = LaneContext(run_id=run.id, lane=lane, row=row, lead=lead, lead_ref=lead_ref, cfg=cfg, event=event,
                              lookup=lookup, owner=lookup.get("owner"), open_deal=bool(lookup.get("open_deal")))
            built: list[Step] = []
            if search is not None and not contact_like and not [r for r in reviews if r.created_at > search.created_at]:
                built = after_lookup(ctx, search)
                reasons.append(f"{lane}: lookup {lookup.get('result')}")
            elif not contact_like and reviews:
                review = reviews[-1]
                decision = decision_for(lane, review, facts)
                if review.status == S.COMMITTED and decision is not None:
                    ctx.decision = decision
                    built = after_review(ctx, review)
                    if built:
                        reasons.append(f"{lane}: review decided {decision.get('decision')}")
            new += built
        if not new:
            return False
        await ledger.create_steps(self.r, self.keys, new, actor=self.agent_id, event_type=EventType.PLAN_REVISED,
                                  payload={"reason": "lane progression", "details": reasons})
        return True

    async def _fail_orphans(self, steps: list[Step]) -> None:
        """Planned steps whose dependency died (and was not replanned) can never run."""
        for lane_steps in group_lanes(steps).values():
            for s in lane_steps:
                if s.status == S.DEAD and not any(x.kind == s.kind and x.created_at > s.created_at for x in lane_steps):
                    await fail_dependents(self.r, self.keys, s, lane_steps, actor=self.agent_id)
        for s in steps:  # run-level steps (parse)
            if s.lane is None and s.status == S.DEAD:
                for d in steps:
                    if d.status == S.PLANNED and s.id in d.depends_on:
                        with contextlib.suppress(ledger.IllegalTransition):
                            await ledger.transition(self.r, self.keys, d.id, S.DEAD, actor=self.agent_id,
                                                    actor_role="orchestrator", reason=f"dependency {s.id} is dead")

    async def _release(self, run: Run, steps: list[Step]) -> list[str]:
        """planned -> ready (enqueued on its skill queue) once every dependency
        is committed, with bind: values resolved from the lane (B5 deterministic
        dependency release)."""
        status = {s.id: s.status for s in steps}
        lanes = group_lanes(steps)
        released: list[str] = []
        for s in steps:
            if s.status != S.PLANNED or not all(status.get(d) == S.COMMITTED for d in s.depends_on):
                continue
            updates: dict[str, Any] = {}
            if has_binds(s.inputs) or has_binds(s.postcondition.model_dump()):
                try:
                    lane_steps = lanes.get(s.lane or "", [])
                    updates["inputs"] = resolve_binds(s.inputs, lane_steps)
                    updates["postcondition"] = Postcondition.model_validate(
                        resolve_binds(s.postcondition.model_dump(), lane_steps))
                except BindError as exc:
                    log.warning("cannot release %s: %s", s.id, exc)
                    with contextlib.suppress(ledger.IllegalTransition):
                        await ledger.transition(self.r, self.keys, s.id, S.DEAD, actor=self.agent_id,
                                                actor_role="orchestrator", reason=f"unresolvable input: {exc}")
                    continue
            try:
                await ledger.transition(self.r, self.keys, s.id, S.READY, actor=self.agent_id,
                                        actor_role="orchestrator", **updates)
                released.append(s.id)
            except ledger.IllegalTransition:
                pass
        return released

    # -- finish ------------------------------------------------------------------

    async def _review_skills_served(self) -> set[str]:
        """Skills with a live agent (a review step in ready is moving only if a
        reviewer is alive to take it; human steps wait for a human)."""
        served: set[str] = set()
        for a in await agents.list_agents(self.r, self.keys):
            if a["alive"]:
                served |= {sk.id.value for sk in a["card"].skills}
        return served

    async def waiting_and_moving(self, steps: list[Step]) -> tuple[set[str], set[str]]:
        served = await self._review_skills_served()
        waiting: set[str] = set()
        moving: set[str] = set()
        for s in steps:
            if s.status in HUMAN_WAIT:
                waiting.add(s.id)
            elif s.status == S.READY and s.skill in (Skill.REVIEW, Skill.HUMAN) and s.skill.value not in served:
                waiting.add(s.id)
            elif s.status in MOVING:
                moving.add(s.id)
        by_id = {s.id: s for s in steps}
        changed = True
        while changed:  # planned steps behind a waiting step wait too; others are moving (about to release)
            changed = False
            for s in steps:
                if s.status == S.PLANNED and s.id not in waiting and s.id not in moving:
                    deps = [by_id.get(d) for d in s.depends_on]
                    if any(d is not None and d.id in waiting for d in deps):
                        waiting.add(s.id)
                        changed = True
                    elif all(d is not None and (d.status == S.COMMITTED or d.id in moving) for d in deps):
                        moving.add(s.id)
                        changed = True
        for s in steps:  # anything left planned (e.g. dead deps not yet failed) counts as moving
            if s.status == S.PLANNED and s.id not in waiting:
                moving.add(s.id)
        return waiting, moving

    async def _default_context(self, run: Run, facts: dict[str, Any]) -> CheckContext:
        if self._crm is None:
            try:
                from .crm_api import reader_from_redis

                self._crm = await reader_from_redis(self.r)
            except Exception as exc:  # noqa: BLE001 - no CRM: CRM criteria fail with the reason
                log.warning("no CRM reader for the criteria sweep: %s", exc)
        return CheckContext(run_id=run.id, facts=facts, crm=self._crm, data_dir=get_settings().data_dir,
                            extra={"run": run})

    async def _maybe_finish(self, run: Run, cfg: RunConfig) -> Run:
        steps = await ledger.list_steps(self.r, self.keys, run.id)
        if not steps:
            return run
        waiting, moving = await self.waiting_and_moving(steps)
        if moving:
            if run.status == RunStatus.COMPLETED_PENDING_INPUT:
                run = await ledger.set_run_status(self.r, self.keys, run.id, RunStatus.RUNNING, actor=self.agent_id,
                                                  reason="work resumed")
            return run
        fingerprint = hashlib.sha256(json.dumps(sorted((s.id, s.status.value) for s in steps)).encode()).hexdigest()
        if run.status == RunStatus.COMPLETED_PENDING_INPUT and self._last_finish.get(run.id) == fingerprint:
            return run
        self._last_finish[run.id] = fingerprint
        facts = await ledger.get_facts(self.r, self.keys, run.id)
        lanes = lane_outcomes(steps, facts, waiting_ids=waiting)
        ctx = await self.context_factory(run, facts)
        results = await run_checks.evaluate_criteria(run.criteria, lanes, ctx)
        criteria = [Criterion(id=c.id, text=c.text, check=c.check, status=res.status if res.status in (
            "pending", "verified", "waived", "failed") else "pending", evidence=res.evidence)
            for c, res in zip(run.criteria, results, strict=True)]
        parse_dead = any(s.kind == StepKind.FILE_PARSE and s.status == S.DEAD for s in steps)
        if parse_dead:
            status, reason = RunStatus.FAILED, "the parse step is dead"
        elif run_checks.all_met(results):
            status, reason = RunStatus.COMPLETED, f"all {len(results)} criteria verified or waived"
        elif any(x.status == run_checks.FAILED for x in results):
            failed = [x for x in results if x.status == run_checks.FAILED]
            status, reason = RunStatus.FAILED, "; ".join(f"{x.id}: {x.evidence}" for x in failed)
        else:
            pending = [x for x in results if x.status == run_checks.PENDING]
            status = RunStatus.COMPLETED_PENDING_INPUT
            reason = "; ".join(f"{x.id}: {x.evidence}" for x in pending)
        counts: dict[str, int] = {}
        for x in lanes:
            counts[x["status"]] = counts.get(x["status"], 0) + 1
        payload = {"from": run.status.value, "to": status.value, "reason": reason,
                   "criteria": [{"id": c.id, "check": c.check, "status": c.status, "evidence": c.evidence}
                                for c in criteria],
                   "lanes": counts, "waiting_steps": sorted(waiting),
                   "steps": _step_counts(steps)}
        if status == run.status:
            # same status (pending input re-swept): refresh criteria under a status event
            return await ledger.update_run(self.r, self.keys, run.id, actor=self.agent_id,
                                           event_type=EventType.RUN_STATUS, payload=payload, criteria=criteria)
        etype = {RunStatus.COMPLETED: EventType.RUN_COMPLETED, RunStatus.FAILED: EventType.RUN_FAILED,
                 RunStatus.COMPLETED_PENDING_INPUT: EventType.RUN_COMPLETED_PENDING_INPUT}[status]
        return await ledger.update_run(self.r, self.keys, run.id, actor=self.agent_id, event_type=etype,
                                       payload=payload, criteria=criteria, status=status, finished_at=now_ms())

    async def run_until_settled(self, run_id: str, *, timeout: float = 120.0, interval: float = 0.25) -> Run:
        """Test/CLI helper: reconcile repeatedly until the run leaves the active
        statuses (or settles in completed_pending_input)."""
        loop = asyncio.get_running_loop()
        deadline = loop.time() + timeout
        while True:
            run = await self.reconcile(run_id)
            if run is None or run.status not in ACTIVE_RUN_STATUSES or run.status == RunStatus.COMPLETED_PENDING_INPUT:
                return run  # type: ignore[return-value]
            if loop.time() > deadline:
                raise TimeoutError(f"run {run_id} still {run.status.value} after {timeout}s")
            await asyncio.sleep(interval)


def _step_counts(steps: list[Step]) -> dict[str, dict[str, int]]:
    out: dict[str, dict[str, int]] = {}
    for s in steps:
        out.setdefault(s.kind.value, {})
        out[s.kind.value][s.status.value] = out[s.kind.value].get(s.status.value, 0) + 1
    return out


def is_finished(run: Run) -> bool:
    return run.status in (RunStatus.COMPLETED, RunStatus.FAILED, RunStatus.COMPLETED_PENDING_INPUT)


__all__ = ["Orchestrator", "submit_goal", "orchestrator_card", "is_finished", "TERMINAL_STATUSES"]
