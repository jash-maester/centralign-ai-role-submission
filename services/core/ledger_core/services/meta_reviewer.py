"""Entrypoint for the meta-reviewer service (Track J): consumes queue:review,
resolves review steps at/above threshold, escalates the rest to a human."""

from __future__ import annotations

import asyncio
import logging

from ledger_core import postconditions
from ledger_core.keys import Keys
from ledger_core.meta_reviewer import MetaReviewer
from ledger_core.redis_conn import connect
from ledger_core.settings import get_settings


async def main() -> None:
    s = get_settings()
    postconditions.load_all()
    r = connect()
    reviewer = MetaReviewer(r, Keys(s.ledger_ns), agent_id=s.agent_id or "meta-reviewer")
    reviewer.install_signal_handlers()
    try:
        await reviewer.run()
    finally:
        await reviewer.aclose()
        await r.aclose()


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(name)s %(levelname)s %(message)s")
    asyncio.run(main())
