"""Entrypoint for the verifier service: Track A core + Track F world handles
(read-only CRM reader, Mailpit client) and the LLM judge for soft checks (D4)."""

from __future__ import annotations

import asyncio
import logging

from ledger_core.keys import Keys
from ledger_core.redis_conn import connect
from ledger_core.settings import get_settings
from ledger_core.judge import make_judge
from ledger_core.verifier import Verifier, make_context_factory


async def main() -> None:
    s = get_settings()
    r = connect()
    factory = make_context_factory()
    verifier = Verifier(r, Keys(s.ledger_ns), agent_id=s.agent_id, context_factory=factory, judge=make_judge())
    verifier.install_signal_handlers()
    try:
        await verifier.run()
    finally:
        await factory.aclose()
        await r.aclose()


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(name)s %(levelname)s %(message)s")
    asyncio.run(main())
