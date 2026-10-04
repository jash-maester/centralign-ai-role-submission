"""POST /chaos/{fault}: fault injection switches (F1-F5, plans/05).

Sets the switch in Keys.faults and appends fault.injected (phase=injected).
Workers consume a shot when they hit the fault and emit fault.injected with
phase=consumed (worker_base.take_fault_shot).

Body (all optional): {"shots": n | "on", "skill": "...", "agent_id": "...",
"run_id": "...", "kill": bool}; `?run_id=` works too (query wins).

Which run the injection belongs to (Track N): the given run_id; else the most
recent run that is in flight (created / understanding / planning / running;
never one that is completed_pending_input or finished); else none: the switch
is armed and the injection recorded as pending (Keys.faults_pending), and the
first run whose worker consumes the shot gets the injection on its timeline
(faults.attach_pending, fault.injected phase=injected attached=true).
- false_claim defaults to the next browser step (field false_claim:browser.espocrm);
  pass skill="" for any skill.
- kill_worker records the event; with agent_id it also kills that agent's
  container through the docker socket (only the api has it). `kill: false`
  records only (make chaos-kill-browser kills with `docker compose kill`).
"""

from __future__ import annotations

import asyncio
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel

from ledger_core import faults, ledger
from ledger_core.events import append_event
from ledger_core.keys import Keys
from ledger_core.protocol import AgentCard, Event, EventType, FaultName, RunStatus, Skill

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


IN_FLIGHT = frozenset({RunStatus.CREATED, RunStatus.UNDERSTANDING, RunStatus.PLANNING, RunStatus.RUNNING})


async def _latest_running_run(r, keys: Keys) -> str | None:
    """The most recent run still in flight (a completed_pending_input run only
    waits on a human: a fault injected now would hit the next run instead)."""
    for run in await ledger.list_runs(r, keys, limit=50):
        if run.status in IN_FLIGHT:
            return run.id
    return None


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
    await r.hdel(keys.faults_pending, fault.value)
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
async def inject(fault: FaultName, body: FaultBody | None = None, run_id: str | None = Query(None),
                 r=Depends(get_r), keys: Keys = Depends(get_keys)) -> dict[str, Any]:
    body = body or FaultBody()
    shots = body.shots
    if isinstance(shots, str) and shots != "on":
        raise HTTPException(422, "shots must be a positive integer or 'on'")
    if isinstance(shots, int) and shots < 1:
        raise HTTPException(422, "shots must be >= 1")
    explicit = run_id or body.run_id
    if explicit and await ledger.get_run(r, keys, explicit) is None:
        raise HTTPException(404, f"run {explicit} not found")
    run_id = explicit or await _latest_running_run(r, keys)
    pending = run_id is None and fault != FaultName.KILL_WORKER
    payload: dict[str, Any] = {"fault": fault.value, "phase": "injected", "pending": pending}
    result: dict[str, Any] = {"fault": fault.value, "run_id": run_id, "pending": pending}

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
    if pending:  # no run in flight: the next run that consumes the shot gets it
        await faults.mark_pending(r, keys, fault, event_id=result["event_id"], payload=payload)
    return result
