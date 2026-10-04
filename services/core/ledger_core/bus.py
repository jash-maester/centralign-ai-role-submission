"""Skill queues on Redis Streams with consumer groups (plans/01-architecture.md §3-4, A6/A8).

Every bus message is one protocol.Envelope (A8), stored in the stream entry's
`envelope` field:

- queue:{skill}  task.submit / submitted   "this step is ready for your skill"
- queue:verify   task.artifact / completed "this step's claim awaits the verifier"

Consumers read through a consumer group (`workers` on skill queues, `verifiers`
on the verify queue) with their agent id as consumer name, so two consumers
never receive the same entry (A6). A consumer that dies leaves its entry
pending; `Consumer` periodically XAUTOCLAIMs entries idle longer than
`min_idle_ms`. That is always safe: the step's lease and the ledger's state
machine decide who actually works on a step, a message only says "look at it".

Normally callers do not enqueue directly: ledger.transition(..., to=READY)
enqueues on the step's skill queue and ledger.claim() enqueues on the verify
queue, both atomically with the state change.
"""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass

import redis.asyncio as aioredis
from pydantic import ValidationError
from redis.exceptions import ResponseError

from .keys import Keys
from .protocol import Envelope, Skill

log = logging.getLogger("ledger.bus")

WORKER_GROUP = "workers"
VERIFY_GROUP = "verifiers"


@dataclass
class Delivery:
    stream: str
    msg_id: str
    envelope: Envelope

    @property
    def step_id(self) -> str:
        return self.envelope.task_id


def task_envelope(step_id: str, run_id: str, skill: Skill | str, *, sender: str = "ledger", fence: int = 0) -> Envelope:
    return Envelope(
        run_id=run_id, task_id=step_id, to_skill=Skill(skill), type="task.submit",
        state="submitted", fence=fence, **{"from": sender},
    )


def artifact_envelope(step_id: str, run_id: str, skill: Skill | str, *, sender: str, fence: int) -> Envelope:
    return Envelope(
        run_id=run_id, task_id=step_id, to_skill=Skill(skill), type="task.artifact",
        state="completed", fence=fence, **{"from": sender},
    )


def envelope_fields(env: Envelope) -> dict[str, str]:
    """Stream entry fields for an envelope (use with pipeline.xadd inside a MULTI)."""
    return {"step_id": env.task_id, "envelope": env.model_dump_json(by_alias=True)}


async def enqueue(
    r: aioredis.Redis, keys: Keys, skill: Skill | str, step_id: str, *, run_id: str,
    sender: str = "ledger", fence: int = 0,
) -> str:
    env = task_envelope(step_id, run_id, skill, sender=sender, fence=fence)
    return await r.xadd(keys.queue(str(Skill(skill))), envelope_fields(env))


async def enqueue_verify(
    r: aioredis.Redis, keys: Keys, step_id: str, *, run_id: str, skill: Skill | str, sender: str, fence: int,
) -> str:
    env = artifact_envelope(step_id, run_id, skill, sender=sender, fence=fence)
    return await r.xadd(keys.verify_queue, envelope_fields(env))


async def ensure_group(r: aioredis.Redis, stream: str, group: str) -> None:
    """Create the consumer group (and the stream) if missing. Reads from the start,
    so steps enqueued before any consumer existed are not lost."""
    try:
        await r.xgroup_create(stream, group, id="0", mkstream=True)
    except ResponseError as exc:
        if "BUSYGROUP" not in str(exc):
            raise


def _decode(stream: str, msg_id: str, fields: dict) -> Delivery | None:
    try:
        return Delivery(stream, msg_id, Envelope.model_validate_json(fields["envelope"]))
    except (KeyError, ValidationError, TypeError) as exc:
        log.warning("dropping invalid bus message %s on %s: %s", msg_id, stream, exc)
        return None


class Consumer:
    """One agent reading one or more streams through a consumer group."""

    def __init__(
        self, r: aioredis.Redis, streams: list[str], group: str, name: str, *,
        min_idle_ms: int = 30_000, reclaim_every_s: float = 5.0,
    ) -> None:
        self.r, self.streams, self.group, self.name = r, list(streams), group, name
        self.min_idle_ms = min_idle_ms
        self.reclaim_every_s = reclaim_every_s
        self._last_reclaim = 0.0

    async def setup(self) -> None:
        for s in self.streams:
            await ensure_group(self.r, s, self.group)

    async def _reclaim(self) -> list[Delivery]:
        out: list[Delivery] = []
        for s in self.streams:
            res = await self.r.xautoclaim(s, self.group, self.name, self.min_idle_ms, start_id="0-0", count=1)
            for msg_id, fields in res[1] if res else []:
                if fields is None:
                    continue
                d = _decode(s, msg_id, fields)
                if d is None:
                    await self.r.xack(s, self.group, msg_id)
                else:
                    out.append(d)
        return out

    async def next(self, block_ms: int = 2000, count: int = 1) -> list[Delivery]:
        """Our own pending entries (after a restart) first, then idle entries of
        dead consumers, then new entries. Blocks up to block_ms."""
        own = await self.r.xreadgroup(self.group, self.name, {s: "0" for s in self.streams}, count=count)
        got = await self._collect(own)
        if got:
            return got
        if time.monotonic() - self._last_reclaim >= self.reclaim_every_s:
            self._last_reclaim = time.monotonic()
            got = await self._reclaim()
            if got:
                return got
        rows = await self.r.xreadgroup(
            self.group, self.name, {s: ">" for s in self.streams}, count=count, block=block_ms
        )
        return await self._collect(rows)

    async def _collect(self, rows) -> list[Delivery]:
        out: list[Delivery] = []
        for stream, entries in rows or []:
            for msg_id, fields in entries:
                if not fields:  # entry deleted from the stream while pending
                    await self.r.xack(stream, self.group, msg_id)
                    continue
                d = _decode(stream, msg_id, fields)
                if d is None:
                    await self.r.xack(stream, self.group, msg_id)
                else:
                    out.append(d)
        return out

    async def ack(self, delivery: Delivery) -> None:
        await self.r.xack(delivery.stream, self.group, delivery.msg_id)


def skill_consumer(r: aioredis.Redis, keys: Keys, skills: list[Skill | str], name: str, **kw) -> Consumer:
    return Consumer(r, [keys.queue(str(Skill(s))) for s in skills], WORKER_GROUP, name, **kw)


def verify_consumer(r: aioredis.Redis, keys: Keys, name: str, **kw) -> Consumer:
    return Consumer(r, [keys.verify_queue], VERIFY_GROUP, name, **kw)
