"""Helpers for the Track H API tests (not a test module).

`api_client(ns)` starts the real FastAPI app (lifespan connects to the test
Redis) and points it at the test's key namespace.
"""

from __future__ import annotations

from contextlib import contextmanager

from fastapi.testclient import TestClient

from ledger_core.keys import Keys


@contextmanager
def api_client(ns: str):
    from app.main import app

    with TestClient(app) as client:
        app.state.keys = Keys(ns)
        yield client


def sse_events(text: str) -> list[dict]:
    """Parse a text/event-stream body into [{id, event, data}] (comments skipped)."""
    out = []
    for block in text.split("\n\n"):
        frame: dict[str, str] = {}
        for line in block.splitlines():
            if not line or line.startswith(":"):
                continue
            name, _, value = line.partition(":")
            frame[name] = value.removeprefix(" ")
        if "data" in frame:
            out.append(frame)
    return out
