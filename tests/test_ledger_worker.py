"""Worker loop, reaper and verifier together (A1, A3, A5, A6, A7, D3, F2)."""

from __future__ import annotations

import asyncio
import os
import signal
import subprocess
import sys

from ledger_helpers import CSV, S, new_run, new_step, wait_for

from ledger_core import agents, ledger, reaper
from ledger_core.config import RunConfig
from ledger_core.events import read_events
from ledger_core.protocol import EventType, StepStatus
from ledger_core.verifier import Verifier
from ledger_core.worker_base import WorkContext, Worker, WorkResult, take_fault_shot, worker_card
from ledger_core.workers.parser import make_handler

CARD = {"file.parse": ["file.parse"]}


def parser_worker(r, keys, agent_id="worker-parser", handler=None, **kw) -> Worker:
    return Worker(r, keys, worker_card(agent_id, "parser", CARD), handler or make_handler("/repo/data"),
                  block_ms=150, **kw)


async def ready_step(r, keys, run_id=None, **kw):
    step = await new_step(r, keys, run_id, **kw)
    await ledger.transition(r, keys, step.id, S.READY, actor="test", actor_role="orchestrator")
    return step


async def status(r, keys, step_id) -> StepStatus:
    return (await ledger.get_step(r, keys, step_id)).status


async def test_parse_step_end_to_end_and_replay(r, keys):
    step = await ready_step(r, keys)
    worker, verifier = parser_worker(r, keys), Verifier(r, keys, block_ms=150)
    assert await worker.run_until_idle() == 1
    assert await status(r, keys, step.id) == S.CLAIMED_DONE
    assert await ledger.get_facts(r, keys, step.run_id) == {}  # A5: claim is not a fact
    assert await verifier.run_until_idle() == 1
    s = await ledger.get_step(r, keys, step.id)
    assert s.status == S.COMMITTED and s.history[0].worker == "worker-parser"
    assert s.history[0].observations[0]["usable"] == 10
    facts = await ledger.get_facts(r, keys, step.run_id)
    assert len([k for k in facts if k.startswith("lead:")]) == 10
    path = [e.payload["to"] for e in await read_events(r, keys, run_id=step.run_id)
            if e.step_id == step.id and "to" in e.payload]
    assert path == ["ready", "leased", "claimed_done", "verified", "committed"]
    # A1: replaying the events rebuilds every step status
    assert await ledger.rebuild_statuses_from_events(r, keys, step.run_id) == {step.id: S.COMMITTED}


async def test_replay_rebuilds_mixed_statuses(r, keys):
    run = await new_run(r, keys)
    a = await ready_step(r, keys, run.id)
    b = await new_step(r, keys, run.id, max_attempts=1)
    c = await new_step(r, keys, run.id)
    await ledger.transition(r, keys, b.id, S.READY, actor="o", actor_role="orchestrator")
    calls = {"n": 0}

    async def flaky(step, ctx):
        calls["n"] += 1
        return WorkResult(summary="nothing", data={}) if step.id == b.id else await make_handler("/repo/data")(step, ctx)

    await parser_worker(r, keys, handler=flaky).run_until_idle()
    await Verifier(r, keys, block_ms=150).run_until_idle()
    await ledger.transition(r, keys, c.id, S.REPLANNED, actor="o", actor_role="orchestrator")
    actual = {s.id: s.status for s in await ledger.list_steps(r, keys, run.id)}
    assert actual == {a.id: S.COMMITTED, b.id: S.DEAD, c.id: S.REPLANNED}
    assert await ledger.rebuild_statuses_from_events(r, keys, run.id) == actual


async def test_rejection_reason_reaches_next_attempt(r, keys):
    step = await ready_step(r, keys)
    seen: list[list[str]] = []
    real = make_handler("/repo/data")

    async def learner(step, ctx: WorkContext):
        seen.append(ctx.rejection_reasons)
        if not ctx.attempts:
            return WorkResult(summary="lazy", data={"rows": [], "flagged": []})
        assert ctx.attempts[0].claim.summary == "lazy"
        return await real(step, ctx)

    worker, verifier = parser_worker(r, keys, handler=learner), Verifier(r, keys, block_ms=150)
    await worker.run_until_idle()
    await verifier.run_until_idle()
    s = await ledger.get_step(r, keys, step.id)
    assert s.status == S.READY and "row accounting" in s.history[0].verdict.reason
    await worker.run_until_idle()
    await verifier.run_until_idle()
    s = await ledger.get_step(r, keys, step.id)
    assert s.status == S.COMMITTED and s.attempt == 2
    assert seen[0] == [] and "row accounting" in seen[1][0]


async def test_false_claim_fault_rejected_then_retry_commits(r, keys):
    step = await ready_step(r, keys)
    await r.hset(keys.faults, "false_claim", 1)
    worker, verifier = parser_worker(r, keys), Verifier(r, keys, block_ms=150)
    await worker.run_until_idle()
    s = await ledger.get_step(r, keys, step.id)
    assert s.claim.acted is False and s.claim.data == {}
    assert not await r.hexists(keys.faults, "false_claim")  # one shot consumed
    await verifier.run_until_idle()
    assert await status(r, keys, step.id) == S.READY
    await worker.run_until_idle()
    await verifier.run_until_idle()
    s = await ledger.get_step(r, keys, step.id)
    assert s.status == S.COMMITTED
    assert [h.outcome for h in s.history] == ["rejected", "committed"]
    types = [e.type for e in await read_events(r, keys, run_id=step.run_id) if e.step_id == step.id]
    assert EventType.FAULT_INJECTED in types


async def test_fault_shots_are_scoped_and_counted(r, keys):
    await r.hset(keys.faults, mapping={"false_claim:browser.espocrm": 2, "model_outage": "on"})
    assert await take_fault_shot(r, keys, "false_claim", skill="file.parse") is None
    assert await take_fault_shot(r, keys, "false_claim", skill="browser.espocrm") == "false_claim:browser.espocrm"
    assert await r.hget(keys.faults, "false_claim:browser.espocrm") == "1"
    assert await take_fault_shot(r, keys, "false_claim", skill="browser.espocrm")
    assert await take_fault_shot(r, keys, "false_claim", skill="browser.espocrm") is None
    assert await take_fault_shot(r, keys, "model_outage") == "model_outage"
    assert await take_fault_shot(r, keys, "model_outage") == "model_outage"  # "on" stays on


async def test_two_workers_racing_for_one_step_exactly_one_leases(r, keys):
    step = await ready_step(r, keys)
    from ledger_core import bus

    for _ in range(3):  # duplicate deliveries of the same step
        await bus.enqueue(r, keys, "file.parse", step.id, run_id=step.run_id)
    gate = asyncio.Event()

    async def slow(step, ctx):
        await gate.wait()
        return WorkResult(summary="ok", data={})

    w1, w2 = parser_worker(r, keys, "p1", slow), parser_worker(r, keys, "p2", slow)
    t1 = asyncio.create_task(w1.run())
    t2 = asyncio.create_task(w2.run())
    try:
        await wait_for(lambda: status_is(r, keys, step.id, S.LEASED))
        await asyncio.sleep(0.5)  # let the other deliveries be consumed and refused
        gate.set()
        await wait_for(lambda: status_is(r, keys, step.id, S.CLAIMED_DONE))
    finally:
        w1.stop()
        w2.stop()
        await asyncio.gather(t1, t2)
    leased = [e for e in await read_events(r, keys, run_id=step.run_id) if e.type == EventType.STEP_LEASED]
    assert len(leased) == 1
    assert len((await ledger.get_step(r, keys, step.id)).history) == 1


async def status_is(r, keys, step_id, want):
    return await status(r, keys, step_id) == want


async def test_agent_card_and_liveness_show_current_step(r, keys):
    step = await ready_step(r, keys)
    gate = asyncio.Event()

    async def slow(step, ctx):
        await gate.wait()
        return WorkResult(summary="ok", data={})

    w = parser_worker(r, keys, "p-live", slow)
    t = asyncio.create_task(w.run())
    try:
        await wait_for(lambda: status_is(r, keys, step.id, S.LEASED))
        [a] = await agents.list_agents(r, keys)
        assert a["card"].id == "p-live" and a["alive"] and a["current_step"] == step.id and a["fence"] == 1
        gate.set()
        await wait_for(lambda: status_is(r, keys, step.id, S.CLAIMED_DONE))
        assert (await agents.get_liveness(r, keys, "p-live"))["current_step"] is None
    finally:
        w.stop()
        await t
    # stopped: liveness gone -> the reaper reports it lost exactly once
    watch = reaper.AgentWatch()
    assert await watch.sweep(r, keys) == ["p-live"]
    assert await watch.sweep(r, keys) == []
    assert [e.type for e in await read_events(r, keys)].count(EventType.AGENT_LOST) == 1


async def test_sigterm_style_stop_releases_lease_for_fast_takeover(r, keys):
    step = await ready_step(r, keys)

    async def forever(step, ctx):
        await asyncio.sleep(3600)

    w = parser_worker(r, keys, "p-stop", forever)
    t = asyncio.create_task(w.run())
    await wait_for(lambda: status_is(r, keys, step.id, S.LEASED))
    w.stop()
    await t
    assert not await r.exists(keys.lease(step.id))
    assert (await reaper.reap_once(r, keys))["requeued"] == [step.id]
    await parser_worker(r, keys, "p-next").run_until_idle()
    assert await status(r, keys, step.id) == S.CLAIMED_DONE


VICTIM = """
import asyncio
from ledger_core.keys import Keys
from ledger_core.redis_conn import connect
from ledger_core.worker_base import Worker, worker_card

async def slow(step, ctx):
    await asyncio.sleep(3600)

async def main():
    w = Worker(connect(), Keys({ns!r}), worker_card("victim", "victim", {{"file.parse": ["file.parse"]}}),
               slow, block_ms=200)
    await w.run()

asyncio.run(main())
"""


async def test_killed_worker_mid_step_is_reaped_and_taken_over(r, keys):
    """SIGKILL a worker process holding a lease: the lease expires, the reaper
    requeues the step and a second worker completes it (A3, F1)."""
    run = await new_run(r, keys)
    await ledger.set_run_config(r, keys, run.id, RunConfig(lease_ttl_s=3), actor="test")
    step = await ready_step(r, keys, run.id)
    proc = subprocess.Popen([sys.executable, "-c", VICTIM.format(ns=keys.ns)], env=dict(os.environ))
    try:
        await wait_for(lambda: status_is(r, keys, step.id, S.LEASED), timeout=20)
        assert (await ledger.get_step(r, keys, step.id)).lease_owner == "victim"
        os.kill(proc.pid, signal.SIGKILL)
        proc.wait(timeout=5)
    finally:
        if proc.poll() is None:
            proc.kill()

    loop = asyncio.get_running_loop()
    killed_at = loop.time()

    async def reaped():
        await reaper.reap_once(r, keys)
        return await status_is(r, keys, step.id, S.READY)

    await wait_for(reaped, timeout=20, interval=0.5)
    assert loop.time() - killed_at < 20  # A3: back in ready within 20s
    await parser_worker(r, keys, "rescuer").run_until_idle()
    await Verifier(r, keys, block_ms=150).run_until_idle()
    s = await ledger.get_step(r, keys, step.id)
    assert s.status == S.COMMITTED
    assert [(h.worker, h.outcome) for h in s.history] == [("victim", "lease_expired"), ("rescuer", "committed")]
    assert s.history[1].fence > s.history[0].fence
    types = [e.type for e in await read_events(r, keys, run_id=run.id) if e.step_id == step.id]
    assert EventType.STEP_LEASE_EXPIRED in types


async def test_lost_lease_cancels_handler_and_nothing_is_claimed(r, keys):
    run = await new_run(r, keys)
    await ledger.set_run_config(r, keys, run.id, RunConfig(lease_ttl_s=3), actor="test")
    step = await ready_step(r, keys, run.id)
    cancelled = asyncio.Event()

    async def slow(step, ctx):
        try:
            await asyncio.sleep(3600)
        except asyncio.CancelledError:
            cancelled.set()
            raise

    w = parser_worker(r, keys, "p-lost", slow)
    t = asyncio.create_task(w.run())
    try:
        await wait_for(lambda: status_is(r, keys, step.id, S.LEASED))
        await r.delete(keys.lease(step.id))  # e.g. a long GC pause let it expire
        await asyncio.wait_for(cancelled.wait(), timeout=5)
    finally:
        w.stop()
        await t
    assert await status(r, keys, step.id) == S.LEASED  # untouched until the reaper runs
    types = [e.type for e in await read_events(r, keys, run_id=run.id)]
    assert EventType.STEP_HEARTBEAT_LOST in types and EventType.STEP_CLAIMED not in types


async def test_handler_exception_becomes_error_claim(r, keys):
    step = await ready_step(r, keys)

    async def broken(step, ctx):
        raise RuntimeError("csv on fire")

    await parser_worker(r, keys, handler=broken).run_until_idle()
    s = await ledger.get_step(r, keys, step.id)
    assert s.status == S.CLAIMED_DONE and "csv on fire" in s.claim.data["error"] and not s.claim.acted
    assert s.history[0].observations[0]["error"].startswith("RuntimeError")
    await Verifier(r, keys, block_ms=150).run_until_idle()
    assert await status(r, keys, step.id) == S.READY


async def test_inputs_resolved_from_committed_facts_only(r, keys):
    run = await new_run(r, keys)
    await ledger.commit_fact(r, keys, run.id, "lead:1", {"email": "a@b.co"}, source_step="stp_0", actor="verifier")
    step = await ready_step(r, keys, run.id, inputs={"lead": "fact:lead:1", "file": CSV, "x": ["fact:lead:1"]})
    got = {}

    async def h(step, ctx):
        got.update(ctx.inputs)
        got["facts"] = ctx.facts
        return WorkResult(summary="ok")

    await parser_worker(r, keys, handler=h).run_until_idle()
    assert got["lead"] == {"email": "a@b.co"} and got["x"] == [{"email": "a@b.co"}]
    assert got["facts"] == {"lead:1": {"email": "a@b.co"}}
    missing = await new_step(r, keys, run.id, inputs={"lead": "fact:lead:99"})
    try:
        await ledger.resolve_inputs(r, keys, missing)
        raise AssertionError("expected MissingFact")
    except ledger.MissingFact as exc:
        assert "lead:99" in str(exc)
