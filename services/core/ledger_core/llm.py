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
"""

from __future__ import annotations

from typing import Any, Literal, Protocol, TypeVar

from pydantic import BaseModel

from .config import RunConfig

Role = Literal["orchestrator", "worker", "verifier", "meta_reviewer"]
T = TypeVar("T", bound=BaseModel)


class LLMError(RuntimeError):
    pass


class LLMBudgetExhausted(LLMError):
    pass


class LLMSpendCapReached(LLMError):
    pass


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


_backend: LLMBackend | None = None


def set_backend(backend: LLMBackend | None) -> None:
    global _backend
    _backend = backend


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
        raise LLMError("no LLM backend installed (Track D implements OpenRouterBackend)")
    return await _backend.complete(role, messages, schema, run_id=run_id, step_id=step_id, config=config)
