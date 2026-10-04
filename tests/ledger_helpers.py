"""Helpers shared by the Track A ledger tests (not a test module)."""

from __future__ import annotations

import asyncio
from typing import Any

from ledger_core import leases, ledger
from ledger_core.protocol import Claim, Postcondition, Run, Skill, Step, StepKind, StepStatus, Verdict

S = StepStatus
CSV = "/repo/data/event_attendees.csv"


async def new_run(r, keys, goal: str = "test run") -> Run:
    return await ledger.create_run(r, keys, goal=goal, actor="test")


def make_step(run_id: str, **kw: Any) -> Step:
    base: dict[str, Any] = dict(
        run_id=run_id, kind=StepKind.FILE_PARSE, skill=Skill.FILE_PARSE, title="parse",
        inputs={"file": CSV}, postcondition=Postcondition(check="file.parsed_rows", args={"file": CSV}),
    )
    base.update(kw)
    return Step(**base)


async def new_step(r, keys, run_id: str | None = None, **kw: Any) -> Step:
    if run_id is None:
        run_id = (await new_run(r, keys)).id
    return await ledger.create_step(r, keys, make_step(run_id, **kw), actor="test")


def a_claim(worker: str, fence: int, **kw: Any) -> Claim:
    return Claim(worker=worker, fence=fence, summary=kw.pop("summary", "did it"), **kw)


def a_verdict(ok: bool, reason: str = "because") -> Verdict:
    return Verdict(ok=ok, check="file.parsed_rows", reason=reason)


async def lease(r, keys, step_id: str, worker: str = "w1", ttl_ms: int = 15_000) -> int:
    fence = await leases.acquire(r, keys, step_id, worker, ttl_ms)
    assert fence is not None
    await ledger.transition(r, keys, step_id, S.LEASED, actor=worker, actor_role="worker", fence=fence)
    return fence


async def drive_to(r, keys, step: Step, target: StepStatus) -> dict[str, Any]:
    """Move a fresh planned step to `target` along legal edges. Returns
    {"worker", "fence"} of the last lease taken (if any)."""
    info: dict[str, Any] = {"worker": None, "fence": None}
    sid = step.id

    async def to(status: StepStatus, role: str = "orchestrator", **kw: Any) -> None:
        await ledger.transition(r, keys, sid, status, actor=role, actor_role=role, **kw)

    async def leased() -> None:
        await to(S.READY)
        info["worker"], info["fence"] = "w1", await lease(r, keys, sid, "w1")

    async def claimed() -> None:
        await leased()
        await ledger.claim(r, keys, sid, a_claim("w1", info["fence"]))
        await leases.release(r, keys, sid, "w1")  # as worker_base does after claiming

    paths = {
        S.PLANNED: lambda: asyncio.sleep(0),
        S.READY: lambda: to(S.READY),
        S.LEASED: leased,
        S.CLAIMED_DONE: claimed,
        S.REPLANNED: lambda: to(S.REPLANNED),
        S.DEAD: lambda: to(S.DEAD),
    }
    if target in paths:
        await paths[target]()
    elif target in (S.VERIFIED, S.COMMITTED, S.REJECTED):
        await claimed()
        if target == S.REJECTED:
            await to(S.REJECTED, "verifier", verdict=a_verdict(False))
        else:
            await to(S.VERIFIED, "verifier", verdict=a_verdict(True))
            if target == S.COMMITTED:
                await to(S.COMMITTED, "verifier")
    elif target == S.LEASE_EXPIRED:
        await leased()
        await to(S.LEASE_EXPIRED, "reaper")
    elif target in (S.REVIEW_REQUIRED, S.INPUT_REQUIRED):
        await leased()
        await to(S.REVIEW_REQUIRED)
        if target == S.INPUT_REQUIRED:
            await to(S.INPUT_REQUIRED, "meta_reviewer")
    got = await ledger.get_step(r, keys, sid)
    assert got.status == target, (got.status, target)
    return info


async def wait_for(pred, timeout: float = 10.0, interval: float = 0.1):
    deadline = asyncio.get_running_loop().time() + timeout
    while True:
        res = await pred()
        if res:
            return res
        if asyncio.get_running_loop().time() > deadline:
            raise AssertionError("condition not met in time")
        await asyncio.sleep(interval)
