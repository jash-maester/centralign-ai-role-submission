"""Postcondition check registry (plans/01-architecture.md §8).

CONTRACT (fixed in W0; Track B implements the checks):

    result = await run_check(name, args, expect, ctx)  -> CheckResult

Checks read the world through a channel different from the one workers act
through (CRM REST, Mailpit API, source file, ledger facts). Deterministic
checks only; the LLM judge for email.draft_valid is layered on by the verifier.
"""

from __future__ import annotations

import importlib
import pkgutil
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from typing import Any

CHECK_NAMES: tuple[str, ...] = (
    "file.parsed_rows",
    "crm.lookup_matches",
    "crm.contact_exists",
    "crm.no_duplicate",
    "crm.task_exists",
    "email.draft_valid",
    "email.sent",
    "review.decided",
    "run.criteria_met",
)


@dataclass
class CheckResult:
    ok: bool
    reason: str
    observed: dict[str, Any] = field(default_factory=dict)


@dataclass
class CheckContext:
    """Read-only handles a check may use. Populated by the verifier."""

    run_id: str
    step_id: str | None = None
    claim: dict[str, Any] | None = None  # the worker's claim data, for comparison only
    facts: dict[str, Any] = field(default_factory=dict)
    crm: Any = None  # crm_api.CrmClient (read-only key)
    mailpit: Any = None  # httpx.AsyncClient to MAILPIT_API_URL
    data_dir: str = "/app/data"
    extra: dict[str, Any] = field(default_factory=dict)


CheckFn = Callable[[dict[str, Any], dict[str, Any], CheckContext], Awaitable[CheckResult]]
REGISTRY: dict[str, CheckFn] = {}


def register(name: str) -> Callable[[CheckFn], CheckFn]:
    if name not in CHECK_NAMES:
        raise ValueError(f"unknown check {name!r}; add it to CHECK_NAMES and plans/01 §8")

    def deco(fn: CheckFn) -> CheckFn:
        REGISTRY[name] = fn
        return fn

    return deco


async def run_check(name: str, args: dict[str, Any], expect: dict[str, Any], ctx: CheckContext) -> CheckResult:
    fn = REGISTRY.get(name)
    if fn is None:
        return CheckResult(False, f"check {name!r} is not implemented")
    return await fn(args, expect, ctx)


def load_all() -> dict[str, CheckFn]:
    """Import every module in ledger_core.checks so their checks register."""
    from . import checks

    for mod in pkgutil.iter_modules(checks.__path__):
        importlib.import_module(f"{checks.__name__}.{mod.name}")
    return REGISTRY
