"""POST /chaos/{fault}: fault injection switches (F1-F5, plans/05).

Sets the switch in Keys.faults and appends fault.injected (phase=injected).
Workers consume a shot when they hit the fault and emit fault.injected with
phase=consumed (worker_base.take_fault_shot).

Body (all optional): {"shots": n | "on", "skill": "...", "agent_id": "...",
"run_id": "...", "kill": bool}.
- false_claim defaults to the next browser step (field false_claim:browser.espocrm);
  pass skill="" for any skill.
- kill_worker records the event; with agent_id it also kills that agent's
  container through the docker socket (only the api has it). `kill: false`
  records only (make chaos-kill-browser kills with `docker compose kill`).
"""

from __future__ import annotations

import asyncio
from typing import Any

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel

from ledger_core.events import append_event
from ledger_core.keys import Keys
from ledger_core.protocol import AgentCard, Event, EventType, FaultName, Skill

from .. import docker_ops
from ..deps import get_keys, get_r

router = APIRouter(tags=["chaos"])

DEFAULT_SKILL: dict[FaultName, str | None] = {
    FaultName.FALSE_CLAIM: Skill.BROWSER_ESPOCRM.value,
}


class FaultBody(BaseModel):
    shots: int | str = 1
    skill: str | None = None
    agent_id: str | None = None
    run_id: str | None = None
    kill: bool = True


def _field(fault: FaultName, skill: str | None) -> str:
    return f"{fault.value}:{skill}" if skill else fault.value


async def _latest_running_run(r, keys: Keys) -> str | None:
    ids = await r.zrevrange(keys.runs, 0, 0)
    return ids[0] if ids else None


@router.get("/chaos")
async def list_faults(r=Depends(get_r), keys: Keys = Depends(get_keys)) -> dict[str, Any]:
    """Armed switches: field -> remaining shots or "on"."""
    return {"faults": [f.value for f in FaultName], "armed": await r.hgetall(keys.faults)}


@router.delete("/chaos/{fault}")
async def clear_fault(fault: FaultName, r=Depends(get_r), keys: Keys = Depends(get_keys)) -> dict[str, Any]:
    armed = await r.hgetall(keys.faults)
    gone = [f for f in armed if f == fault.value or f.startswith(f"{fault.value}:")]
    if gone:
        await r.hdel(keys.faults, *gone)
    return {"cleared": gone}


async def _leases_held(r, keys: Keys, agent_id: str, run_id: str | None) -> list[str]:
    if not run_id:
        return []
    from ledger_core import ledger, leases
    from ledger_core.protocol import StepStatus

    out = []
    for st in await ledger.list_steps(r, keys, run_id):
        if st.status == StepStatus.LEASED and await leases.holder(r, keys, st.id) == agent_id:
            out.append(st.id)
    return out


@router.post("/chaos/{fault}")
async def inject(fault: FaultName, body: FaultBody | None = None, r=Depends(get_r),
                 keys: Keys = Depends(get_keys)) -> dict[str, Any]:
    body = body or FaultBody()
    shots = body.shots
    if isinstance(shots, str) and shots != "on":
        raise HTTPException(422, "shots must be a positive integer or 'on'")
    if isinstance(shots, int) and shots < 1:
        raise HTTPException(422, "shots must be >= 1")
    run_id = body.run_id or await _latest_running_run(r, keys)
    payload: dict[str, Any] = {"fault": fault.value, "phase": "injected"}
    result: dict[str, Any] = {"fault": fault.value, "run_id": run_id}

    if fault == FaultName.KILL_WORKER:
        if not body.agent_id:
            raise HTTPException(422, "kill_worker needs agent_id")
        card_raw = await r.hget(keys.agents, body.agent_id)
        service = AgentCard.model_validate_json(card_raw).container if card_raw else None
        service = service or body.agent_id
        live = await r.get(keys.agent_alive(body.agent_id))
        # authoritative: the leases this agent holds right now (the liveness
        # heartbeat's current_step can lag one step behind)
        held_steps = await _leases_held(r, keys, body.agent_id, run_id)
        payload.update(agent_id=body.agent_id, container=service, held=live, held_steps=held_steps)
        if body.kill:
            try:
                result["kill"] = await asyncio.to_thread(docker_ops.kill, service)
            except (docker_ops.DockerUnavailable, docker_ops.ContainerNotFound) as exc:
                result["kill"] = {"error": str(exc)}
        payload["kill"] = result.get("kill", "recorded only")
    else:
        skill = body.skill if body.skill is not None else DEFAULT_SKILL.get(fault)
        field = _field(fault, skill)
        await r.hset(keys.faults, field, str(shots))
        payload.update(switch=field, shots=shots)
        result.update(switch=field, shots=shots)

    ev = Event(run_id=run_id, actor="chaos", type=EventType.FAULT_INJECTED, payload=payload)
    result["event_id"] = await append_event(r, keys, ev)
    return result
