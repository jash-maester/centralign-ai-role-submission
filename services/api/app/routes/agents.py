"""Agents: cards + liveness, per-agent config, restart, sandboxed shell (plans/01 §9a, H4, H9-H11).

- GET /agents: bare list (make chaos-kill-browser parses it) of AgentCard
  fields + alive, last_heartbeat_ms, heartbeat_age_ms, current_step, run_id,
  lease_fence, lease_ttl_ms (remaining), lease_total_ms, models, model,
  steps_done, rejections (for ?run_id=, default the newest run).
- GET/PUT /agents/{id}/config: agent_config.py (prompt versions, layers, tools).
- POST /agents/{id}/restart: docker restart of the agent's compose container.
- WS /agents/{id}/exec: docker exec into the agent's container as uid 10001
  (non-root), tty bridged both ways; emits shell.opened. Agent containers have
  no docker socket and no host mounts; only the api talks to docker.
"""

from __future__ import annotations

import asyncio
import json
import socket
import threading
import time
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, WebSocket, WebSocketDisconnect
from pydantic import BaseModel

from ledger_core import agent_config, agents as agents_mod, ledger, playbook
from ledger_core.events import append_event
from ledger_core.keys import Keys
from ledger_core.prompts import LAYER_NAMES, LAYERS, LOCKED_LAYERS, TOGGLEABLE_LAYERS, approx_tokens
from ledger_core.protocol import AgentCard, Event, EventType, ToolSpec
from ledger_core.settings import get_settings

from .. import docker_ops
from ..deps import get_keys, get_r, read_run_config

router = APIRouter(tags=["agents"])

ACTOR = "api"
RECENT_EVENT_SCAN = 3000


# ---- helpers ----------------------------------------------------------------------


async def get_card(r, keys: Keys, agent_id: str) -> AgentCard | None:
    raw = await r.hget(keys.agents, agent_id)
    return AgentCard.model_validate_json(raw) if raw else None


def service_for(agent_id: str, card: AgentCard | None) -> str:
    return (card.container if card and card.container else None) or agent_id


async def _agent_stats(r, keys: Keys, run_id: str | None) -> dict[str, dict[str, int]]:
    stats: dict[str, dict[str, int]] = {}
    if not run_id:
        return stats
    for step in await ledger.list_steps(r, keys, run_id):
        for a in step.history:
            if not a.worker:
                continue
            s = stats.setdefault(a.worker, {"steps_done": 0, "rejections": 0, "attempts": 0})
            s["attempts"] += 1
            if a.outcome == "committed":
                s["steps_done"] += 1
            elif a.outcome == "rejected":
                s["rejections"] += 1
    return stats


async def _last_models(r, keys: Keys) -> dict[str, str]:
    """agent id -> model of its latest llm.call / cache hit (recent events only)."""
    out: dict[str, str] = {}
    for _sid, fields in await r.xrevrange(keys.events, count=RECENT_EVENT_SCAN):
        ev = json.loads(fields["json"])
        if ev["type"] in ("llm.call", "llm.cache_hit") and ev["actor"] not in out:
            if ev["payload"].get("model") and ev["payload"].get("ok", True):
                out[ev["actor"]] = ev["payload"]["model"]
    return out


async def agent_rows(r, keys: Keys, run_id: str | None = None) -> list[dict[str, Any]]:
    if run_id is None:
        newest = await r.zrevrange(keys.runs, 0, 0)
        run_id = newest[0] if newest else None
    stats = await _agent_stats(r, keys, run_id)
    models_used = await _last_models(r, keys)
    s = get_settings()
    rows = []
    now = time.time() * 1000
    for a in await agents_mod.list_agents(r, keys):
        card: AgentCard = a["card"]
        step_id = a.get("current_step")
        ttl = await r.pttl(keys.lease(step_id)) if step_id else None
        total = None
        if step_id and a.get("run_id"):
            total = (await read_run_config(r, keys, a["run_id"]))["config"]["lease_ttl_s"] * 1000
        models = s.models_for(card.model_role) if card.model_role else []
        st = stats.get(card.id, {})
        rows.append({
            **card.model_dump(mode="json"),
            "alive": a["alive"], "last_heartbeat_ms": a.get("ts"),
            "heartbeat_age_ms": int(now - a["ts"]) if a.get("ts") else None,
            "current_step": step_id, "run_id": a.get("run_id"), "lease_fence": a.get("fence"),
            "lease_ttl_ms": ttl if ttl and ttl > 0 else None, "lease_total_ms": total,
            "models": models, "model": models_used.get(card.id) or (models[0] if models else None),
            "steps_done": st.get("steps_done", 0), "rejections": st.get("rejections", 0),
            "attempts": st.get("attempts", 0),
        })
    return rows


async def _recent_calls(r, keys: Keys, agent_id: str, limit: int = 20) -> list[dict[str, Any]]:
    calls = []
    for _sid, fields in await r.xrevrange(keys.events, count=RECENT_EVENT_SCAN):
        ev = json.loads(fields["json"])
        if ev["actor"] != agent_id or ev["type"] not in ("llm.call", "llm.cache_hit"):
            continue
        p = ev["payload"]
        code = "cache" if ev["type"] == "llm.cache_hit" else str(p.get("status") or ("ok" if p.get("ok") else "err"))
        calls.append({"ts": ev["ts"], "call": f"llm {p.get('role', '')} {p.get('model', '')}".strip(),
                      "code": code, "ms": p.get("latency_ms")})
        if len(calls) >= limit:
            break
    return calls


def _playbook_text(card: AgentCard | None) -> str:
    try:
        pb = playbook.load()
    except playbook.PlaybookError:
        try:
            pb = playbook.load(playbook_dir="/repo/playbooks")
        except playbook.PlaybookError:
            return ""
    kinds = [k for sk in (card.skills if card else []) for k in sk.kinds]
    seen, parts = set(), []
    for kind in kinds or [None]:
        try:
            sections = pb.sections_for(kind)
        except playbook.PlaybookError:
            continue
        for sec in sections:
            if sec.title not in seen:
                seen.add(sec.title)
                parts.append(sec.markdown())
    return "\n".join(parts)


async def config_view(r, keys: Keys, agent_id: str) -> dict[str, Any]:
    card = await get_card(r, keys, agent_id)
    cfg = await agent_config.get_config(r, keys, agent_id)
    role = card.model_role if card and card.model_role else "worker"
    prompt = await agent_config.prompt_text(r, keys, agent_id, role)
    layers_on = agent_config.effective_layers(cfg)
    pb_text = _playbook_text(card)
    texts = {"role": prompt, "playbook": pb_text}
    sources = {"role": f"prompt v{cfg.prompt_version}" if cfg.prompt_version else "built-in role text",
               "playbook": "playbook sections by step kind", "step": "step + committed facts (per attempt)",
               "history": "prior attempts + rejection reasons (per attempt)", "schema": "output schema (per call)"}
    layers = [{"id": lid, "name": LAYER_NAMES[lid], "source": sources[lid],
               "tokens": approx_tokens(texts[lid]) if lid in texts else None,
               "enabled": layers_on[lid], "locked": lid not in TOGGLEABLE_LAYERS,
               "locked_reason": ("D3 depends on it" if lid in LOCKED_LAYERS else
                                 "structural" if lid not in TOGGLEABLE_LAYERS else None),
               "preview": texts[lid][:400] if lid in texts else None} for lid in LAYERS]
    return {
        "agent_id": agent_id, "card": card.model_dump(mode="json") if card else None,
        "prompt": prompt, "prompt_version": cfg.prompt_version, "prompt_latest": cfg.prompt_latest,
        "prompt_versions": [p.model_dump(mode="json") for p in await agent_config.list_prompts(r, keys, agent_id)],
        "layers": layers,
        "tools": [t.model_dump(mode="json") for t in agent_config.effective_tools(card, cfg)],
        "recent_calls": await _recent_calls(r, keys, agent_id),
        "models": get_settings().models_for(role) if card and card.model_role else [],
        "revision": cfg.revision,
    }


# ---- routes ---------------------------------------------------------------------------


@router.get("/agents")
async def list_agents(run_id: str | None = None, r=Depends(get_r),
                      keys: Keys = Depends(get_keys)) -> list[dict[str, Any]]:
    return await agent_rows(r, keys, run_id)


@router.get("/agents/{agent_id}/config")
async def get_agent_config(agent_id: str, r=Depends(get_r), keys: Keys = Depends(get_keys)) -> dict[str, Any]:
    return await config_view(r, keys, agent_id)


class AgentConfigPatch(BaseModel):
    prompt: str | None = None
    prompt_note: str | None = None
    prompt_version: int | None = None  # activate (roll back/forward) a stored version
    layers: dict[str, bool] | None = None
    tools: list[ToolSpec] | dict[str, bool] | None = None


@router.put("/agents/{agent_id}/config")
async def put_agent_config(agent_id: str, patch: AgentConfigPatch, r=Depends(get_r),
                           keys: Keys = Depends(get_keys)) -> dict[str, Any]:
    card = await get_card(r, keys, agent_id)
    cfg = await agent_config.get_config(r, keys, agent_id)
    current = {t.id: t for t in agent_config.effective_tools(card, cfg)}
    toggles: dict[str, bool] = {}
    added: list[ToolSpec] = []
    if isinstance(patch.tools, dict):
        toggles = dict(patch.tools)
    elif patch.tools:
        for tool in patch.tools:
            if tool.id not in current:
                added.append(tool)
            elif current[tool.id].enabled != tool.enabled:
                toggles[tool.id] = tool.enabled
    try:
        for tool in added:
            await agent_config.add_tool(r, keys, agent_id, tool, card=card, actor=ACTOR)
        await agent_config.apply_update(r, keys, agent_id, prompt=patch.prompt, prompt_note=patch.prompt_note,
                                        layers=patch.layers, tools=toggles or None, card=card, actor=ACTOR)
        if patch.prompt_version is not None:
            await agent_config.activate_prompt(r, keys, agent_id, patch.prompt_version, actor=ACTOR)
    except agent_config.LockedLayerError as exc:
        raise HTTPException(409, str(exc)) from exc
    except agent_config.AgentConfigError as exc:  # includes ToolLockedError
        raise HTTPException(422, str(exc)) from exc
    return await config_view(r, keys, agent_id)


@router.post("/agents/{agent_id}/restart")
async def restart_agent(agent_id: str, r=Depends(get_r), keys: Keys = Depends(get_keys)) -> dict[str, Any]:
    card = await get_card(r, keys, agent_id)
    service = service_for(agent_id, card)
    try:
        result = await asyncio.to_thread(docker_ops.restart, service)
    except docker_ops.DockerUnavailable as exc:
        raise HTTPException(503, str(exc)) from exc
    except docker_ops.ContainerNotFound as exc:
        raise HTTPException(404, str(exc)) from exc
    await append_event(r, keys, Event(actor=ACTOR, type=EventType.CONFIG_UPDATED, payload={
        "scope": "agent", "agent_id": agent_id, "action": "restart", "container": result["name"]}))
    return {"agent_id": agent_id, **result}


# ---- WS /agents/{id}/exec ---------------------------------------------------------------


class ShellUnavailable(RuntimeError):
    pass


def open_exec(service: str, agent_id: str) -> tuple[Any, str, Any, dict[str, Any]]:
    """Start `sh` (bash when present) in the container as the non-root shell user.
    Returns (raw socket, exec id, low-level api, container info)."""
    ct = docker_ops.find_container(service)
    info = {"name": ct.name, "status": ct.status, "id": ct.short_id}
    if ct.status != "running":
        state = ct.attrs.get("State", {})
        raise ShellUnavailable(f"container {ct.name} is {ct.status} (exit code {state.get('ExitCode')}, "
                               f"finished {state.get('FinishedAt', '?')})")
    api = ct.client.api
    exec_id = api.exec_create(
        ct.id, ["/bin/sh", "-c", "if command -v bash >/dev/null; then exec bash; else exec sh; fi"],
        stdin=True, tty=True, user=docker_ops.SHELL_USER, workdir="/tmp",
        environment={"TERM": "xterm-256color", "HOME": "/tmp", "PS1": f"{agent_id}$ "},
    )["Id"]
    sock = api.exec_start(exec_id, socket=True, tty=True)
    raw = getattr(sock, "_sock", sock)
    return raw, exec_id, api, info


@router.websocket("/agents/{agent_id}/exec")
async def exec_ws(ws: WebSocket, agent_id: str) -> None:
    await ws.accept()
    r, keys = ws.app.state.redis, ws.app.state.keys
    card = await get_card(r, keys, agent_id)
    service = service_for(agent_id, card)
    try:
        raw, exec_id, api, info = await asyncio.to_thread(open_exec, service, agent_id)
    except (docker_ops.DockerUnavailable, docker_ops.ContainerNotFound, ShellUnavailable) as exc:
        await ws.send_text(f"\r\n\x1b[31m[ledger] no shell for {agent_id}: {exc}\x1b[0m\r\n")
        await ws.close(code=1011, reason=str(exc)[:120])
        return
    await append_event(r, keys, Event(actor=ACTOR, type=EventType.SHELL_OPENED, payload={
        "agent_id": agent_id, "container": info["name"], "user": docker_ops.SHELL_USER}))
    loop = asyncio.get_running_loop()
    closed = threading.Event()

    def pump() -> None:
        try:
            while not closed.is_set():
                data = raw.recv(4096)
                if not data:
                    break
                asyncio.run_coroutine_threadsafe(ws.send_bytes(data), loop).result(timeout=10)
        except Exception:  # noqa: BLE001 - socket closed or websocket gone
            pass
        finally:
            closed.set()
            asyncio.run_coroutine_threadsafe(_close(ws, "shell exited"), loop)

    reader = threading.Thread(target=pump, daemon=True)
    reader.start()
    try:
        while not closed.is_set():
            msg = await ws.receive()
            if msg["type"] == "websocket.disconnect":
                break
            data = msg.get("bytes") or (msg.get("text") or "").encode()
            if data[:1] == b"{":
                try:
                    ctl = json.loads(data)
                except ValueError:
                    ctl = None
                if isinstance(ctl, dict) and ctl.get("type") == "resize":
                    rows, cols = int(ctl.get("rows") or 24), int(ctl.get("cols") or 80)
                    await asyncio.to_thread(api.exec_resize, exec_id, height=rows, width=cols)
                    continue
            await asyncio.to_thread(raw.sendall, data)
    except (WebSocketDisconnect, RuntimeError, OSError):
        pass
    finally:
        closed.set()
        try:
            raw.shutdown(socket.SHUT_RDWR)
        except OSError:
            pass
        raw.close()


async def _close(ws: WebSocket, reason: str) -> None:
    try:
        await ws.close(code=1000, reason=reason)
    except RuntimeError:
        pass
