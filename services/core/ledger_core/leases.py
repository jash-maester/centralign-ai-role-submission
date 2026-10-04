"""Leases with TTL, heartbeat and fencing tokens (plans/01-architecture.md §5, A3/A4).

    fence = await acquire(r, keys, step_id, owner, ttl_ms)   # None if someone holds it
    ok    = await heartbeat(r, keys, step_id, owner, ttl_ms)  # False once the lease is lost
    ok    = await release(r, keys, step_id, owner)            # compare-and-delete

acquire is `SET lease:{step} owner NX PX ttl` followed by `INCR fence:{step}`,
done in one Lua script so a crash can never leave a lease without a fresh
fence. The fence is monotonic per step and is what ledger.transition() checks:
a write carrying a fence lower than fence:{step} is stale and rejected.

A worker that dies (even SIGKILL) needs no cleanup: its lease key expires and
the reaper returns the step to `ready`.
"""

from __future__ import annotations

import redis.asyncio as aioredis

from .keys import Keys

_ACQUIRE = """
if redis.call('SET', KEYS[1], ARGV[1], 'NX', 'PX', ARGV[2]) then
  return redis.call('INCR', KEYS[2])
end
return 0
"""

_HEARTBEAT = """
if redis.call('GET', KEYS[1]) == ARGV[1] then
  return redis.call('PEXPIRE', KEYS[1], ARGV[2])
end
return 0
"""

_RELEASE = """
if redis.call('GET', KEYS[1]) == ARGV[1] then
  return redis.call('DEL', KEYS[1])
end
return 0
"""


async def acquire(r: aioredis.Redis, keys: Keys, step_id: str, owner: str, ttl_ms: int) -> int | None:
    """Take the lease. Returns the new fencing token, or None if it is held."""
    fence = await r.eval(_ACQUIRE, 2, keys.lease(step_id), keys.fence(step_id), owner, int(ttl_ms))
    return int(fence) or None


async def heartbeat(r: aioredis.Redis, keys: Keys, step_id: str, owner: str, ttl_ms: int) -> bool:
    """Extend the lease only if `owner` still holds it."""
    return bool(await r.eval(_HEARTBEAT, 1, keys.lease(step_id), owner, int(ttl_ms)))


async def release(r: aioredis.Redis, keys: Keys, step_id: str, owner: str) -> bool:
    """Delete the lease only if `owner` still holds it."""
    return bool(await r.eval(_RELEASE, 1, keys.lease(step_id), owner))


async def holder(r: aioredis.Redis, keys: Keys, step_id: str) -> str | None:
    return await r.get(keys.lease(step_id))


async def current_fence(r: aioredis.Redis, keys: Keys, step_id: str) -> int:
    return int(await r.get(keys.fence(step_id)) or 0)
