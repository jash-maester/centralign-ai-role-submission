"""A6/A8: skill queues with consumer groups; every message is an Envelope."""

from __future__ import annotations

from ledger_core import bus
from ledger_core.protocol import Envelope


async def test_two_consumers_never_get_the_same_message(r, keys):
    a = bus.skill_consumer(r, keys, ["browser.espocrm"], "worker-browser-1")
    b = bus.skill_consumer(r, keys, ["browser.espocrm"], "worker-browser-2")
    await a.setup()
    await b.setup()
    for i in range(20):
        await bus.enqueue(r, keys, "browser.espocrm", f"stp_{i}", run_id="run_x")
    got_a, got_b = [], []
    for _ in range(20):
        for consumer, got in ((a, got_a), (b, got_b)):
            for d in await consumer.next(block_ms=50):
                got.append(d.step_id)
                await consumer.ack(d)
    assert got_a and got_b
    assert set(got_a).isdisjoint(got_b) and len(got_a) + len(got_b) == 20
    assert set(got_a) | set(got_b) == {f"stp_{i}" for i in range(20)}


async def test_messages_are_envelopes_and_enqueued_before_group_exists(r, keys):
    await bus.enqueue(r, keys, "file.parse", "stp_1", run_id="run_1", sender="orchestrator")
    c = bus.skill_consumer(r, keys, ["file.parse"], "p1")
    await c.setup()
    [d] = await c.next(block_ms=50)
    assert isinstance(d.envelope, Envelope)
    assert d.envelope.run_id == "run_1" and d.envelope.from_ == "orchestrator"
    assert d.envelope.type == "task.submit" and d.envelope.state == "submitted"
    await c.ack(d)
    assert await c.next(block_ms=50) == []


async def test_invalid_messages_are_dropped(r, keys):
    c = bus.skill_consumer(r, keys, ["file.parse"], "p1")
    await c.setup()
    await r.xadd(keys.queue("file.parse"), {"junk": "1"})
    assert await c.next(block_ms=50) == []
    assert (await r.xpending(keys.queue("file.parse"), bus.WORKER_GROUP))["pending"] == 0


async def test_dead_consumers_pending_message_is_reclaimed(r, keys):
    dead = bus.skill_consumer(r, keys, ["file.parse"], "dead")
    alive = bus.skill_consumer(r, keys, ["file.parse"], "alive", min_idle_ms=100, reclaim_every_s=0)
    await dead.setup()
    await bus.enqueue(r, keys, "file.parse", "stp_1", run_id="run_1")
    [d] = await dead.next(block_ms=50)  # delivered, never acked (SIGKILL)
    assert await alive.next(block_ms=50) == []
    import asyncio

    await asyncio.sleep(0.15)
    [d2] = await alive.next(block_ms=50)
    assert d2.step_id == "stp_1" and d2.msg_id == d.msg_id
    await alive.ack(d2)


async def test_verify_queue_has_its_own_group(r, keys):
    await bus.enqueue_verify(r, keys, "stp_1", run_id="run_1", skill="file.parse", sender="w", fence=3)
    v = bus.verify_consumer(r, keys, "verifier")
    await v.setup()
    [d] = await v.next(block_ms=50)
    assert d.envelope.type == "task.artifact" and d.envelope.fence == 3
