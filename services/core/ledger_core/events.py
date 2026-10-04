"""Append-only event log (ledger:events). Shared by every service.

Events are never mutated. ledger.py builds state transitions on top of this;
anything that needs to record what happened (config edits, LLM calls, faults)
calls append_event directly.
"""

from __future__ import annotations

from collections.abc import AsyncIterator

import redis.asyncio as aioredis

from .keys import Keys
from .protocol import Event


def encode_event(event: Event) -> dict[str, str]:
    """Stream fields for one event. Use with pipeline.xadd(keys.events, ...) when an
    event must be written in the same MULTI/EXEC as the state change it records."""
    return {"json": event.model_dump_json(exclude={"id"})}


async def append_event(r: aioredis.Redis, keys: Keys, event: Event) -> str:
    """Append and return the stream id (also set on event.id)."""
    stream_id = await r.xadd(keys.events, encode_event(event))
    event.id = stream_id
    return stream_id


def _decode(stream_id: str, fields: dict) -> Event:
    ev = Event.model_validate_json(fields["json"])
    ev.id = stream_id
    return ev


async def read_events(
    r: aioredis.Redis, keys: Keys, *, after: str = "-", count: int = 1000, run_id: str | None = None
) -> list[Event]:
    start = after if after == "-" else f"({after}"
    rows = await r.xrange(keys.events, min=start, count=count)
    events = [_decode(sid, f) for sid, f in rows]
    return [e for e in events if run_id is None or e.run_id == run_id]


async def tail_events(
    r: aioredis.Redis, keys: Keys, *, last_id: str = "$", block_ms: int = 5000, run_id: str | None = None
) -> AsyncIterator[Event]:
    """Yield new events forever (SSE, CLI tail). Pass last_id to resume."""
    while True:
        rows = await r.xread({keys.events: last_id}, block=block_ms, count=200)
        for _stream, entries in rows or []:
            for sid, fields in entries:
                last_id = sid
                ev = _decode(sid, fields)
                if run_id is None or ev.run_id == run_id:
                    yield ev
