"""The verifier: a claim is not a fact (plans/01 §8, D1-D3).

    v = Verifier(r, keys)                 # python -m ledger_core.services.verifier
    await v.run()                         # consume queue:verify until stop()
    await v.verify(step_id)               # one step (tests)

For each step in `claimed_done`:
1. Build a postconditions.CheckContext with `context_factory(step, r, keys)`.
   The default gives run/step ids, the claim data (for comparison only), the
   committed facts and DATA_DIR. Track F passes its own factory to add the
   read-only CRM client and the Mailpit client.
2. Resolve "fact:<key>" refs in postcondition args/expect, then
   `run_check(check, args, expect, ctx)`. A check that raises is a failed check.
3. If the deterministic check passed and a `judge` is installed (Track F: LLM
   judge for soft checks, D4), the judge may still fail it.
4. ok  -> ledger.commit(): verified -> committed, and the step's facts are
   written in the same transaction. Facts come from `facts_from_claim`.
   not ok -> ledger.reject(): rejected (reason stored on the attempt in
   step.history, so the next attempt's WorkContext shows it, D3), then
   `reject_policy` picks READY (retry, requeued), DEAD (max_attempts
   rejections) or None (stay rejected; the orchestrator replans, B5).

Facts registry
--------------
Each step kind registers how a verified claim becomes facts:

    from ledger_core.verifier import register_facts

    @register_facts(StepKind.CRM_CREATE_CONTACT)
    def _contact_facts(step, claim, result) -> dict[str, Any]:
        return {f"{step.lane}.contact_id": result.observed["id"]}

Prefer values the check observed (result.observed) over claim data where the
check read them from the world. Kinds without an extractor commit
{"step:<step_id>": claim.data}. Register extractors in the kind's check module
(ledger_core/checks/<channel>.py), which postconditions.load_all() imports.
"""

from __future__ import annotations

import asyncio
import logging
import signal
from collections.abc import Awaitable, Callable
from typing import Any

import redis.asyncio as aioredis

from . import agents, bus, ledger, postconditions
from .config import RunConfig
from .keys import Keys
from .postconditions import CheckContext, CheckResult
from .protocol import AgentCard, Claim, Step, StepKind, StepStatus, Verdict
from .settings import get_settings

log = logging.getLogger("ledger.verifier")

FactExtractor = Callable[[Step, Claim, CheckResult], dict[str, Any]]
ContextFactory = Callable[[Step, aioredis.Redis, Keys], Awaitable[CheckContext]]
Judge = Callable[[Step, CheckResult, CheckContext], Awaitable[CheckResult]]
RejectPolicy = Callable[[Step, Verdict, RunConfig], Awaitable[StepStatus | None]]

FACT_EXTRACTORS: dict[StepKind, FactExtractor] = {}


def register_facts(kind: StepKind | str) -> Callable[[FactExtractor], FactExtractor]:
    def deco(fn: FactExtractor) -> FactExtractor:
        FACT_EXTRACTORS[StepKind(kind)] = fn
        return fn

    return deco


def facts_from_claim(step: Step, claim: Claim, result: CheckResult) -> dict[str, Any]:
    fn = FACT_EXTRACTORS.get(step.kind)
    if fn is None:
        return {f"step:{step.id}": claim.data}
    return fn(step, claim, result)


async def default_context(step: Step, r: aioredis.Redis, keys: Keys) -> CheckContext:
    return CheckContext(
        run_id=step.run_id, step_id=step.id,
        claim=dict(step.claim.data) if step.claim else None,
        facts=await ledger.get_facts(r, keys, step.run_id),
        data_dir=get_settings().data_dir,
        extra={"step": step, "claim": step.claim, "inputs": step.inputs},
    )


async def default_reject_policy(step: Step, verdict: Verdict, cfg: RunConfig) -> StepStatus | None:
    """Retry until the step has been rejected max_attempts times, then dead.
    (`step` is the claimed_done step, before this rejection is recorded.)"""
    if ledger.rejection_count(step) + 1 >= step.max_attempts:
        return StepStatus.DEAD
    return StepStatus.READY


def verifier_card(agent_id: str = "verifier") -> AgentCard:
    return AgentCard.model_validate({
        "id": agent_id, "name": "Verifier", "role": "verifier", "model_role": "verifier",
        "side_effects": False, "container": "verifier",
        "tools": [
            {"id": "postconditions", "type": "function", "name": "Postcondition registry",
             "detail": ", ".join(postconditions.CHECK_NAMES)},
            {"id": "crm-rest", "type": "rest", "name": "EspoCRM REST (read-only key)",
             "locked_reason": "Verifier-only channel"},
            {"id": "mailpit", "type": "rest", "name": "Mailpit API (read-only)"},
            {"id": "source-files", "type": "function", "name": "Source files (DATA_DIR, read-only)"},
        ],
    })


class Verifier:
    def __init__(
        self, r: aioredis.Redis, keys: Keys, *, agent_id: str = "verifier",
        context_factory: ContextFactory = default_context, judge: Judge | None = None,
        reject_policy: RejectPolicy = default_reject_policy, block_ms: int = 2000,
        card: AgentCard | None = None,
    ) -> None:
        self.r, self.keys, self.agent_id = r, keys, agent_id
        self.context_factory, self.judge, self.reject_policy = context_factory, judge, reject_policy
        self.block_ms = block_ms
        self.card = card or verifier_card(agent_id)
        self.consumer = bus.verify_consumer(r, keys, agent_id)
        self._stop = asyncio.Event()
        self._started = False

    async def start(self) -> None:
        if self._started:
            return
        postconditions.load_all()
        await self.consumer.setup()
        await agents.register_agent(self.r, self.keys, self.card)
        await agents.set_alive(self.r, self.keys, self.agent_id)
        self._started = True

    def stop(self) -> None:
        self._stop.set()

    def install_signal_handlers(self) -> None:
        loop = asyncio.get_running_loop()
        for sig in (signal.SIGTERM, signal.SIGINT):
            loop.add_signal_handler(sig, self.stop)

    async def run(self) -> None:
        await self.start()
        live = asyncio.create_task(self._liveness())
        try:
            while not self._stop.is_set():
                for d in await self.consumer.next(self.block_ms):
                    try:
                        await self.verify(d.step_id)
                    except Exception:  # noqa: BLE001 - one bad step must not stop verification
                        log.exception("verifying %s failed", d.step_id)
                    finally:
                        await self.consumer.ack(d)
        finally:
            live.cancel()
            await agents.clear_alive(self.r, self.keys, self.agent_id)

    async def run_until_idle(self, max_messages: int = 100) -> int:
        """Test helper: verify queued steps until none arrive within block_ms."""
        await self.start()
        n = 0
        while n < max_messages:
            got = await self.consumer.next(self.block_ms)
            if not got:
                break
            for d in got:
                try:
                    await self.verify(d.step_id)
                finally:
                    await self.consumer.ack(d)
                n += 1
        return n

    async def _liveness(self) -> None:
        while True:
            await asyncio.sleep(agents.DEFAULT_LIVENESS_TTL_S / 3)
            try:
                await agents.set_alive(self.r, self.keys, self.agent_id)
            except Exception:  # noqa: BLE001
                log.exception("liveness update failed")

    async def check(self, step: Step) -> tuple[CheckResult, CheckContext]:
        ctx = await self.context_factory(step, self.r, self.keys)
        pc = step.postcondition
        args = await ledger.resolve_inputs(self.r, self.keys, step, pc.args, strict=False)
        expect = await ledger.resolve_inputs(self.r, self.keys, step, pc.expect, strict=False)
        try:
            result = await postconditions.run_check(pc.check, args, expect, ctx)
        except Exception as exc:  # noqa: BLE001 - a crashing check is a failed check
            log.exception("check %s crashed on %s", pc.check, step.id)
            result = CheckResult(False, f"check {pc.check} raised {type(exc).__name__}: {exc}")
        if result.ok and self.judge is not None:
            result = await self.judge(step, result, ctx)
        return result, ctx

    async def verify(self, step_id: str) -> Step | None:
        """Verify one step if it is claimed_done; returns the updated step."""
        step = await ledger.get_step(self.r, self.keys, step_id)
        if step is None or step.status != StepStatus.CLAIMED_DONE or step.claim is None:
            return None
        result, _ctx = await self.check(step)
        verdict = Verdict(ok=result.ok, check=step.postcondition.check, reason=result.reason,
                          observed=result.observed, verifier=self.agent_id)
        try:
            if result.ok:
                facts = facts_from_claim(step, step.claim, result)
                return await ledger.commit(self.r, self.keys, step.id, verdict, facts, actor=self.agent_id)
            cfg = await ledger.get_run_config(self.r, self.keys, step.run_id)
            then = await self.reject_policy(step, verdict, cfg)
            return await ledger.reject(self.r, self.keys, step.id, verdict, actor=self.agent_id, then=then)
        except ledger.IllegalTransition as exc:  # another verifier got there first
            log.info("verify %s skipped: %s", step.id, exc)
            return await ledger.get_step(self.r, self.keys, step.id)
