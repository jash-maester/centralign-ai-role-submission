"""Entrypoint for the orchestrator service (Track G): understand, plan, fan-out,
release, replan, finish; runs the lease reaper (plans/01 §5)."""

from __future__ import annotations

import asyncio
import logging

from ledger_core import postconditions
from ledger_core.keys import Keys
from ledger_core.orchestrator import Orchestrator
from ledger_core.redis_conn import connect
from ledger_core.settings import get_settings


async def main() -> None:
    s = get_settings()
    postconditions.load_all()
    r = connect()
    orch = Orchestrator(r, Keys(s.ledger_ns), agent_id=s.agent_id or "orchestrator")
    orch.install_signal_handlers()
    try:
        await orch.run()
    finally:
        await r.aclose()


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(name)s %(levelname)s %(message)s")
    asyncio.run(main())
