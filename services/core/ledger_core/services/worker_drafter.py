"""Entrypoint for worker-drafter (Track K): skill email.draft.

Drafts one follow-up email per eligible lead from committed facts + the
playbook's Follow-up policy, with llm.complete(role="worker")
(workers/drafter.py). Never sends anything.
"""

from __future__ import annotations

import asyncio
import logging
from typing import Any

import redis.asyncio as aioredis

from ledger_core.keys import Keys
from ledger_core.protocol import AgentCard, Skill, StepKind
from ledger_core.redis_conn import connect
from ledger_core.settings import get_settings
from ledger_core.worker_base import Worker, worker_card
from ledger_core.workers.drafter import make_handler


def drafter_card(agent_id: str, container: str = "worker-drafter") -> AgentCard:
    return worker_card(
        agent_id, "Email drafter", {Skill.EMAIL_DRAFT.value: [StepKind.EMAIL_DRAFT.value]},
        side_effects=False, model_role="worker", container=container,
        tools=[{"id": "llm-worker", "type": "llm", "name": "LLM (role worker)",
                "detail": "one call per draft; template + tone rules from the playbook"}],
    )


def build_worker(r: aioredis.Redis, keys: Keys, agent_id: str = "worker-drafter", **kw: Any) -> Worker:
    playbook_dir = kw.pop("playbook_dir", None)
    return Worker(r, keys, drafter_card(agent_id, kw.pop("container", "worker-drafter")),
                  make_handler(playbook_dir=playbook_dir), **kw)


async def main() -> None:
    s = get_settings()
    r = connect()
    worker = build_worker(r, Keys(s.ledger_ns), s.agent_id)
    worker.install_signal_handlers()
    try:
        await worker.run()
    finally:
        await r.aclose()


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(name)s %(levelname)s %(message)s")
    asyncio.run(main())
