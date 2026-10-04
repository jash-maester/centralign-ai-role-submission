"""Entrypoint for worker-browser-N (Track I): skill browser.espocrm on the ledger.

One Chromium context per replica (Operator keyed by AGENT_ID), driven by the
shared worker loop (ledger_core.worker_base): lease + fence, heartbeats, claim.
SIGTERM releases the lease; SIGKILL / `docker kill` leaves it to expire and the
reaper requeues the step for the other replica (higher fence).
"""

from __future__ import annotations

import asyncio
import logging

from ledger_core.keys import Keys
from ledger_core.redis_conn import connect
from ledger_core.settings import get_settings
from ledger_core.worker_base import Worker

from .operator import Operator
from .worker import BrowserHandler, browser_card


async def main() -> None:
    s = get_settings()
    r = connect()
    op = Operator(s.agent_id)
    await op.start()
    worker = Worker(r, Keys(s.ledger_ns), browser_card(s.agent_id), BrowserHandler(op))
    worker.install_signal_handlers()
    try:
        await worker.run()
    finally:
        await op.close()
        await r.aclose()


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(name)s %(levelname)s %(message)s")
    asyncio.run(main())
