"""GET /stream: server-sent events tailing ledger:events (plans/01 §10, H2).

- `id:` is the Redis stream id, `event:` the event type, `data:` the Event JSON.
- Resume: the `Last-Event-ID` header (or ?last_event_id=) continues after that id.
- Without a resume id: with ?run_id= the run's backlog is replayed from the
  start (the GUI's reducer rebuilds from it); without run_id only new events.
  ?from=now skips the backlog.
- A comment line (`: ping`) every 15 s keeps proxies from closing the stream.
- ?follow=false ends after the backlog; ?max_s= closes after that many seconds
  (handy for curl captures and tests).
"""

from __future__ import annotations

import asyncio
import json
import time
from collections.abc import AsyncIterator

from fastapi import APIRouter, Depends, Header, Query, Request
from fastapi.responses import StreamingResponse

from ledger_core.events import read_events
from ledger_core.keys import Keys
from ledger_core.protocol import Event

from ..deps import get_keys, get_r

router = APIRouter(tags=["stream"])

PING_S = 15.0
BLOCK_MS = 1000
BACKLOG_PAGE = 1000


def sse_frame(ev: Event) -> str:
    data = json.dumps(ev.model_dump(mode="json"), separators=(",", ":"))
    return f"id: {ev.id}\nevent: {ev.type.value}\ndata: {data}\n\n"


async def event_frames(
    r, keys: Keys, *, start: str, run_id: str | None, follow: bool, max_s: float | None,
    is_disconnected=None,
) -> AsyncIterator[str]:
    """`start` is a stream id to continue after, "-" for the beginning or "$" for now."""
    deadline = time.monotonic() + max_s if max_s else None
    yield "retry: 2000\n\n"
    last = start
    if start == "$":
        newest = await r.xrevrange(keys.events, count=1)
        last = newest[0][0] if newest else "0-0"
    # backlog
    while True:
        batch = await read_events(r, keys, after=last, count=BACKLOG_PAGE)
        for ev in batch:
            last = ev.id or last
            if run_id is None or ev.run_id == run_id:
                yield sse_frame(ev)
        if len(batch) < BACKLOG_PAGE:
            break
    if last == "-":
        last = "0-0"
    if not follow:
        return
    # live tail
    last_ping = time.monotonic()
    while True:
        if deadline and time.monotonic() >= deadline:
            return
        if is_disconnected and await is_disconnected():
            return
        block = BLOCK_MS
        if deadline:
            block = max(1, min(block, int((deadline - time.monotonic()) * 1000)))
        rows = await r.xread({keys.events: last}, block=block, count=200)
        for _stream, entries in rows or []:
            for sid, fields in entries:
                last = sid
                ev = Event.model_validate_json(fields["json"])
                ev.id = sid
                if run_id is None or ev.run_id == run_id:
                    yield sse_frame(ev)
        if time.monotonic() - last_ping >= PING_S:
            last_ping = time.monotonic()
            yield ": ping\n\n"
        await asyncio.sleep(0)


@router.get("/stream")
async def stream(
    request: Request,
    run_id: str | None = None,
    from_: str | None = Query(None, alias="from", pattern="^(start|now)$"),
    follow: bool = True,
    max_s: float | None = Query(None, gt=0, le=3600),
    last_event_id_q: str | None = Query(None, alias="last_event_id"),
    last_event_id: str | None = Header(None, alias="Last-Event-ID"),
    r=Depends(get_r), keys: Keys = Depends(get_keys),
) -> StreamingResponse:
    resume = last_event_id or last_event_id_q
    if resume:
        start = resume
    elif from_ == "now" or (from_ is None and run_id is None):
        start = "$"
    else:
        start = "-"
    frames = event_frames(r, keys, start=start, run_id=run_id, follow=follow, max_s=max_s,
                          is_disconnected=request.is_disconnected)
    return StreamingResponse(frames, media_type="text/event-stream", headers={
        "Cache-Control": "no-cache", "X-Accel-Buffering": "no", "Connection": "keep-alive",
    })
