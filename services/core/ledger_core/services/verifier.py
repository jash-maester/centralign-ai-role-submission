"""Entrypoint for the verifier service: Track A core + Track F world handles
(read-only CRM reader, Mailpit client) and the LLM judge for soft checks (D4).

The service uses the orchestrator's replan-aware reject policy, the same one the
in-process `local_verifier` uses: after `replan_after_rejections` rejections of a
CRM step, when the run's `crm_write_path` offers another skill (`auto`), the step
is held in `rejected` so the orchestrator moves the lane (browser -> api.espocrm)
instead of retrying the failing path (plans/01 §5 "Replan", B5).
"""

from __future__ import annotations

import asyncio
import logging

import redis.asyncio as aioredis

from ledger_core.judge import make_judge
from ledger_core.keys import Keys
from ledger_core.orchestrator_replan import reject_policy
from ledger_core.redis_conn import connect
from ledger_core.settings import get_settings
from ledger_core.verifier import ContextFactory, Verifier, make_context_factory


def build_verifier(r: aioredis.Redis, keys: Keys, agent_id: str, factory: ContextFactory) -> Verifier:
    return Verifier(r, keys, agent_id=agent_id, context_factory=factory, judge=make_judge(),
                    reject_policy=reject_policy)


async def main() -> None:
    s = get_settings()
    r = connect()
    factory = make_context_factory()
    verifier = build_verifier(r, Keys(s.ledger_ns), s.agent_id, factory)
    verifier.install_signal_handlers()
    try:
        await verifier.run()
    finally:
        await factory.aclose()
        await r.aclose()


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(name)s %(levelname)s %(message)s")
    asyncio.run(main())
