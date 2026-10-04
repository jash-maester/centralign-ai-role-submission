"""Entrypoint for worker-api (Track F): skill api.espocrm (C11 fallback; the
Phase 2 CRM path before the browser operator exists).

Wires Track B's REST handlers (workers/crm_api_skill.py) onto worker_base:
check-then-act comes from RunConfig.check_then_act; the claim carries
contact_id / action / owner / task_id (+ contact_url, idempotency_key).

Inputs the handlers cannot act on (SkillInputError: no email, ambiguous
account, unknown region, missing contact) are recorded as a `blocked`
observation (retryable=False) and claimed as not acted; the verifier then
rejects with what REST shows, and the orchestrator can replan (B5).

The worker holds only the `ledger-writer` key; it never sees the verifier's.
"""

from __future__ import annotations

import asyncio
import logging
from typing import Any

import redis.asyncio as aioredis

from ledger_core.crm_api import CrmWriter, writer_from_redis
from ledger_core.keys import Keys
from ledger_core.protocol import CRM_KINDS, AgentCard, Skill, Step
from ledger_core.redis_conn import connect
from ledger_core.settings import get_settings
from ledger_core.worker_base import WorkContext, Worker, WorkResult, worker_card
from ledger_core.workers import crm_api_skill
from ledger_core.workers.crm_api_skill import SkillInputError

log = logging.getLogger("ledger.worker_api")

CLAIM_KEYS = ("contact_id", "action", "owner", "task_id")


def api_card(agent_id: str) -> AgentCard:
    return worker_card(
        agent_id, "CRM API worker", {Skill.API_ESPOCRM.value: [k.value for k in CRM_KINDS]},
        side_effects=True, container="worker-api",
        tools=[{"id": "crm-rest-writer", "type": "rest", "name": "EspoCRM REST (ledger-writer key)",
                "detail": "create/update contacts, create tasks; check-then-act; C11 fallback skill"}],
    )


def make_handler(*, writer: CrmWriter | None = None, secrets_keys: Keys | None = None):
    """Handler for worker_base.Worker. `writer` is injected in tests; otherwise
    built once from the seed's Keys.crm_secrets (namespace `secrets_keys`, else
    the worker's own keys)."""
    state: dict[str, Any] = {"writer": writer}

    async def handle(step: Step, ctx: WorkContext) -> WorkResult:
        if state["writer"] is None:
            state["writer"] = await writer_from_redis(ctx.r, secrets_keys or ctx.keys)
        if ctx.rejection_reasons:
            await ctx.observe({"retry_after_rejection": ctx.rejection_reasons[-1]})
        try:
            out = await crm_api_skill.handle(step.kind.value, ctx.inputs, writer=state["writer"],
                                             check_then_act=ctx.config.check_then_act)
        except SkillInputError as exc:
            await ctx.observe({"blocked": str(exc), "retryable": False, "skill": Skill.API_ESPOCRM.value})
            return WorkResult(summary=f"blocked: {exc}", data={"blocked": True, "error": str(exc)}, acted=False)
        data = dict(out["data"])
        await ctx.observe({"acted": out["acted"], **{k: data[k] for k in CLAIM_KEYS if data.get(k) is not None}})
        return WorkResult(summary=out["summary"], data=data, acted=bool(out["acted"]))

    async def aclose() -> None:
        if state["writer"] is not None:
            await state["writer"].aclose()
            state["writer"] = None

    handle.aclose = aclose  # type: ignore[attr-defined]
    return handle


def build_worker(r: aioredis.Redis, keys: Keys, agent_id: str, **kw: Any) -> Worker:
    return Worker(r, keys, api_card(agent_id), make_handler(**kw))


async def main() -> None:
    s = get_settings()
    r = connect()
    worker = build_worker(r, Keys(s.ledger_ns), s.agent_id)
    worker.install_signal_handlers()
    try:
        await worker.run()
    finally:
        await worker.handler.aclose()
        await r.aclose()


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(name)s %(levelname)s %(message)s")
    asyncio.run(main())
