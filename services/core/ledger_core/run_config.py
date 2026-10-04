"""Per-run configuration store (plans/01-architecture.md §6a, F6, F7).

Defaults = playbook front matter over env (config.defaults_from). Stored per
run at Keys.run_config(run_id) as JSON:

    {"config": {...RunConfig...}, "config_hash": "<sha256>", "version": n, "updated_by": "..."}

Every change emits run.config_updated with the diff ({key: [old, new]}) and
the new hash. Changes apply from the next attempt: services read the config
when they start an attempt (llm.complete loads it by run_id when no config is
passed), never mid-attempt.
"""

from __future__ import annotations

import json
from typing import Any

import redis.asyncio as aioredis
from pydantic import ValidationError

from .config import RunConfig, defaults_from
from .events import append_event
from .keys import Keys
from .protocol import Event, EventType
from .settings import Settings, get_settings

# Env settings that seed RunConfig before the playbook front matter.
ENV_KEYS = ("review_auto_threshold", "approval_auto_threshold", "determinism")


class RunConfigError(ValueError):
    pass


def env_defaults(settings: Settings | None = None) -> dict[str, Any]:
    s = settings or get_settings()
    return {k: getattr(s, k) for k in ENV_KEYS if hasattr(s, k)}


def defaults(playbook: Any = None, *, settings: Settings | None = None) -> RunConfig:
    """Playbook defaults over env. `playbook` is a playbook.Playbook, a front-matter dict or None."""
    front = getattr(playbook, "front_matter", playbook)
    return defaults_from(front if isinstance(front, dict) else None, env_defaults(settings))


def diff(old: RunConfig, new: RunConfig) -> dict[str, list[Any]]:
    a, b = old.model_dump(mode="json"), new.model_dump(mode="json")
    return {k: [a.get(k), b[k]] for k in b if a.get(k) != b[k]}


async def get_record(r: aioredis.Redis, keys: Keys, run_id: str) -> dict[str, Any] | None:
    if await r.type(keys.run_config(run_id)) in ("hash", b"hash"):  # ledger.set_run_config (Track A format)
        from .ledger import get_run_config

        cfg = await get_run_config(r, keys, run_id)
        return {"config": cfg.model_dump(mode="json"), "config_hash": cfg.config_hash(), "version": 0,
                "updated_by": "ledger"}
    raw = await r.get(keys.run_config(run_id))
    return json.loads(raw) if raw else None


async def get(r: aioredis.Redis, keys: Keys, run_id: str, *, playbook: Any = None) -> RunConfig:
    """Stored config for the run, else the defaults (not persisted)."""
    record = await get_record(r, keys, run_id)
    if record is None:
        return defaults(playbook)
    return RunConfig.model_validate(record["config"])


def _record(cfg: RunConfig, version: int, actor: str) -> str:
    return json.dumps({"config": cfg.model_dump(mode="json"), "config_hash": cfg.config_hash(),
                       "version": version, "updated_by": actor}, sort_keys=True)


async def init(r: aioredis.Redis, keys: Keys, run_id: str, cfg: RunConfig | None = None, *,
               playbook: Any = None, actor: str = "api") -> RunConfig:
    """Store the run's starting config if none exists yet. Returns the stored config."""
    cfg = cfg or defaults(playbook)
    created = await r.set(keys.run_config(run_id), _record(cfg, 1, actor), nx=True)
    if not created:
        return await get(r, keys, run_id)
    await append_event(r, keys, Event(actor=actor, type=EventType.RUN_CONFIG_UPDATED, run_id=run_id, payload={
        "initial": True, "version": 1, "config_hash": cfg.config_hash(), "config": cfg.model_dump(mode="json"),
    }))
    return cfg


async def put(r: aioredis.Redis, keys: Keys, run_id: str, patch: dict[str, Any] | RunConfig, *,
              playbook: Any = None, actor: str = "api", reason: str | None = None) -> RunConfig:
    """Merge `patch` onto the run's config, validate, store, emit the diff.

    Unknown keys raise RunConfigError (a PUT with a typo must not be ignored
    silently). No event when nothing changed.
    """
    if isinstance(patch, RunConfig):
        patch = patch.model_dump(mode="json")
    unknown = sorted(set(patch) - set(RunConfig.model_fields))
    if unknown:
        raise RunConfigError(f"unknown run config keys: {', '.join(unknown)}")
    key = keys.run_config(run_id)
    while True:
        async with r.pipeline(transaction=True) as pipe:
            try:
                await pipe.watch(key)
                raw = await pipe.get(key)
                record = json.loads(raw) if raw else None
                old = RunConfig.model_validate(record["config"]) if record else defaults(playbook)
                try:
                    new = RunConfig.model_validate({**old.model_dump(mode="json"), **patch})
                except ValidationError as exc:
                    raise RunConfigError(str(exc)) from exc
                changes = diff(old, new)
                if record is not None and not changes:
                    return old
                version = (record or {}).get("version", 0) + 1
                pipe.multi()
                pipe.set(key, _record(new, version, actor))
                await pipe.execute()
                break
            except aioredis.WatchError:
                continue
    payload: dict[str, Any] = {"diff": changes, "version": version, "config_hash": new.config_hash()}
    if reason:
        payload["reason"] = reason
    await append_event(r, keys, Event(actor=actor, type=EventType.RUN_CONFIG_UPDATED, run_id=run_id, payload=payload))
    return new


async def reset_to_playbook_defaults(r: aioredis.Redis, keys: Keys, run_id: str, playbook: Any, *,
                                     actor: str = "api") -> RunConfig:
    """H13 "Reset to playbook defaults"."""
    return await put(r, keys, run_id, defaults(playbook), actor=actor, reason="reset_to_playbook_defaults")


async def set_determinism(r: aioredis.Redis, keys: Keys, run_id: str, level: float, seed: int | None = None, *,
                          actor: str = "api") -> RunConfig:
    """F6: one slider sets per-role temperatures (+ seed). Emits run.config_updated and config.updated."""
    patch: dict[str, Any] = {"determinism": level}
    if seed is not None:
        patch["seed"] = seed
    cfg = await put(r, keys, run_id, patch, actor=actor, reason="determinism")
    await r.set(keys.run_determinism(run_id), str(cfg.determinism))
    temps = {role: cfg.temperature(role) for role in ("orchestrator", "worker", "meta_reviewer", "verifier")}
    await append_event(r, keys, Event(actor=actor, type=EventType.CONFIG_UPDATED, run_id=run_id, payload={
        "scope": "run", "key": "determinism", "determinism": cfg.determinism, "seed": cfg.seed,
        "seed_pinned": cfg.seed_pinned, "temperatures": temps, "config_hash": cfg.config_hash(),
    }))
    return cfg
