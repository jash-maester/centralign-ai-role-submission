"""Entrypoint for the worker-parser service (Track A): skill file.parse."""

from __future__ import annotations

import asyncio
import logging

from ledger_core.keys import Keys
from ledger_core.protocol import AgentCard, Skill, StepKind
from ledger_core.redis_conn import connect
from ledger_core.settings import get_settings
from ledger_core.worker_base import Worker, worker_card
from ledger_core.workers.parser import make_handler


def parser_card(agent_id: str) -> AgentCard:
    return worker_card(
        agent_id, "Attendee file parser", {Skill.FILE_PARSE.value: [StepKind.FILE_PARSE.value]},
        side_effects=False, container="worker-parser",
        tools=[{"id": "csv", "type": "function", "name": "CSV reader + normaliser",
                "detail": "headers, trim, lowercase emails, title-case names, E.164 phones, in-file dedupe"}],
    )


async def main() -> None:
    s = get_settings()
    r = connect()
    worker = Worker(r, Keys(s.ledger_ns), parser_card(s.agent_id), make_handler(s.data_dir))
    worker.install_signal_handlers()
    try:
        await worker.run()
    finally:
        await r.aclose()


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(name)s %(levelname)s %(message)s")
    asyncio.run(main())
