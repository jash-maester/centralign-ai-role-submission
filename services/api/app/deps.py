"""Shared helpers for the API routes: Redis handle, key namespace, run config.

Routes get Redis and Keys from app.state (set in main.lifespan); tests swap
app.state.keys for a unique namespace.
"""

from __future__ import annotations

import json
from typing import Any

import redis.asyncio as aioredis
from fastapi import HTTPException, Request

from ledger_core import ledger, run_config
from ledger_core.config import RunConfig
from ledger_core.keys import Keys
from ledger_core.protocol import Run


def get_r(request: Request) -> aioredis.Redis:
    return request.app.state.redis


def get_keys(request: Request) -> Keys:
    return request.app.state.keys


async def must_run(r: aioredis.Redis, keys: Keys, run_id: str) -> Run:
    run = await ledger.get_run(r, keys, run_id)
    if run is None:
        raise HTTPException(404, f"run {run_id} not found")
    return run


async def read_run_config(r: aioredis.Redis, keys: Keys, run_id: str) -> dict[str, Any]:
    """{config, config_hash, version, stored} for a run, whichever store wrote it.

    run_config.py (Track D) stores a JSON string at Keys.run_config; the CLI's
    ledger.set_run_config (Track A) stores a Hash at the same key. Read both so
    the API never trips over WRONGTYPE; the API itself writes through run_config.
    """
    key = keys.run_config(run_id)
    kind = await r.type(key)
    if kind == "hash":
        cfg = await ledger.get_run_config(r, keys, run_id)
        return {"config": cfg.model_dump(mode="json"), "config_hash": cfg.config_hash(), "version": None,
                "stored": True}
    record = await run_config.get_record(r, keys, run_id) if kind == "string" else None
    if record is None:
        cfg = run_config.defaults()
        return {"config": cfg.model_dump(mode="json"), "config_hash": cfg.config_hash(), "version": 0,
                "stored": False}
    cfg = RunConfig.model_validate(record["config"])
    return {"config": cfg.model_dump(mode="json"), "config_hash": cfg.config_hash(),
            "version": record.get("version"), "stored": True}


async def migrate_hash_config(r: aioredis.Redis, keys: Keys, run_id: str) -> None:
    """Before a run_config.put on a run whose config is a ledger Hash, rewrite
    it in run_config's JSON format (same values) so put() can WATCH/GET it."""
    key = keys.run_config(run_id)
    if await r.type(key) != "hash":
        return
    cfg = await ledger.get_run_config(r, keys, run_id)
    record = {"config": cfg.model_dump(mode="json"), "config_hash": cfg.config_hash(), "version": 1,
              "updated_by": "api-migrate"}
    pipe = r.pipeline(transaction=True)
    pipe.delete(key)
    pipe.set(key, json.dumps(record, sort_keys=True))
    await pipe.execute()
