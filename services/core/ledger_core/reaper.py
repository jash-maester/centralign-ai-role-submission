"""Reaper: self-healing for dead workers (plans/01 §5, A3, A7).

    await run_reaper(r, keys, interval_s=2)     # forever; the orchestrator runs it
    await reap_once(r, keys)                    # one sweep (tests, CLI)
    python -m ledger_core.reaper                # standalone

Each sweep:
- every step in Keys.leased_steps whose lease key is gone (holder died or
  was SIGKILLed, or stopped gracefully and released it) -> lease_expired
  (step.lease_expired) -> ready (requeued on its skill queue). A step whose
  leases have expired max_attempts times goes to dead instead, so a step that
  crashes every worker cannot loop forever.
- every agent in Keys.agents whose liveness key expired -> agent.lost
  (once, until the agent comes back).
"""

from __future__ import annotations

import asyncio
import logging

import redis.asyncio as aioredis

from . import ledger
from .events import append_event
from .keys import Keys
from .protocol import Event, EventType, StepStatus

log = logging.getLogger("ledger.reaper")
ACTOR = "reaper"


async def reap_steps(r: aioredis.Redis, keys: Keys, *, actor: str = ACTOR) -> list[str]:
    requeued = []
    for step_id in await r.smembers(keys.leased_steps):
        if await r.exists(keys.lease(step_id)):
            continue
        step = await ledger.get_step(r, keys, step_id)
        if step is None or step.status != StepStatus.LEASED:
            await r.srem(keys.leased_steps, step_id)
            continue
        if await r.exists(keys.lease(step_id)):  # re-check: a worker may have just leased it
            continue
        try:
            step = await ledger.expire_lease(r, keys, step_id, actor=actor)
        except ledger.IllegalTransition:
            continue  # the worker claimed in the meantime
        if step.status == StepStatus.READY:
            requeued.append(step_id)
    return requeued


class AgentWatch:
    """Remembers which agents were already reported lost (one agent.lost each)."""

    def __init__(self) -> None:
        self.lost: set[str] = set()

    async def sweep(self, r: aioredis.Redis, keys: Keys, *, actor: str = ACTOR) -> list[str]:
        newly_lost = []
        for agent_id in await r.hkeys(keys.agents):
            alive = await r.exists(keys.agent_alive(agent_id))
            if alive:
                self.lost.discard(agent_id)
            elif agent_id not in self.lost:
                self.lost.add(agent_id)
                newly_lost.append(agent_id)
                await append_event(r, keys, Event(actor=actor, type=EventType.AGENT_LOST,
                                                  payload={"agent_id": agent_id}))
        return newly_lost


async def reap_once(r: aioredis.Redis, keys: Keys, watch: AgentWatch | None = None) -> dict[str, list[str]]:
    requeued = await reap_steps(r, keys)
    lost = await (watch or AgentWatch()).sweep(r, keys)
    return {"requeued": requeued, "agents_lost": lost}


async def run_reaper(r: aioredis.Redis, keys: Keys, interval_s: float = 2.0) -> None:
    watch = AgentWatch()
    while True:
        try:
            res = await reap_once(r, keys, watch)
            if res["requeued"] or res["agents_lost"]:
                log.info("reaper: %s", res)
        except asyncio.CancelledError:
            raise
        except Exception:  # noqa: BLE001 - keep sweeping
            log.exception("reaper sweep failed")
        await asyncio.sleep(interval_s)


def main() -> None:
    from .redis_conn import connect
    from .settings import get_settings

    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(name)s %(message)s")
    asyncio.run(run_reaper(connect(), Keys(get_settings().ledger_ns)))


if __name__ == "__main__":
    main()
