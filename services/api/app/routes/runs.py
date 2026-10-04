"""Runs, steps, events, facts, run config, determinism, replay (plans/01 §10).

POST /runs creates the run in the ledger (run.created + starting run config);
the orchestrator picks it up from there. A body that carries a hand-written
`steps` plan (the CLI's run-spec format) is stored and released directly, so
`curl` can drive a run without the planner.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel, Field

from ledger_core import ledger, playbook, run_config
from ledger_core.cli import build_steps
from ledger_core.config import RunConfig
from ledger_core.events import read_events
from ledger_core.keys import Keys
from ledger_core.protocol import Event, Run, RunStatus, StepStatus
from ledger_core.settings import get_settings
from ledger_core.workers.parser import file_sha256

from ..deps import get_keys, get_r, migrate_hash_config, must_run, read_run_config

router = APIRouter(tags=["runs"])

ACTOR = "api"
_PAGE_MAX = 1000


class NewRun(BaseModel):
    goal: str = Field(min_length=1)
    input_file: str | None = None  # file name under DATA_DIR
    playbook: str = "event-leads.md"
    config: dict[str, Any] = Field(default_factory=dict)  # RunConfig overrides
    steps: list[dict[str, Any]] | None = None  # optional hand-written plan (cli run-spec format)


class DeterminismBody(BaseModel):
    level: float = Field(ge=0, le=1)
    seed: int | None = None


class ReplayBody(BaseModel):
    determinism: float | None = Field(default=None, ge=0, le=1)
    seed: int | None = None


# ---- helpers -------------------------------------------------------------------


def data_file(name: str) -> Path | None:
    """Resolve `name` under DATA_DIR (or the test mount /repo/data); never outside it."""
    for base in (Path(get_settings().data_dir), Path("/repo/data")):
        base = base.resolve()
        path = (base / name).resolve()
        if base not in path.parents:
            raise HTTPException(422, f"input_file escapes DATA_DIR: {name!r}")
        if path.is_file():
            return path
    return None


def load_playbook(name: str) -> playbook.Playbook | None:
    for base in (None, "/repo/playbooks"):
        try:
            return playbook.load(name, base)
        except playbook.PlaybookError:
            continue
    return None


async def _status_counts(r, keys: Keys, run_id: str) -> dict[str, int]:
    ids = await r.lrange(keys.run_steps(run_id), 0, -1)
    if not ids:
        return {}
    pipe = r.pipeline(transaction=False)
    for sid in ids:
        pipe.hget(keys.step(sid), "status")
    counts: dict[str, int] = {}
    for st in await pipe.execute():
        if st:
            counts[st] = counts.get(st, 0) + 1
    return counts


async def run_summary(r, keys: Keys, run: Run) -> dict[str, Any]:
    counts = await _status_counts(r, keys, run.id)
    return {**run.model_dump(mode="json"), "steps_total": sum(counts.values()),
            "steps_committed": counts.get(StepStatus.COMMITTED.value, 0), "step_counts": counts}


async def create_run(r, keys: Keys, body: NewRun, *, replay_of: str | None = None,
                     cfg: RunConfig | None = None) -> Run:
    run = Run(goal=body.goal, input_file=body.input_file, playbook=body.playbook, replay_of=replay_of)
    if body.input_file:
        path = data_file(body.input_file)
        if path is None:
            raise HTTPException(422, f"input_file {body.input_file!r} not found under DATA_DIR")
        run.input_sha256 = file_sha256(path)
    pb = load_playbook(body.playbook)
    if pb is not None:
        run.playbook_hash = pb.content_hash
    if cfg is None:
        unknown = set(body.config) - set(RunConfig.model_fields)
        if unknown:
            raise HTTPException(422, f"unknown run config keys: {', '.join(sorted(unknown))}")
        base = run_config.defaults(pb)
        try:
            cfg = RunConfig.model_validate({**base.model_dump(mode="json"), **body.config})
        except ValueError as exc:
            raise HTTPException(422, f"invalid config: {exc}") from exc
    run.config_hash = cfg.config_hash()
    await ledger.create_run(r, keys, run, actor=ACTOR)
    await run_config.init(r, keys, run.id, cfg, actor=ACTOR)
    if body.steps:
        try:
            steps = build_steps({"steps": body.steps}, run.id, cfg)
        except (KeyError, ValueError) as exc:
            raise HTTPException(422, f"invalid steps: {exc}") from exc
        await ledger.create_steps(r, keys, steps, actor=ACTOR, payload={"source": "hand-written plan (API)"})
        await ledger.set_run_status(r, keys, run.id, RunStatus.RUNNING, actor=ACTOR)
        await ledger.release_dependents(r, keys, run.id, actor=ACTOR)
    return await ledger.get_run(r, keys, run.id) or run


# ---- runs ------------------------------------------------------------------------


@router.post("/runs", status_code=201)
async def post_run(body: NewRun, r=Depends(get_r), keys: Keys = Depends(get_keys)) -> dict[str, Any]:
    run = await create_run(r, keys, body)
    return await run_summary(r, keys, run)


@router.get("/runs")
async def get_runs(limit: int = Query(50, ge=1, le=500), r=Depends(get_r),
                   keys: Keys = Depends(get_keys)) -> list[dict[str, Any]]:
    return [await run_summary(r, keys, run) for run in await ledger.list_runs(r, keys, limit)]


@router.get("/runs/{run_id}")
async def get_run(run_id: str, r=Depends(get_r), keys: Keys = Depends(get_keys)) -> dict[str, Any]:
    run = await must_run(r, keys, run_id)
    out = await run_summary(r, keys, run)
    cfg = await read_run_config(r, keys, run_id)
    out["current_config_hash"] = cfg["config_hash"]
    return out


@router.get("/runs/{run_id}/steps")
async def get_steps(run_id: str, r=Depends(get_r), keys: Keys = Depends(get_keys)) -> list[dict[str, Any]]:
    await must_run(r, keys, run_id)
    return [s.model_dump(mode="json") for s in await ledger.list_steps(r, keys, run_id)]


@router.get("/runs/{run_id}/steps/{step_id}")
async def get_step(run_id: str, step_id: str, r=Depends(get_r), keys: Keys = Depends(get_keys)) -> dict[str, Any]:
    step = await ledger.get_step(r, keys, step_id)
    if step is None or step.run_id != run_id:
        raise HTTPException(404, f"step {step_id} not found in run {run_id}")
    events = [e for e in await ledger.run_events(r, keys, run_id) if e.step_id == step_id]
    ttl = await r.pttl(keys.lease(step_id))
    return {**step.model_dump(mode="json"),
            "lease_ttl_ms": ttl if ttl and ttl > 0 else None,
            "lease_holder": await r.get(keys.lease(step_id)),
            "events": [e.model_dump(mode="json") for e in events]}


@router.get("/runs/{run_id}/events")
async def get_events(
    run_id: str, after: str | None = Query(None, description="stream id; return events after it"),
    limit: int = Query(200, ge=1, le=_PAGE_MAX), type: list[str] | None = Query(None),
    r=Depends(get_r), keys: Keys = Depends(get_keys),
) -> dict[str, Any]:
    """Oldest first. `next` is the id to pass as `after` for the next page (null at the end)."""
    await must_run(r, keys, run_id)
    out: list[Event] = []
    cursor = after or "-"
    exhausted = False
    while len(out) < limit:
        batch = await read_events(r, keys, after=cursor, count=_PAGE_MAX)
        if not batch:
            exhausted = True
            break
        for ev in batch:
            cursor = ev.id or cursor
            if ev.run_id == run_id and (not type or ev.type.value in type):
                out.append(ev)
                if len(out) >= limit:
                    break
    return {"events": [e.model_dump(mode="json") for e in out],
            "next": None if exhausted or not out else out[-1].id}


@router.get("/runs/{run_id}/facts")
async def get_facts(run_id: str, r=Depends(get_r), keys: Keys = Depends(get_keys)) -> dict[str, Any]:
    await must_run(r, keys, run_id)
    records = await ledger.get_fact_records(r, keys, run_id)
    return {k: v.model_dump(mode="json") for k, v in sorted(records.items())}


# ---- run config ------------------------------------------------------------------


@router.get("/runs/{run_id}/config")
async def get_config(run_id: str, r=Depends(get_r), keys: Keys = Depends(get_keys)) -> dict[str, Any]:
    await must_run(r, keys, run_id)
    return await read_run_config(r, keys, run_id)


@router.put("/runs/{run_id}/config")
async def put_config(run_id: str, patch: dict[str, Any], r=Depends(get_r),
                     keys: Keys = Depends(get_keys)) -> dict[str, Any]:
    run = await must_run(r, keys, run_id)
    patch = patch.get("config", patch) if isinstance(patch.get("config"), dict) else patch
    patch = {k: v for k, v in patch.items() if k != "config_hash"}
    await migrate_hash_config(r, keys, run_id)
    try:
        await run_config.put(r, keys, run_id, patch, playbook=load_playbook(run.playbook), actor=ACTOR)
    except run_config.RunConfigError as exc:
        raise HTTPException(422, str(exc)) from exc
    return await read_run_config(r, keys, run_id)


@router.post("/runs/{run_id}/config/reset")
async def reset_config(run_id: str, r=Depends(get_r), keys: Keys = Depends(get_keys)) -> dict[str, Any]:
    """H13 "Reset to playbook defaults"."""
    run = await must_run(r, keys, run_id)
    await migrate_hash_config(r, keys, run_id)
    await run_config.reset_to_playbook_defaults(r, keys, run_id, load_playbook(run.playbook), actor=ACTOR)
    return await read_run_config(r, keys, run_id)


@router.post("/runs/{run_id}/determinism")
async def post_determinism(run_id: str, body: DeterminismBody, r=Depends(get_r),
                           keys: Keys = Depends(get_keys)) -> dict[str, Any]:
    await must_run(r, keys, run_id)
    await migrate_hash_config(r, keys, run_id)
    cfg = await run_config.set_determinism(r, keys, run_id, body.level, body.seed, actor=ACTOR)
    out = await read_run_config(r, keys, run_id)
    out["temperatures"] = {role: cfg.temperature(role) for role in ("orchestrator", "worker", "meta_reviewer",
                                                                    "verifier")}
    return out


@router.post("/runs/{run_id}/replay", status_code=201)
async def post_replay(run_id: str, body: ReplayBody | None = None, r=Depends(get_r),
                      keys: Keys = Depends(get_keys)) -> dict[str, Any]:
    """F9: a new run with the same goal, input file, playbook and config. With
    LLM_CACHE=replay-only every LLM call must hit the cache."""
    src = await must_run(r, keys, run_id)
    cfg = RunConfig.model_validate((await read_run_config(r, keys, run_id))["config"])
    if body and body.determinism is not None:
        cfg = cfg.model_copy(update={"determinism": body.determinism})
    if body and body.seed is not None:
        cfg = cfg.model_copy(update={"seed": body.seed})
    run = await create_run(r, keys, NewRun(goal=src.goal, input_file=src.input_file, playbook=src.playbook),
                           replay_of=src.id, cfg=cfg)
    out = await run_summary(r, keys, run)
    if src.input_sha256 and run.input_sha256 and src.input_sha256 != run.input_sha256:
        out["warning"] = f"input file changed since {src.id} (sha256 {src.input_sha256[:12]})"
    return out
