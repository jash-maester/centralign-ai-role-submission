"""A3/A4: leases (SET NX PX + INCR fence), heartbeat and release with owner checks."""

from __future__ import annotations

import asyncio

from ledger_core import leases


async def test_acquire_is_exclusive_and_fences_increase(r, keys):
    assert await leases.acquire(r, keys, "s1", "a", 5000) == 1
    assert await leases.acquire(r, keys, "s1", "b", 5000) is None
    assert await leases.holder(r, keys, "s1") == "a"
    assert await leases.release(r, keys, "s1", "b") is False  # not the owner
    assert await leases.holder(r, keys, "s1") == "a"
    assert await leases.release(r, keys, "s1", "a") is True
    assert await leases.acquire(r, keys, "s1", "b", 5000) == 2
    assert await leases.current_fence(r, keys, "s1") == 2


async def test_heartbeat_extends_only_for_owner(r, keys):
    await leases.acquire(r, keys, "s1", "a", 300)
    assert await leases.heartbeat(r, keys, "s1", "b", 10_000) is False
    assert await leases.heartbeat(r, keys, "s1", "a", 10_000) is True
    assert await r.pttl(keys.lease("s1")) > 5_000


async def test_expired_lease_can_be_taken_over_with_a_higher_fence(r, keys):
    f1 = await leases.acquire(r, keys, "s1", "a", 200)
    await asyncio.sleep(0.35)
    assert await leases.heartbeat(r, keys, "s1", "a", 200) is False  # lost
    f2 = await leases.acquire(r, keys, "s1", "b", 5000)
    assert f2 == f1 + 1
    assert await leases.release(r, keys, "s1", "a") is False  # late release cannot free b's lease
    assert await leases.holder(r, keys, "s1") == "b"


async def test_racing_acquires_exactly_one_wins(r, keys):
    results = await asyncio.gather(*[leases.acquire(r, keys, "s1", f"w{i}", 5000) for i in range(25)])
    winners = [x for x in results if x is not None]
    assert winners == [1]
