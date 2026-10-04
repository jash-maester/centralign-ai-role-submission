"""ScriptedBackend: answers llm.complete() from fixture JSON files (LLM_BACKEND=scripted).

For offline end-to-end runs when the free daily budget is gone, and for
integration tests that need realistic multi-call conversations. The fixture
format and lookup rules are documented in tests/fixtures/llm/README.md:

- every *.json under the fixtures dir (LLM_FIXTURES_DIR, default
  tests/fixtures/llm) holds one fixture object or a list of them:
  {"role": ..., "schema": ..., "fixture_key"?: ..., "match"?: ..., "response" | "responses" | "error"}
- the caller may put a hint `[fixture_key: <key>]` in a system message
  (prompts.assemble(..., fixture_key=...) does this);
- lookup order for (role, schema name): exact fixture_key, then a fixture
  whose "match" substring occurs in the messages, then the default fixture
  (no fixture_key, no match). A miss raises LLMError naming what to add.

Fault F4 (Track M): `model_outage` is honoured here too, so chaos runs on the
scripted backend show the same timeline as on OpenRouter: for a role in
llm_openrouter.MODEL_OUTAGE_ROLES one shot is consumed (fault.injected
phase=consumed), the injected primary model "fails" and model.fallback names
the scripted model that answers instead.
"""

from __future__ import annotations

import json
import os
import re
from pathlib import Path
from typing import Any

import redis.asyncio as aioredis
from pydantic import BaseModel

from .config import RunConfig
from .keys import Keys
from .llm import CallInfo, LLMError, record_call
from .protocol import EventType
from .settings import Settings, get_settings

FIXTURE_KEY = re.compile(r"\[fixture_key:\s*([^\]\s]+)\s*\]")


def fixture_hint(messages: list[dict[str, Any]]) -> str | None:
    for m in messages:
        if m.get("role") == "system":
            found = FIXTURE_KEY.search(str(m.get("content", "")))
            if found:
                return found.group(1)
    return None


def default_fixtures_dir(settings: Settings | None = None) -> Path:
    s = settings or get_settings()
    if s.llm_fixtures_dir:
        return Path(s.llm_fixtures_dir)
    candidates = [Path(os.environ.get("REPO_DIR", "/repo")) / "tests/fixtures/llm"]
    here = Path(__file__).resolve()
    if len(here.parents) > 3:
        candidates.append(here.parents[3] / "tests/fixtures/llm")
    for c in candidates:
        if c.is_dir():
            return c
    return candidates[0]


class ScriptedBackend:
    model_name = "scripted"

    def __init__(
        self,
        fixtures_dir: str | os.PathLike[str] | None = None,
        *,
        settings: Settings | None = None,
        r: aioredis.Redis | None = None,
        keys: Keys | None = None,
        emit_events: bool = True,
    ) -> None:
        self.settings = settings or get_settings()
        self.dir = Path(fixtures_dir) if fixtures_dir else default_fixtures_dir(self.settings)
        self.emit_events = emit_events
        self._r, self._keys = r, keys
        self._sink: Any = None
        self.fixtures: list[dict[str, Any]] = []
        self._used: dict[int, int] = {}
        self.reload()

    def reload(self) -> None:
        self.fixtures, self._used = [], {}
        if not self.dir.is_dir():
            return
        for path in sorted(self.dir.rglob("*.json")):
            data = json.loads(path.read_text())
            for fx in data if isinstance(data, list) else [data]:
                if not {"role", "schema"} <= fx.keys():
                    raise LLMError(f"{path}: fixture needs 'role' and 'schema'")
                if not ({"response", "responses", "error"} & fx.keys()):
                    raise LLMError(f"{path}: fixture needs 'response', 'responses' or 'error'")
                self.fixtures.append({**fx, "_file": path.name})

    def find(self, role: str, schema: str, messages: list[dict[str, Any]]) -> dict[str, Any] | None:
        pool = [f for f in self.fixtures if f["role"] == role and f["schema"] == schema]
        key = fixture_hint(messages)
        if key is not None:
            for f in pool:
                if f.get("fixture_key") == key:
                    return f
        text = "\n".join(str(m.get("content", "")) for m in messages)
        for f in pool:
            if f.get("match") and not f.get("fixture_key") and f["match"] in text:
                return f
        for f in pool:
            if not f.get("fixture_key") and not f.get("match"):
                return f
        return None

    async def complete(
        self,
        role: str,
        messages: list[dict[str, Any]],
        schema: type[BaseModel],
        *,
        run_id: str | None = None,
        step_id: str | None = None,
        config: RunConfig | None = None,
    ) -> Any:
        fx = self.find(role, schema.__name__, messages)
        if fx is None:
            hint = fixture_hint(messages)
            raise LLMError(
                f"no scripted fixture for role={role!r} schema={schema.__name__!r}"
                f"{f' fixture_key={hint!r}' if hint else ''} in {self.dir} (see tests/fixtures/llm/README.md)"
            )
        if "error" in fx:
            raise LLMError(f"scripted error ({fx['_file']}): {fx['error']}")
        model = fx.get("model") or f"{self.model_name}:{fx['_file']}"
        if self.emit_events:
            await self._outage(role, model, run_id, step_id, config)
        answers = fx["responses"] if "responses" in fx else [fx["response"]]
        n = self._used.get(id(fx), 0)
        self._used[id(fx)] = n + 1
        result = schema.model_validate(answers[min(n, len(answers) - 1)])
        if self.emit_events:
            await self._emit(run_id, step_id, {
                "role": role, "model": model, "ok": True, "status": 200, "cached": False, "scripted": True,
                "prompt_tokens": 0, "completion_tokens": 0, "total_tokens": 0, "cost_usd": 0.0, "latency_ms": 0,
            })
        record_call(CallInfo(role=role, model=model))
        return result

    async def _outage(self, role: str, model: str, run_id: str | None, step_id: str | None,
                      config: RunConfig | None) -> None:
        """F4 on the scripted backend: consume one model_outage shot and fall back."""
        from . import faults
        from .llm_openrouter import MODEL_OUTAGE_ROLES, OUTAGE_MODEL, LedgerSink
        from .protocol import FaultName

        if role not in MODEL_OUTAGE_ROLES:
            return
        if self._sink is None:
            self._sink = LedgerSink(self._r, self._keys, self.settings)
        if not await faults.consume(self._sink.r, self._sink.keys, FaultName.MODEL_OUTAGE):
            return
        await self._sink.emit(EventType.FAULT_INJECTED, {
            "fault": FaultName.MODEL_OUTAGE.value, "switch": FaultName.MODEL_OUTAGE.value, "phase": "consumed",
            "effect": f"primary {role} model replaced by {OUTAGE_MODEL}", "role": role,
        }, run_id, step_id)
        reason = f"fault model_outage: {OUTAGE_MODEL} is not a valid model id"
        if config is not None and not config.model_fallback:
            raise LLMError(f"all models failed for role {role}: {OUTAGE_MODEL}: {reason}")
        await self._sink.emit(EventType.MODEL_FALLBACK, {"role": role, "from": OUTAGE_MODEL, "to": model,
                                                         "reason": reason}, run_id, step_id)

    async def _emit(self, run_id: str | None, step_id: str | None, payload: dict[str, Any]) -> None:
        if self._sink is None:
            from .llm_openrouter import LedgerSink

            self._sink = LedgerSink(self._r, self._keys, self.settings)
        await self._sink.emit(EventType.LLM_CALL, payload, run_id, step_id)
