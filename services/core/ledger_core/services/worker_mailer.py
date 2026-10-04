"""Entrypoint for worker-mailer (Track K): skill email.send.

Sends an approved draft via SMTP to Mailpit only when its approval fact is
committed; honours RunConfig.dry_run; check-then-act through the Mailpit API
so a takeover never double-sends (workers/mailer.py).
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
from ledger_core.workers.mailer import make_handler


def mailer_card(agent_id: str, container: str = "worker-mailer") -> AgentCard:
    return worker_card(
        agent_id, "Mailer", {Skill.EMAIL_SEND.value: [StepKind.EMAIL_SEND.value]},
        side_effects=True, container=container,
        tools=[{"id": "smtp", "type": "smtp", "name": "SMTP (Mailpit)", "detail": "sends approved drafts only"},
               {"id": "mailpit-read", "type": "rest", "name": "Mailpit API (read)",
                "detail": "check-then-act: was this Message-ID already sent?"}],
    )


def build_worker(r: aioredis.Redis, keys: Keys, agent_id: str = "worker-mailer", **kw: Any) -> Worker:
    handler_kw = {k: kw.pop(k) for k in ("mailpit", "send") if k in kw}
    return Worker(r, keys, mailer_card(agent_id, kw.pop("container", "worker-mailer")), make_handler(**handler_kw),
                  **kw)


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
