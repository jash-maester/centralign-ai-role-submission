"""Entrypoint for worker-browser-N (placeholder until Phase 4 / Tracks C + I)."""

import os

from ledger_core.services.idle import run

if __name__ == "__main__":
    run(os.environ.get("AGENT_ID", "worker-browser"), "Phase 4 / Tracks C + I")
