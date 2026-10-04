"""The only path to an LLM (plans/01-architecture.md §6, §6b).

CONTRACT (fixed in W0; Track D implements the body):

    result = await complete(
        role="orchestrator" | "worker" | "verifier" | "meta_reviewer",
        messages=[{"role": "system", "content": ...}, ...],
        schema=SomePydanticModel,
        run_id=..., step_id=..., config=RunConfig | None,
    )

- Returns an instance of `schema` (validated), never raw text.
- Model list per role from env (Settings.models_for(role)); falls back down
  the list on error and emits model.fallback.
- Temperature from RunConfig.temperature(role); seed passed when pinned.
- Response cache, daily request budget and spend cap per §6b. Raises
  LLMBudgetExhausted / LLMSpendCapReached / LLMError.
- Tests install a stub with set_backend(); unit tests never call OpenRouter.

Backends (Track D):
- LLM_BACKEND=openrouter (default): llm_openrouter.OpenRouterBackend
- LLM_BACKEND=scripted: llm_scripted.ScriptedBackend (fixture files, offline runs)
- LLM_BACKEND=stub: llm_testing.StubLLM (programmable, for tests)
complete() installs the env backend lazily when none was set.

After a successful complete(), `last_call()` returns metadata about it (model,
cached, tokens, cost) for callers that record which model decided something
(Verdict.model, ReviewDecision.model). It is a context variable, so it is
safe under asyncio concurrency.
"""

from __future__ import annotations

import os
from contextvars import ContextVar
from dataclasses import dataclass
from typing import Any, Literal, Protocol, TypeVar

from pydantic import BaseModel

from .config import RunConfig

Role = Literal["orchestrator", "worker", "verifier", "meta_reviewer"]
T = TypeVar("T", bound=BaseModel)

BACKENDS = ("openrouter", "scripted", "stub")


class LLMError(RuntimeError):
    pass


class LLMBudgetExhausted(LLMError):
    pass


class LLMSpendCapReached(LLMError):
    pass


class LLMCacheMiss(LLMError):
    """LLM_CACHE=replay-only and no cached response exists for this call."""


class LLMBackend(Protocol):
    async def complete(
        self,
        role: Role,
        messages: list[dict[str, Any]],
        schema: type[T],
        *,
        run_id: str | None = None,
        step_id: str | None = None,
        config: RunConfig | None = None,
    ) -> T: ...


@dataclass
class CallInfo:
    """What happened on the most recent successful complete() in this context."""

    role: str
    model: str
    cached: bool = False
    prompt_tokens: int = 0
    completion_tokens: int = 0
    cost_usd: float = 0.0
    latency_ms: int = 0
    fallback_from: list[str] | None = None


_last_call: ContextVar[CallInfo | None] = ContextVar("ledger_llm_last_call", default=None)


def record_call(info: CallInfo) -> None:
    """Backends call this after a successful completion."""
    _last_call.set(info)


def last_call() -> CallInfo | None:
    return _last_call.get()


_backend: LLMBackend | None = None


def set_backend(backend: LLMBackend | None) -> None:
    global _backend
    _backend = backend


def get_backend() -> LLMBackend | None:
    return _backend


def backend_from_env(name: str | None = None) -> LLMBackend:
    """Build the backend named by LLM_BACKEND (openrouter | scripted | stub)."""
    name = (name or os.environ.get("LLM_BACKEND") or "openrouter").strip().lower()
    if name == "openrouter":
        from .llm_openrouter import OpenRouterBackend

        return OpenRouterBackend()
    if name == "scripted":
        from .llm_scripted import ScriptedBackend

        return ScriptedBackend()
    if name == "stub":
        from .llm_testing import StubLLM

        return StubLLM()
    raise LLMError(f"unknown LLM_BACKEND {name!r}; expected one of {BACKENDS}")


async def complete(
    role: Role,
    messages: list[dict[str, Any]],
    schema: type[T],
    *,
    run_id: str | None = None,
    step_id: str | None = None,
    config: RunConfig | None = None,
) -> T:
    if _backend is None:
        set_backend(backend_from_env())
    assert _backend is not None
    return await _backend.complete(role, messages, schema, run_id=run_id, step_id=step_id, config=config)
