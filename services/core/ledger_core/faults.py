"""Fault switches (plans/02 F1-F5): the one module that reads and writes Keys.faults.

    await faults.set_fault(r, keys, FaultName.FALSE_CLAIM, shots=1, scope="api.espocrm", actor="api")
    field = await faults.consume(r, keys, FaultName.FALSE_CLAIM, scope="api.espocrm")   # or None
    await faults.clear(r, keys, FaultName.FALSE_CLAIM)                                   # every scope
    await faults.active(r, keys)                                                          # {field: value}

Storage (Keys.faults, a Hash): field "<fault>" (global) or "<fault>:<scope>"
(scoped, e.g. "false_claim:browser.espocrm" or "expire_session:worker-browser-1");
value "on" (until cleared) or a remaining shot count. consume() checks the
scoped fields before the global one and takes one shot atomically (Lua), so
two workers never both consume the last shot.

Every set/clear appends fault.injected with phase=set|cleared and who/what;
consumers record the moment a fault fires with `record_consumed` (phase=consumed).

Who reads which switch (all through consume()):
- false_claim     worker_base: the handler is skipped and success is claimed (F2)
- model_outage    llm_openrouter: the primary worker model id is made invalid (F4)
- expire_session  browser worker: Operator.expire_session() before the skill (F3)
- ui_changed      browser worker: selectors.set_ui_changed(True) (F5)
- kill_worker     never consumed; recorded for the timeline (the kill is docker)
"""

from __future__ import annotations

import json
from typing import Any

import redis.asyncio as aioredis

from .events import append_event
from .keys import Keys
from .protocol import Event, EventType, FaultName, now_ms

DESCRIPTIONS: dict[FaultName, str] = {
    FaultName.FALSE_CLAIM: "next worker step claims done without acting; the verifier must reject it",
    FaultName.KILL_WORKER: "the worker holding a lease is killed; its lease expires and another worker takes over",
    FaultName.EXPIRE_SESSION: "the CRM browser session is invalidated; the operator must log in again",
    FaultName.MODEL_OUTAGE: "the primary worker model id is made invalid; the LLM layer falls back",
    FaultName.UI_CHANGED: "a CRM UI selector is broken; the browser skill gives up and the step is replanned",
}

# Atomically take one shot from the first set field in ARGV order.
# "on" is never consumed; a count is decremented (the field removed at 0).
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
    if n and n <= 0 then redis.call('HDEL', KEYS[1], f) end
  end
end
return false
"""


def field_name(fault: FaultName | str, scope: str | None = None) -> str:
    fault = FaultName(fault)
    return f"{fault.value}:{scope}" if scope else fault.value


def _value(shots: int | None) -> str:
    if shots is None:
        return "on"
    if shots < 1:
        raise ValueError("shots must be >= 1 (or None for 'on')")
    return str(int(shots))


async def set_fault(
    r: aioredis.Redis, keys: Keys, fault: FaultName | str, *, shots: int | None = 1, scope: str | None = None,
    actor: str = "api", run_id: str | None = None, target: str | None = None, extra: dict[str, Any] | None = None,
) -> str:
    """Arm a fault switch and append fault.injected (phase=set). shots=None means
    "on" until cleared. `target` names who it is aimed at (agent id, skill) for
    the timeline. Returns the hash field that was set."""
    fault = FaultName(fault)
    f = field_name(fault, scope)
    value = _value(shots)
    await r.hset(keys.faults, f, value)
    await append_event(r, keys, Event(
        run_id=run_id, actor=actor, type=EventType.FAULT_INJECTED,
        payload={"fault": fault.value, "phase": "set", "switch": f, "value": value, "scope": scope,
                 "target": target or scope, "by": actor, "what": DESCRIPTIONS[fault], **(extra or {})},
    ))
    return f


async def consume(r: aioredis.Redis, keys: Keys, fault: FaultName | str, *, scope: str | list[str] | None = None) -> str | None:
    """Take one shot of `fault` if armed: scoped field(s) first, then the global
    one. Returns the field consumed, or None. Emits nothing (the consumer knows
    the run/step; call record_consumed)."""
    fault = FaultName(fault)
    scopes = [scope] if isinstance(scope, str) else list(scope or [])
    fields = [field_name(fault, s) for s in scopes if s] + [fault.value]
    return await r.eval(_TAKE_SHOT, 1, keys.faults, *fields) or None


async def mark_pending(r: aioredis.Redis, keys: Keys, fault: FaultName | str, *, event_id: str | None,
                       payload: dict[str, Any], actor: str = "chaos") -> None:
    """A fault injected while no run was running (POST /chaos/{fault} without
    run_id): remember it so the next run that consumes it gets it on its timeline."""
    fault = FaultName(fault)
    await r.hset(keys.faults_pending, fault.value, json.dumps(
        {"event_id": event_id, "ts": now_ms(), "actor": actor, "payload": payload}, sort_keys=True))


async def attach_pending(r: aioredis.Redis, keys: Keys, fault: FaultName | str, *, run_id: str | None,
                         step_id: str | None = None) -> str | None:
    """The first run that consumes a pending fault owns it: append its
    fault.injected (phase=injected, attached=true) to that run, just before the
    consumer's phase=consumed event. Returns the event id, or None."""
    if not run_id:
        return None
    fault = FaultName(fault)
    raw = await r.hget(keys.faults_pending, fault.value)
    if not raw or not await r.hdel(keys.faults_pending, fault.value):  # HDEL: exactly one consumer attaches
        return None
    try:
        rec = json.loads(raw)
    except ValueError:
        rec = {}
    payload = {**(rec.get("payload") or {}), "fault": fault.value, "phase": "injected", "pending": False,
               "attached": True, "pending_since": rec.get("ts"), "pending_event": rec.get("event_id")}
    return await append_event(r, keys, Event(run_id=run_id, step_id=step_id, actor=rec.get("actor") or "chaos",
                                             type=EventType.FAULT_INJECTED, payload=payload))


async def record_consumed(
    r: aioredis.Redis, keys: Keys, fault: FaultName | str, switch: str, *, actor: str,
    run_id: str | None = None, step_id: str | None = None, effect: str = "", **payload: Any,
) -> str:
    """Append fault.injected (phase=consumed): the moment the fault took effect
    (after attaching a pending injection to this run, if there is one)."""
    fault = FaultName(fault)
    await attach_pending(r, keys, fault, run_id=run_id, step_id=step_id)
    return await append_event(r, keys, Event(
        run_id=run_id, step_id=step_id, actor=actor, type=EventType.FAULT_INJECTED,
        payload={"fault": fault.value, "switch": switch, "phase": "consumed",
                 "effect": effect or DESCRIPTIONS[fault], **payload},
    ))


async def consume_and_record(
    r: aioredis.Redis, keys: Keys, fault: FaultName | str, *, actor: str, scope: str | list[str] | None = None,
    run_id: str | None = None, step_id: str | None = None, effect: str = "", **payload: Any,
) -> str | None:
    """consume() + record_consumed() when a shot was taken."""
    shot = await consume(r, keys, fault, scope=scope)
    if shot:
        await record_consumed(r, keys, fault, shot, actor=actor, run_id=run_id, step_id=step_id,
                              effect=effect, **payload)
    return shot


async def is_armed(r: aioredis.Redis, keys: Keys, fault: FaultName | str, *, scope: str | None = None) -> bool:
    """Peek without consuming (scoped field or the global one)."""
    fields = ([field_name(fault, scope)] if scope else []) + [FaultName(fault).value]
    for v in await r.hmget(keys.faults, fields):
        if v is not None and (v == "on" or (v.lstrip("-").isdigit() and int(v) > 0)):
            return True
    return False


async def active(r: aioredis.Redis, keys: Keys) -> dict[str, str]:
    """Every armed switch: {field: "on" | shots}."""
    return dict(await r.hgetall(keys.faults))


async def clear(
    r: aioredis.Redis, keys: Keys, fault: FaultName | str | None = None, *, scope: str | None = None,
    actor: str = "api", run_id: str | None = None,
) -> list[str]:
    """Disarm switches: one field (fault + scope), every scope of a fault
    (scope=None), or everything (fault=None). Appends fault.injected
    (phase=cleared) per fault that had a field removed."""
    current = await r.hkeys(keys.faults)
    if fault is None:
        doomed = list(current)
    elif scope is not None:
        doomed = [f for f in current if f == field_name(fault, scope)]
    else:
        base = FaultName(fault).value
        doomed = [f for f in current if f == base or f.startswith(base + ":")]
    if not doomed:
        return []
    await r.hdel(keys.faults, *doomed)
    await r.hdel(keys.faults_pending, *{f.split(":", 1)[0] for f in doomed})
    by_fault: dict[str, list[str]] = {}
    for f in doomed:
        by_fault.setdefault(f.split(":", 1)[0], []).append(f)
    for name, fields in by_fault.items():
        await append_event(r, keys, Event(
            run_id=run_id, actor=actor, type=EventType.FAULT_INJECTED,
            payload={"fault": name, "phase": "cleared", "switches": fields, "by": actor},
        ))
    return doomed


# ---------------------------------------------------------------------------
# CLI (until the API's /chaos routes exist):
#   python -m ledger_core.faults set false_claim --scope api.espocrm --shots 1
#   python -m ledger_core.faults clear [false_claim]      python -m ledger_core.faults list
# ---------------------------------------------------------------------------


async def _main(argv: list[str] | None = None) -> int:
    import argparse

    from .redis_conn import connect
    from .settings import get_settings

    p = argparse.ArgumentParser(prog="python -m ledger_core.faults")
    sub = p.add_subparsers(dest="cmd", required=True)
    s = sub.add_parser("set")
    s.add_argument("fault", choices=[f.value for f in FaultName])
    s.add_argument("--scope")
    s.add_argument("--shots", type=int, default=1, help="0 = 'on' until cleared")
    s.add_argument("--run")
    c = sub.add_parser("clear")
    c.add_argument("fault", nargs="?", choices=[f.value for f in FaultName])
    c.add_argument("--scope")
    sub.add_parser("list")
    a = p.parse_args(argv)
    r = connect()
    keys = Keys(get_settings().ledger_ns)
    try:
        if a.cmd == "set":
            f = await set_fault(r, keys, a.fault, shots=a.shots or None, scope=a.scope, actor="cli", run_id=a.run)
            print(f"armed {f} = {await r.hget(keys.faults, f)}")
        elif a.cmd == "clear":
            print("cleared", " ".join(await clear(r, keys, a.fault, scope=a.scope, actor="cli")) or "nothing")
        else:
            for k, v in sorted((await active(r, keys)).items()):
                print(f"{k} = {v}")
    finally:
        await r.aclose()
    return 0


if __name__ == "__main__":
    import asyncio
    import sys

    sys.exit(asyncio.run(_main()))
