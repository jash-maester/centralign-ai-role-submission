"""Entrypoint for the verifier service (Track A core). Track F adds the CRM /
Mailpit check context and the LLM judge by passing context_factory= and
judge= to Verifier (see ledger_core/verifier.py)."""

from __future__ import annotations

import asyncio
import logging

from ledger_core.keys import Keys
from ledger_core.redis_conn import connect
from ledger_core.settings import get_settings
from ledger_core.verifier import Verifier


async def main() -> None:
    s = get_settings()
    r = connect()
    verifier = Verifier(r, Keys(s.ledger_ns), agent_id=s.agent_id)
    verifier.install_signal_handlers()
    try:
        await verifier.run()
    finally:
        await r.aclose()


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(name)s %(levelname)s %(message)s")
    asyncio.run(main())
