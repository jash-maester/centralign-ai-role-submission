"""Per-agent configuration (plans/01-architecture.md §7, §9a; H9, H10).

Stored at Keys.agent_config(agent_id) as JSON (AgentConfig). Prompt texts are
versioned at Keys.agent_prompt(agent_id, n), n = 1, 2, ...; version 0 means
the built-in role text from prompts.ROLE_INSTRUCTIONS.

- Prompt: update_prompt() stores a new version and activates it;
  activate_prompt() rolls back/forward. Both emit prompt.updated.
- Layers: set_layer() toggles prompt layers; "history" (layer 4) is locked on
  because D3 depends on it, "role" and "step" are structural. Emits config.updated.
- Tools: the AgentCard lists tools; this config overrides `enabled` per tool id
  and can add tools (H10). Workers can never enable the CRM REST tool: that
  channel belongs to the verifier and meta-reviewer (§8, §9a). Emits tool.toggled.

All writes are optimistic transactions (WATCH), so concurrent GUI edits never
lose each other's changes.
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Callable
from typing import Any

import redis.asyncio as aioredis
from pydantic import BaseModel, Field

from .events import append_event
from .keys import Keys
from .prompts import LAYERS, LOCKED_LAYERS, ROLE_INSTRUCTIONS, TOGGLEABLE_LAYERS
from .protocol import AgentCard, Event, EventType, ToolSpec, now_ms

CRM_REST_LOCK = "Verifier-only channel"


class AgentConfigError(ValueError):
    pass


class LockedLayerError(AgentConfigError):
    pass


class ToolLockedError(AgentConfigError):
    pass


class PromptVersion(BaseModel):
    version: int
    text: str
    author: str = "api"
    note: str | None = None
    sha256: str = ""
    created_at: int = Field(default_factory=now_ms)


class AgentConfig(BaseModel):
    agent_id: str
    prompt_version: int = 0  # active version; 0 = built-in role text
    prompt_latest: int = 0  # highest stored version
    layers: dict[str, bool] = Field(default_factory=lambda: {k: True for k in LAYERS})
    tools: dict[str, bool] = Field(default_factory=dict)  # enabled overrides by tool id
    extra_tools: list[ToolSpec] = Field(default_factory=list)  # added in the GUI (H10)
    revision: int = 0
    updated_at: int = Field(default_factory=now_ms)


# ---- tool rules -------------------------------------------------------------


def is_worker(card: AgentCard | None, agent_id: str) -> bool:
    return card.role == "worker" if card is not None else agent_id.startswith("worker")


def is_crm_rest(tool: ToolSpec) -> bool:
    if tool.type != "rest":
        return False
    blob = f"{tool.id} {tool.name} {tool.detail}".lower()
    return "espocrm" in blob or "crm" in blob


def _lock_reason(tool: ToolSpec, worker: bool) -> str | None:
    if worker and is_crm_rest(tool):
        return tool.locked_reason or CRM_REST_LOCK
    return tool.locked_reason


def effective_tools(card: AgentCard | None, cfg: AgentConfig) -> list[ToolSpec]:
    """Card tools + added tools with this config's switches and the hard locks applied."""
    worker = is_worker(card, cfg.agent_id)
    out = []
    for tool in [*(card.tools if card else []), *cfg.extra_tools]:
        lock = _lock_reason(tool, worker)
        if worker and is_crm_rest(tool):
            enabled = False
        elif tool.locked_reason:
            enabled = tool.enabled
        else:
            enabled = cfg.tools.get(tool.id, tool.enabled)
        out.append(tool.model_copy(update={"enabled": enabled, "locked_reason": lock}))
    return out


def effective_layers(cfg: AgentConfig) -> dict[str, bool]:
    return {k: (True if k not in TOGGLEABLE_LAYERS else cfg.layers.get(k, True)) for k in LAYERS}


# ---- storage ------------------------------------------------------------------


async def get_config(r: aioredis.Redis, keys: Keys, agent_id: str) -> AgentConfig:
    raw = await r.get(keys.agent_config(agent_id))
    return AgentConfig.model_validate_json(raw) if raw else AgentConfig(agent_id=agent_id)


async def _mutate(
    r: aioredis.Redis, keys: Keys, agent_id: str,
    fn: Callable[[AgentConfig], tuple[AgentConfig, dict[str, str]] | None],
) -> tuple[AgentConfig, bool]:
    """Apply fn(cfg) -> (new_cfg, extra_keys_to_set) | None (no change) atomically."""
    key = keys.agent_config(agent_id)
    while True:
        async with r.pipeline(transaction=True) as pipe:
            try:
                await pipe.watch(key)
                raw = await pipe.get(key)
                cfg = AgentConfig.model_validate_json(raw) if raw else AgentConfig(agent_id=agent_id)
                result = fn(cfg)
                if result is None:
                    return cfg, False
                new, extra = result
                new.revision = cfg.revision + 1
                new.updated_at = now_ms()
                pipe.multi()
                for k, v in extra.items():
                    pipe.set(k, v)
                pipe.set(key, new.model_dump_json())
                await pipe.execute()
                return new, True
            except aioredis.WatchError:
                continue


async def _emit(r: aioredis.Redis, keys: Keys, actor: str, type_: EventType, payload: dict[str, Any]) -> None:
    await append_event(r, keys, Event(actor=actor, type=type_, payload=payload))


# ---- prompt -------------------------------------------------------------------


async def get_prompt(r: aioredis.Redis, keys: Keys, agent_id: str, version: int | None = None) -> PromptVersion | None:
    """A stored version (default: the active one). None for version 0 / missing."""
    if version is None:
        version = (await get_config(r, keys, agent_id)).prompt_version
    if version <= 0:
        return None
    raw = await r.get(keys.agent_prompt(agent_id, version))
    return PromptVersion.model_validate_json(raw) if raw else None


async def list_prompts(r: aioredis.Redis, keys: Keys, agent_id: str) -> list[PromptVersion]:
    cfg = await get_config(r, keys, agent_id)
    if cfg.prompt_latest == 0:
        return []
    raws = await r.mget([keys.agent_prompt(agent_id, v) for v in range(1, cfg.prompt_latest + 1)])
    return [PromptVersion.model_validate_json(x) for x in raws if x]


async def prompt_text(r: aioredis.Redis, keys: Keys, agent_id: str, role: str) -> str:
    """The text for prompt layer 1: the active version, else the built-in role text."""
    stored = await get_prompt(r, keys, agent_id)
    return stored.text if stored else ROLE_INSTRUCTIONS.get(role, ROLE_INSTRUCTIONS["worker"])


async def update_prompt(r: aioredis.Redis, keys: Keys, agent_id: str, text: str, *, actor: str = "api",
                        note: str | None = None) -> PromptVersion:
    text = text.strip()
    if not text:
        raise AgentConfigError("prompt text is empty")
    created: dict[str, Any] = {}

    def fn(cfg: AgentConfig):
        v = cfg.prompt_latest + 1
        pv = PromptVersion(version=v, text=text, author=actor, note=note,
                           sha256=hashlib.sha256(text.encode()).hexdigest())
        created.update(pv=pv, previous=cfg.prompt_version)
        new = cfg.model_copy(update={"prompt_version": v, "prompt_latest": v})
        return new, {keys.agent_prompt(agent_id, v): pv.model_dump_json()}

    await _mutate(r, keys, agent_id, fn)
    pv: PromptVersion = created["pv"]
    await _emit(r, keys, actor, EventType.PROMPT_UPDATED, {
        "agent_id": agent_id, "version": pv.version, "previous_version": created["previous"],
        "sha256": pv.sha256, "chars": len(pv.text), "note": note,
    })
    return pv


async def activate_prompt(r: aioredis.Redis, keys: Keys, agent_id: str, version: int, *,
                          actor: str = "api") -> AgentConfig:
    """Switch the active prompt (0 = built-in text). Emits prompt.updated."""
    if version > 0 and await r.get(keys.agent_prompt(agent_id, version)) is None:
        raise AgentConfigError(f"agent {agent_id} has no prompt version {version}")
    previous: dict[str, int] = {}

    def fn(cfg: AgentConfig):
        if cfg.prompt_version == version:
            return None
        previous["v"] = cfg.prompt_version
        return cfg.model_copy(update={"prompt_version": version}), {}

    cfg, changed = await _mutate(r, keys, agent_id, fn)
    if changed:
        await _emit(r, keys, actor, EventType.PROMPT_UPDATED, {
            "agent_id": agent_id, "version": version, "previous_version": previous["v"], "activated": True,
        })
    return cfg


# ---- layers -------------------------------------------------------------------


async def set_layer(r: aioredis.Redis, keys: Keys, agent_id: str, layer: str, enabled: bool, *,
                    actor: str = "api") -> AgentConfig:
    if layer not in LAYERS:
        raise AgentConfigError(f"unknown prompt layer {layer!r}; layers are {', '.join(LAYERS)}")
    if layer not in TOGGLEABLE_LAYERS:
        if enabled:
            return await get_config(r, keys, agent_id)
        why = "prior attempts + rejection reasons feed the next attempt (D3)" if layer in LOCKED_LAYERS \
            else "the prompt cannot work without it"
        raise LockedLayerError(f"layer {layer!r} is locked on: {why}")
    old: dict[str, bool] = {}

    def fn(cfg: AgentConfig):
        if cfg.layers.get(layer, True) == enabled:
            return None
        old["v"] = cfg.layers.get(layer, True)
        return cfg.model_copy(update={"layers": {**cfg.layers, layer: enabled}}), {}

    cfg, changed = await _mutate(r, keys, agent_id, fn)
    if changed:
        await _emit(r, keys, actor, EventType.CONFIG_UPDATED, {
            "scope": "agent", "agent_id": agent_id, "field": f"layers.{layer}",
            "old": old["v"], "new": enabled, "revision": cfg.revision,
        })
    return cfg


# ---- tools --------------------------------------------------------------------


async def set_tool(r: aioredis.Redis, keys: Keys, agent_id: str, tool_id: str, enabled: bool, *,
                   card: AgentCard | None = None, actor: str = "api") -> AgentConfig:
    current = await get_config(r, keys, agent_id)
    tool = next((t for t in [*(card.tools if card else []), *current.extra_tools] if t.id == tool_id), None)
    if tool is None:
        raise AgentConfigError(f"agent {agent_id} has no tool {tool_id!r}")
    worker = is_worker(card, agent_id)
    if worker and is_crm_rest(tool):
        if enabled:
            raise ToolLockedError(f"{tool_id}: workers never get the CRM REST tool ({CRM_REST_LOCK})")
        return current
    if tool.locked_reason:
        raise ToolLockedError(f"{tool_id} is locked: {tool.locked_reason}")

    def fn(cfg: AgentConfig):
        if cfg.tools.get(tool_id, tool.enabled) == enabled:
            return None
        return cfg.model_copy(update={"tools": {**cfg.tools, tool_id: enabled}}), {}

    cfg, changed = await _mutate(r, keys, agent_id, fn)
    if changed:
        await _emit(r, keys, actor, EventType.TOOL_TOGGLED, {
            "agent_id": agent_id, "tool_id": tool_id, "type": tool.type, "enabled": enabled,
        })
    return cfg


async def add_tool(r: aioredis.Redis, keys: Keys, agent_id: str, tool: ToolSpec, *,
                   card: AgentCard | None = None, actor: str = "api") -> AgentConfig:
    """Add a tool (REST / MCP / function ...). A CRM REST tool on a worker is added locked off."""
    known = {t.id for t in (card.tools if card else [])}
    if is_worker(card, agent_id) and is_crm_rest(tool):
        tool = tool.model_copy(update={"enabled": False, "locked_reason": CRM_REST_LOCK})

    def fn(cfg: AgentConfig):
        if tool.id in known or any(t.id == tool.id for t in cfg.extra_tools):
            raise AgentConfigError(f"agent {agent_id} already has a tool {tool.id!r}")
        return cfg.model_copy(update={"extra_tools": [*cfg.extra_tools, tool]}), {}

    cfg, _ = await _mutate(r, keys, agent_id, fn)
    await _emit(r, keys, actor, EventType.TOOL_TOGGLED, {
        "agent_id": agent_id, "tool_id": tool.id, "type": tool.type, "enabled": tool.enabled,
        "added": True, "tool": json.loads(tool.model_dump_json()),
    })
    return cfg


async def remove_tool(r: aioredis.Redis, keys: Keys, agent_id: str, tool_id: str, *,
                      actor: str = "api") -> AgentConfig:
    """Remove a tool that was added through add_tool (card tools can only be switched off)."""

    def fn(cfg: AgentConfig):
        if not any(t.id == tool_id for t in cfg.extra_tools):
            raise AgentConfigError(f"{tool_id!r} was not added to {agent_id}; switch card tools off instead")
        tools = {k: v for k, v in cfg.tools.items() if k != tool_id}
        return cfg.model_copy(update={"extra_tools": [t for t in cfg.extra_tools if t.id != tool_id],
                                      "tools": tools}), {}

    cfg, _ = await _mutate(r, keys, agent_id, fn)
    await _emit(r, keys, actor, EventType.TOOL_TOGGLED, {
        "agent_id": agent_id, "tool_id": tool_id, "enabled": False, "removed": True,
    })
    return cfg


# ---- one-shot update (PUT /agents/{id}/config) --------------------------------


async def apply_update(r: aioredis.Redis, keys: Keys, agent_id: str, *, prompt: str | None = None,
                       prompt_note: str | None = None, layers: dict[str, bool] | None = None,
                       tools: dict[str, bool] | None = None, card: AgentCard | None = None,
                       actor: str = "api") -> AgentConfig:
    """Validate everything first, then apply prompt, layer and tool changes (one event each)."""
    for layer, on in (layers or {}).items():
        if layer not in LAYERS:
            raise AgentConfigError(f"unknown prompt layer {layer!r}")
        if layer not in TOGGLEABLE_LAYERS and not on:
            raise LockedLayerError(f"layer {layer!r} is locked on")
    if prompt is not None:
        current = await get_prompt(r, keys, agent_id)
        if current is None or current.text != prompt.strip():
            await update_prompt(r, keys, agent_id, prompt, actor=actor, note=prompt_note)
    for layer, on in (layers or {}).items():
        await set_layer(r, keys, agent_id, layer, on, actor=actor)
    for tool_id, on in (tools or {}).items():
        await set_tool(r, keys, agent_id, tool_id, on, card=card, actor=actor)
    return await get_config(r, keys, agent_id)
