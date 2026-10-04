"""The loop every worker service runs (plans/01 §5, §7; A3, A6, A7).

    worker = Worker(r, keys, card, handle)
    await worker.run()            # until stop() / SIGTERM

    async def handle(step: Step, ctx: WorkContext) -> WorkResult:
        rows = parse(ctx.inputs["file"])
        await ctx.observe({"note": "parsed"})
        return WorkResult(summary="parsed 12 rows", data={...})

Per bus message:
  consume (consumer group, consumer = agent id)
  -> skip unless the step is `ready` and ours
  -> leases.acquire (SET NX PX + INCR fence)        none -> someone else has it, ack
  -> transition ready -> leased with the fence
  -> heartbeat every lease_ttl/3 (RunConfig from run:{id}:config, else defaults);
     a lost lease cancels the handler and nothing is claimed
  -> handler(step, ctx)   (exceptions become an error claim the verifier rejects)
  -> ledger.claim(... fence) -> claimed_done + queue:verify
  -> release, ack

The handler gets a WorkContext built only from ledger state (§7): inputs
resolved from committed facts, the committed facts, prior attempts with their
observations and the verifier's rejection reasons, the RunConfig, and
ctx.observe() which records a step.observation.

Faults: if the `false_claim` switch is set (Keys.faults field
"false_claim:<skill>" or "false_claim"; value = remaining shots or "on"), one
shot is consumed, the handler is skipped and the worker claims success with
acted=False and no work done (F2: the verifier must catch it).

SIGTERM: stop consuming, cancel the current handler, release the lease (so the
reaper requeues the step at once), drop liveness, exit. SIGKILL needs nothing:
the lease expires and the reaper requeues.
"""

from __future__ import annotations

import asyncio
import logging
import signal
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from typing import Any

import redis.asyncio as aioredis
from pydantic import BaseModel, Field
from redis.exceptions import RedisError

from . import agents, bus, leases, ledger
from .config import RunConfig
from .events import append_event
from .keys import Keys
from .protocol import AgentCard, Attempt, Claim, Event, EventType, FaultName, Step, StepStatus

log = logging.getLogger("ledger.worker")


class WorkResult(BaseModel):
    """What a handler returns; becomes the protocol.Claim."""

    summary: str
    data: dict[str, Any] = Field(default_factory=dict)
    evidence: list[str] = Field(default_factory=list)
    acted: bool = True
    model: str | None = None


@dataclass
class WorkContext:
    r: aioredis.Redis
    keys: Keys
    agent_id: str
    step: Step
    fence: int
    inputs: dict[str, Any]
    facts: dict[str, Any]
    attempts: list[Attempt]  # prior attempts on this step (current one excluded)
    config: RunConfig
    observations: list[dict[str, Any]] = field(default_factory=list)

    @property
    def rejection_reasons(self) -> list[str]:
        return [a.verdict.reason for a in self.attempts if a.outcome == "rejected" and a.verdict]

    @property
    def prior_observations(self) -> list[dict[str, Any]]:
        return [o for a in self.attempts for o in a.observations]

    async def observe(self, observation: dict[str, Any]) -> None:
        self.observations.append(observation)
        await ledger.observe(self.r, self.keys, self.step.id, observation, actor=self.agent_id, fence=self.fence)


Handler = Callable[[Step, WorkContext], Awaitable[WorkResult | dict[str, Any]]]

_TAKE_SHOT = """
for i, f in ipairs(ARGV) do
  local v = redis.call('HGET', KEYS[1], f)
  if v then
    if v == 'on' then return f end
    local n = tonumber(v)
    if n and n > 0 then
      if n <= 1 then redis.call('HDEL', KEYS[1], f) else redis.call('HINCRBY', KEYS[1], f, -1) end
      return f
    end
  end
end
return false
"""


async def take_fault_shot(r: aioredis.Redis, keys: Keys, fault: FaultName | str, *, skill: str | None = None) -> str | None:
    """Consume one shot of a fault switch; returns the field consumed or None.
    A skill-scoped field ("<fault>:<skill>") is checked before the global one."""
    fields = ([f"{fault}:{skill}"] if skill else []) + [str(fault)]
    return await r.eval(_TAKE_SHOT, 1, keys.faults, *fields) or None


class Worker:
    def __init__(
        self, r: aioredis.Redis, keys: Keys, card: AgentCard, handler: Handler, *,
        block_ms: int = 2000, liveness_ttl_s: int = agents.DEFAULT_LIVENESS_TTL_S,
        min_idle_ms: int = 30_000,
    ) -> None:
        self.r, self.keys, self.card, self.handler = r, keys, card, handler
        self.agent_id = card.id
        self.skills = {s.id for s in card.skills}
        self.block_ms = block_ms
        self.liveness_ttl_s = liveness_ttl_s
        self.consumer = bus.skill_consumer(r, keys, list(self.skills), self.agent_id, min_idle_ms=min_idle_ms)
        self._stop = asyncio.Event()
        self._current: dict[str, Any] = {}
        self._busy: asyncio.Task | None = None
        self._started = False

    # -- lifecycle ---------------------------------------------------------

    async def start(self) -> None:
        if not self._started:
            await self.consumer.setup()
            await agents.register_agent(self.r, self.keys, self.card)
            await self._beat()
            self._started = True
            log.info("%s ready: consuming %s", self.agent_id, ", ".join(sorted(self.consumer.streams)))

    def stop(self) -> None:
        """Graceful stop: no new work; the step in progress is abandoned (lease released)."""
        self._stop.set()
        if self._busy and not self._busy.done():
            self._busy.cancel()

    def install_signal_handlers(self) -> None:
        loop = asyncio.get_running_loop()
        for sig in (signal.SIGTERM, signal.SIGINT):
            loop.add_signal_handler(sig, self.stop)

    async def run(self) -> None:
        await self.start()
        live = asyncio.create_task(self._liveness_loop())
        try:
            while not self._stop.is_set():
                try:
                    deliveries = await self.consumer.next(self.block_ms)
                except RedisError:
                    log.exception("%s: bus read failed; retrying", self.agent_id)
                    await asyncio.sleep(1)
                    continue
                for d in deliveries:
                    if self._stop.is_set():
                        break
                    self._busy = asyncio.create_task(self.process(d))
                    try:
                        await self._busy
                    except asyncio.CancelledError:
                        if not self._stop.is_set():
                            raise
                    except Exception:  # noqa: BLE001 - unacked: retried from our pending list
                        log.exception("%s: processing %s failed; will retry", self.agent_id, d.step_id)
                        await asyncio.sleep(1)
                    finally:
                        self._busy = None
        finally:
            live.cancel()
            await agents.clear_alive(self.r, self.keys, self.agent_id)

    async def run_until_idle(self, max_messages: int = 100) -> int:
        """Test helper: process queued messages until none arrive within block_ms."""
        await self.start()
        n = 0
        while n < max_messages:
            got = await self.consumer.next(self.block_ms)
            if not got:
                break
            for d in got:
                await self.process(d)
                n += 1
        return n

    async def _beat(self) -> None:
        await agents.set_alive(self.r, self.keys, self.agent_id, ttl_s=self.liveness_ttl_s,
                               current_step=self._current.get("step_id"), run_id=self._current.get("run_id"),
                               fence=self._current.get("fence"))

    async def _liveness_loop(self) -> None:
        while True:
            await asyncio.sleep(self.liveness_ttl_s / 3)
            try:
                await self._beat()
            except Exception:  # noqa: BLE001 - liveness must never kill the worker
                log.exception("liveness update failed")

    # -- one message ---------------------------------------------------------

    async def process(self, d: bus.Delivery) -> str:
        """Handle one delivery. Returns an outcome label (for logs and tests).
        Acked only when handled; after an error or a stop the entry stays
        pending and is redelivered (duplicates are harmless: the lease and the
        state machine decide)."""
        out = await self._process(d)
        await self.consumer.ack(d)
        return out

    async def _process(self, d: bus.Delivery) -> str:
        step = await ledger.get_step(self.r, self.keys, d.step_id)
        if step is None or step.status != StepStatus.READY or step.skill not in self.skills:
            return "skipped"
        cfg = await ledger.get_run_config(self.r, self.keys, step.run_id)
        ttl_ms = cfg.lease_ttl_s * 1000
        fence = await leases.acquire(self.r, self.keys, step.id, self.agent_id, ttl_ms)
        if fence is None:
            return "busy"
        try:
            step = await ledger.transition(self.r, self.keys, step.id, StepStatus.LEASED, actor=self.agent_id,
                                           actor_role="worker", fence=fence)
        except (ledger.IllegalTransition, ledger.StaleFenceError):
            await leases.release(self.r, self.keys, step.id, self.agent_id)
            return "skipped"

        self._current = {"step_id": step.id, "run_id": step.run_id, "fence": fence}
        await self._beat()
        lost = asyncio.Event()
        work = asyncio.create_task(self._work(step, fence, cfg))
        hb = asyncio.create_task(self._heartbeat(step, ttl_ms, cfg.heartbeat_s, lost, work))
        try:
            return await work
        except asyncio.CancelledError:
            if not lost.is_set():
                raise  # stop() / shutdown: lease released below, the reaper requeues
            return "lease_lost"
        finally:
            hb.cancel()
            await leases.release(self.r, self.keys, step.id, self.agent_id)
            self._current = {}
            await self._beat()

    async def _work(self, step: Step, fence: int, cfg: RunConfig) -> str:
        claim_result = await self._run_handler(step, fence, cfg)
        try:
            await ledger.claim(self.r, self.keys, step.id, Claim(
                worker=self.agent_id, fence=fence, summary=claim_result.summary, data=claim_result.data,
                evidence=claim_result.evidence, acted=claim_result.acted,
            ), model=claim_result.model)
        except (ledger.IllegalTransition, ledger.StaleFenceError) as exc:
            log.warning("%s: claim for %s refused: %s", self.agent_id, step.id, exc)
            return "claim_refused"
        return "claimed"

    async def _run_handler(self, step: Step, fence: int, cfg: RunConfig) -> WorkResult:
        shot = await take_fault_shot(self.r, self.keys, FaultName.FALSE_CLAIM, skill=step.skill.value)
        if shot:
            await append_event(self.r, self.keys, Event(
                run_id=step.run_id, step_id=step.id, actor=self.agent_id, type=EventType.FAULT_INJECTED,
                payload={"fault": FaultName.FALSE_CLAIM.value, "switch": shot, "phase": "consumed",
                         "effect": "handler skipped; claiming success without acting"}))
            return WorkResult(summary="done", data={}, acted=False)
        try:
            ctx = WorkContext(
                r=self.r, keys=self.keys, agent_id=self.agent_id, step=step, fence=fence,
                inputs=await ledger.resolve_inputs(self.r, self.keys, step),
                facts=await ledger.get_facts(self.r, self.keys, step.run_id),
                attempts=step.history[:-1], config=cfg,
            )
            out = await self.handler(step, ctx)
            return out if isinstance(out, WorkResult) else WorkResult.model_validate(out)
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # noqa: BLE001 - becomes a claim the verifier will reject
            log.exception("%s: handler failed on %s", self.agent_id, step.id)
            err = f"{type(exc).__name__}: {exc}"
            try:
                await ledger.observe(self.r, self.keys, step.id, {"error": err}, actor=self.agent_id, fence=fence)
            except ledger.LedgerError:
                pass
            return WorkResult(summary=f"handler error: {err}", data={"error": err}, acted=False)

    async def _heartbeat(self, step: Step, ttl_ms: int, every_s: float, lost: asyncio.Event,
                         work: asyncio.Task) -> None:
        while True:
            await asyncio.sleep(every_s)
            if not await leases.heartbeat(self.r, self.keys, step.id, self.agent_id, ttl_ms):
                lost.set()
                await append_event(self.r, self.keys, Event(
                    run_id=step.run_id, step_id=step.id, actor=self.agent_id, type=EventType.STEP_HEARTBEAT_LOST,
                    payload={"fence": step.fence}))
                work.cancel()
                return


def worker_card(agent_id: str, name: str, skills: dict[str, list[str]], *, side_effects: bool = False,
                tools: list[dict[str, Any]] | None = None, model_role: str | None = None,
                container: str | None = None) -> AgentCard:
    """Small helper to build a worker AgentCard: skills = {skill: [kinds]}."""
    return AgentCard.model_validate({
        "id": agent_id, "name": name, "role": "worker", "model_role": model_role,
        "skills": [{"id": s, "kinds": k} for s, k in skills.items()], "side_effects": side_effects,
        "tools": tools or [], "container": container or agent_id,
    })
