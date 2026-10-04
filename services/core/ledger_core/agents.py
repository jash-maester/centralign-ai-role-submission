"""Agent cards and liveness (plans/01 §3-4, A7).

- `agents` Hash: agent_id -> AgentCard JSON (written once at start, agent.registered).
- `agent:{id}:alive` String with a TTL, refreshed by the agent while it runs.
  Value is JSON: {"ts", "current_step", "run_id", "fence"} so /agents can show
  what each agent holds right now. Key gone = agent lost (the reaper emits
  agent.lost once).
"""

from __future__ import annotations

import json
from typing import Any

import redis.asyncio as aioredis

from .events import append_event
from .keys import Keys
from .protocol import AgentCard, Event, EventType, now_ms

DEFAULT_LIVENESS_TTL_S = 10


async def register_agent(r: aioredis.Redis, keys: Keys, card: AgentCard) -> None:
    await r.hset(keys.agents, card.id, card.model_dump_json())
    await append_event(r, keys, Event(actor=card.id, type=EventType.AGENT_REGISTERED,
                                      payload={"role": card.role, "skills": [s.id.value for s in card.skills],
                                               "container": card.container}))


async def set_alive(
    r: aioredis.Redis, keys: Keys, agent_id: str, *, ttl_s: int = DEFAULT_LIVENESS_TTL_S,
    current_step: str | None = None, run_id: str | None = None, fence: int | None = None,
) -> None:
    value = {"ts": now_ms(), "current_step": current_step, "run_id": run_id, "fence": fence}
    await r.set(keys.agent_alive(agent_id), json.dumps(value), ex=ttl_s)


async def clear_alive(r: aioredis.Redis, keys: Keys, agent_id: str) -> None:
    await r.delete(keys.agent_alive(agent_id))


async def get_liveness(r: aioredis.Redis, keys: Keys, agent_id: str) -> dict[str, Any] | None:
    raw = await r.get(keys.agent_alive(agent_id))
    return json.loads(raw) if raw else None


async def list_agents(r: aioredis.Redis, keys: Keys) -> list[dict[str, Any]]:
    """[{card: AgentCard, alive: bool, current_step, run_id, fence, ts}] sorted by id."""
    cards = await r.hgetall(keys.agents)
    out = []
    for agent_id in sorted(cards):
        live = await get_liveness(r, keys, agent_id)
        out.append({"card": AgentCard.model_validate_json(cards[agent_id]), "alive": live is not None,
                    **(live or {"current_step": None, "run_id": None, "fence": None, "ts": None})})
    return out
