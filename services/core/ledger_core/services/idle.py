"""Placeholder entrypoint for a service whose build track has not landed yet.

Keeps the container up (so compose healthchecks and `make up` work from W0)
and says plainly that it does nothing. Each track replaces its service module.
"""

from __future__ import annotations

import logging
import os
import signal
import sys
import time


def run(service: str, phase: str) -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(name)s %(message)s")
    log = logging.getLogger(service)
    log.info("placeholder: %s is not implemented yet (%s); idling", service, phase)
    signal.signal(signal.SIGTERM, lambda *_: sys.exit(0))
    while True:
        time.sleep(3600)


if __name__ == "__main__":
    run(os.environ.get("AGENT_ID", "service"), "unassigned")
