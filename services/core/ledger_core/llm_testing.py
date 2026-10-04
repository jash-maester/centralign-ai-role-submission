"""StubLLM: a programmable LLM backend for tests (never touches the network).

    stub = StubLLM()
    stub.on("orchestrator", Plan, response={"steps": [...]})           # fixed answer
    stub.on("worker", Draft, responses=[bad, good])                    # one per call, last repeats
    stub.on("verifier", Judge, exc=LLMBudgetExhausted("budget"))       # raise
    stub.on(None, Decision, fn=lambda call: {...})                     # computed from the call
    with stub.installed():
        ... code under test calls llm.complete(...) ...
    assert stub.calls[0].role == "orchestrator"

Rules match on (role, schema); None is a wildcard and later rules win over
earlier ones with the same specificity. An unmatched call raises LLMError
naming the missing (role, schema), so a test never silently gets nonsense.
"""

from __future__ import annotations

import contextlib
from collections.abc import Callable, Iterator
from dataclasses import dataclass, field
from typing import Any

from pydantic import BaseModel

from . import llm
from .config import RunConfig
from .llm import CallInfo, LLMError, record_call


@dataclass
class StubCall:
    role: str
    messages: list[dict[str, Any]]
    schema: type[BaseModel]
    run_id: str | None = None
    step_id: str | None = None
    config: RunConfig | None = None

    @property
    def text(self) -> str:
        """All message contents joined, for `assert "reason" in call.text`."""
        return "\n".join(str(m.get("content", "")) for m in self.messages)


@dataclass
class _Rule:
    role: str | None
    schema: str | None
    responses: list[Any] = field(default_factory=list)
    fn: Callable[[StubCall], Any] | None = None
    used: int = 0

    def specificity(self) -> int:
        return (self.role is not None) + (self.schema is not None)


class StubLLM:
    model_name = "stub"

    def __init__(self) -> None:
        self.calls: list[StubCall] = []
        self._rules: list[_Rule] = []

    def on(
        self,
        role: str | None = None,
        schema: type[BaseModel] | str | None = None,
        *,
        response: Any = None,
        responses: list[Any] | None = None,
        exc: BaseException | None = None,
        fn: Callable[[StubCall], Any] | None = None,
    ) -> StubLLM:
        """Program an answer. `response`/`responses` items may be dicts, model instances or exceptions."""
        items: list[Any] = list(responses or [])
        if response is not None:
            items.append(response)
        if exc is not None:
            items.append(exc)
        if not items and fn is None:
            raise ValueError("StubLLM.on needs response, responses, exc or fn")
        name = schema if isinstance(schema, str) or schema is None else schema.__name__
        self._rules.append(_Rule(role, name, items, fn))
        return self

    def reset(self) -> None:
        self.calls.clear()
        self._rules.clear()

    def calls_for(self, role: str | None = None, schema: type[BaseModel] | None = None) -> list[StubCall]:
        return [c for c in self.calls if (role is None or c.role == role) and (schema is None or c.schema is schema)]

    @contextlib.contextmanager
    def installed(self) -> Iterator[StubLLM]:
        previous = llm.get_backend()
        llm.set_backend(self)
        try:
            yield self
        finally:
            llm.set_backend(previous)

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
        call = StubCall(role, [dict(m) for m in messages], schema, run_id, step_id, config)
        self.calls.append(call)
        rule = self._match(role, schema.__name__)
        if rule is None:
            raise LLMError(f"StubLLM has no response for role={role!r} schema={schema.__name__!r}")
        if rule.fn is not None and not rule.responses:
            answer = rule.fn(call)
        else:
            answer = rule.responses[min(rule.used, len(rule.responses) - 1)]
        rule.used += 1
        if isinstance(answer, BaseException):
            raise answer
        result = answer if isinstance(answer, schema) else schema.model_validate(
            answer.model_dump() if isinstance(answer, BaseModel) else answer)
        record_call(CallInfo(role=role, model=self.model_name))
        return result

    def _match(self, role: str, schema: str) -> _Rule | None:
        best: _Rule | None = None
        for rule in self._rules:  # later rules win ties
            if rule.role not in (None, role) or rule.schema not in (None, schema):
                continue
            if best is None or rule.specificity() >= best.specificity():
                best = rule
        return best
