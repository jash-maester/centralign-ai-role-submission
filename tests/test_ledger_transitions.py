"""A2/A4: the step state machine, roles and fencing, enforced by ledger.transition."""

from __future__ import annotations

import pytest

from ledger_core import bus, leases, ledger
from ledger_core.events import read_events
from ledger_core.protocol import LEGAL_TRANSITIONS, VERIFIER_ONLY_STATUSES, Envelope, EventType, StepStatus

from ledger_helpers import S, a_claim, a_verdict, drive_to, lease, new_run, new_step

LEGAL = [(src, dst) for src, dsts in LEGAL_TRANSITIONS.items() for dst in sorted(dsts)]
ILLEGAL = [(src, dst) for src in StepStatus for dst in StepStatus if dst not in LEGAL_TRANSITIONS[src]]


def _role_for(dst: StepStatus) -> str:
    return "verifier" if dst in VERIFIER_ONLY_STATUSES else "orchestrator"


@pytest.mark.parametrize("src,dst", LEGAL, ids=[f"{a.value}->{b.value}" for a, b in LEGAL])
async def test_every_legal_transition(r, keys, src, dst):
    step = await new_step(r, keys)
    info = await drive_to(r, keys, step, src)
    n_before = len(await read_events(r, keys))
    kw = {}
    if dst == S.LEASED:
        fence = await leases.acquire(r, keys, step.id, "w2", 15_000)
        got = await ledger.transition(r, keys, step.id, dst, actor="w2", actor_role="worker", fence=fence)
    elif dst == S.CLAIMED_DONE:
        got = await ledger.claim(r, keys, step.id, a_claim(info["worker"], info["fence"]))
    else:
        if dst in (S.VERIFIED, S.REJECTED):
            kw["verdict"] = a_verdict(dst == S.VERIFIED)
        got = await ledger.transition(r, keys, step.id, dst, actor="t", actor_role=_role_for(dst), **kw)
    assert got.status == dst
    assert (await ledger.get_step(r, keys, step.id)).status == dst
    new = (await read_events(r, keys))[n_before:]
    assert new[-1].type == ledger.TRANSITION_EVENT[dst]
    assert new[-1].payload["from"] == src.value and new[-1].payload["to"] == dst.value


@pytest.mark.parametrize("src,dst", ILLEGAL, ids=[f"{a.value}->{b.value}" for a, b in ILLEGAL])
async def test_every_illegal_transition_raises(r, keys, src, dst):
    step = await new_step(r, keys)
    await drive_to(r, keys, step, src)
    before = await ledger.get_step(r, keys, step.id)
    n_before = len(await read_events(r, keys))
    with pytest.raises(ledger.IllegalTransition):
        await ledger.transition(r, keys, step.id, dst, actor="t", actor_role="verifier",
                                verdict=a_verdict(True), claim=a_claim("w1", 1))
    after = await ledger.get_step(r, keys, step.id)
    assert after == before
    assert len(await read_events(r, keys)) == n_before


@pytest.mark.parametrize("target", sorted(VERIFIER_ONLY_STATUSES))
@pytest.mark.parametrize("role", ["worker", "orchestrator", "meta_reviewer", "reaper"])
async def test_only_the_verifier_enters_verifier_only_states(r, keys, target, role):
    step = await new_step(r, keys)
    src = S.VERIFIED if target == S.COMMITTED else S.CLAIMED_DONE
    info = await drive_to(r, keys, step, src)
    with pytest.raises(ledger.ForbiddenTransition):
        await ledger.transition(r, keys, step.id, target, actor="x", actor_role=role,
                                fence=info["fence"], verdict=a_verdict(True))
    assert (await ledger.get_step(r, keys, step.id)).status == src


async def test_workers_only_lease_claim_or_ask_for_review(r, keys):
    step = await new_step(r, keys)
    info = await drive_to(r, keys, step, S.LEASED)
    for target in (S.DEAD, S.LEASE_EXPIRED):
        with pytest.raises(ledger.ForbiddenTransition):
            await ledger.transition(r, keys, step.id, target, actor="w1", actor_role="worker", fence=info["fence"])
    with pytest.raises(ledger.ForbiddenTransition, match="fencing token"):
        await ledger.transition(r, keys, step.id, S.CLAIMED_DONE, actor="w1", actor_role="worker",
                                claim=a_claim("w1", 1))
    got = await ledger.transition(r, keys, step.id, S.REVIEW_REQUIRED, actor="w1", actor_role="worker",
                                  fence=info["fence"])
    assert got.status == S.REVIEW_REQUIRED and got.history[-1].outcome == "review_required"


async def test_stale_fence_write_is_rejected_and_logged(r, keys):
    step = await new_step(r, keys)
    await ledger.transition(r, keys, step.id, S.READY, actor="o", actor_role="orchestrator")
    old = await lease(r, keys, step.id, "w-old", ttl_ms=15_000)
    # w-old stalls; its lease vanishes and the reaper hands the step to w-new
    await r.delete(keys.lease(step.id))
    await ledger.expire_lease(r, keys, step.id)
    new = await lease(r, keys, step.id, "w-new")
    assert new > old

    with pytest.raises(ledger.StaleFenceError):
        await ledger.claim(r, keys, step.id, a_claim("w-old", old))
    with pytest.raises(ledger.StaleFenceError):
        await ledger.observe(r, keys, step.id, {"note": "late"}, actor="w-old", fence=old)

    s = await ledger.get_step(r, keys, step.id)
    assert s.status == S.LEASED and s.lease_owner == "w-new" and s.fence == new and s.claim is None
    stale = [e for e in await read_events(r, keys) if e.type == EventType.STEP_STALE_FENCE]
    assert len(stale) == 2
    assert stale[0].payload["fence"] == old and stale[0].payload["current_fence"] == new
    assert stale[0].actor == "w-old"
    # the current holder still can claim
    s = await ledger.claim(r, keys, step.id, a_claim("w-new", new))
    assert s.status == S.CLAIMED_DONE


async def test_late_claim_after_requeue_is_refused(r, keys):
    """Lease expired and the step is back in ready, nobody re-leased yet."""
    step = await new_step(r, keys)
    await ledger.transition(r, keys, step.id, S.READY, actor="o", actor_role="orchestrator")
    fence = await lease(r, keys, step.id, "w1")
    await r.delete(keys.lease(step.id))
    await ledger.expire_lease(r, keys, step.id)
    with pytest.raises(ledger.IllegalTransition):
        await ledger.claim(r, keys, step.id, a_claim("w1", fence))
    with pytest.raises(ledger.StaleFenceError):
        await ledger.observe(r, keys, step.id, {"x": 1}, actor="w1", fence=fence)


async def test_ready_enqueues_and_claim_enqueues_for_verifier(r, keys):
    step = await new_step(r, keys)
    await ledger.transition(r, keys, step.id, S.READY, actor="o", actor_role="orchestrator")
    rows = await r.xrange(keys.queue("file.parse"))
    assert len(rows) == 1
    env = Envelope.model_validate_json(rows[0][1]["envelope"])  # A8
    assert env.task_id == step.id and env.type == "task.submit" and env.to_skill.value == "file.parse"
    fence = await lease(r, keys, step.id)
    assert await r.sismember(keys.leased_steps, step.id)
    await ledger.claim(r, keys, step.id, a_claim("w1", fence))
    assert not await r.sismember(keys.leased_steps, step.id)
    rows = await r.xrange(keys.verify_queue)
    env = Envelope.model_validate_json(rows[0][1]["envelope"])
    assert env.task_id == step.id and env.type == "task.artifact" and env.fence == fence
    assert bus.envelope_fields(env)["step_id"] == step.id


async def test_history_records_attempts_claims_and_verdicts(r, keys):
    step = await new_step(r, keys)
    await drive_to(r, keys, step, S.CLAIMED_DONE)
    s = await ledger.reject(r, keys, step.id, a_verdict(False, "row 3 missing"))
    assert s.status == S.READY and s.attempt == 1
    a = s.history[0]
    assert a.attempt == 1 and a.worker == "w1" and a.claim.summary == "did it"
    assert a.verdict.reason == "row 3 missing" and a.outcome == "rejected" and a.ended_at
    fence = await lease(r, keys, step.id, "w2")
    await ledger.observe(r, keys, step.id, {"saw": "page"}, actor="w2", fence=fence)
    await ledger.claim(r, keys, step.id, a_claim("w2", fence, summary="second"))
    await leases.release(r, keys, step.id, "w2")
    s = await ledger.commit(r, keys, step.id, a_verdict(True, "fine"), {"k": 1})
    assert [h.outcome for h in s.history] == ["rejected", "committed"]
    assert s.history[1].observations[0]["saw"] == "page" and s.attempt == 2
    assert ledger.rejection_reasons(s) == ["row 3 missing"]


async def test_reject_policy_targets(r, keys):
    dead = await new_step(r, keys)
    await drive_to(r, keys, dead, S.CLAIMED_DONE)
    s = await ledger.reject(r, keys, dead.id, a_verdict(False, "nope"), then=S.DEAD)
    assert s.status == S.DEAD and s.history[-1].outcome == "rejected"
    hold = await new_step(r, keys)
    await drive_to(r, keys, hold, S.CLAIMED_DONE)
    s = await ledger.reject(r, keys, hold.id, a_verdict(False, "nope"), then=None)
    assert s.status == S.REJECTED


async def test_transition_can_reroute_skill_on_ready(r, keys):
    from ledger_core.protocol import Postcondition, Skill, StepKind

    step = await new_step(r, keys, kind=StepKind.CRM_CREATE_CONTACT, skill=Skill.BROWSER_ESPOCRM,
                          postcondition=Postcondition(check="crm.contact_exists"))
    await ledger.transition(r, keys, step.id, S.READY, actor="o", actor_role="orchestrator",
                            skill=Skill.API_ESPOCRM)
    assert await r.xlen(keys.queue("api.espocrm")) == 1
    with pytest.raises(ValueError):
        await ledger.transition(r, keys, step.id, S.DEAD, actor="o", actor_role="orchestrator", status=S.READY)


async def test_missing_step_raises(r, keys):
    with pytest.raises(ledger.NotFound):
        await ledger.transition(r, keys, "stp_nope", S.READY, actor="o", actor_role="orchestrator")


async def test_concurrent_transitions_one_wins(r, keys):
    import asyncio

    step = await new_step(r, keys)
    results = await asyncio.gather(
        *[ledger.transition(r, keys, step.id, S.READY, actor=f"o{i}", actor_role="orchestrator") for i in range(10)],
        return_exceptions=True,
    )
    assert sum(1 for x in results if not isinstance(x, Exception)) == 1
    assert all(isinstance(x, ledger.IllegalTransition) for x in results if isinstance(x, Exception))
    assert await r.xlen(keys.queue("file.parse")) == 1


async def test_runs_status_and_config(r, keys):
    from ledger_core.config import RunConfig
    from ledger_core.protocol import RunStatus

    run = await new_run(r, keys, "goal A")
    await new_run(r, keys, "goal B")
    assert [x.goal for x in await ledger.list_runs(r, keys)][:2] == ["goal B", "goal A"]
    await ledger.set_run_status(r, keys, run.id, RunStatus.RUNNING, actor="o")
    done = await ledger.set_run_status(r, keys, run.id, RunStatus.COMPLETED, actor="o")
    assert done.status == RunStatus.COMPLETED and done.finished_at
    types = [e.type for e in await read_events(r, keys, run_id=run.id)]
    assert types == [EventType.RUN_CREATED, EventType.RUN_STATUS, EventType.RUN_COMPLETED]

    assert await ledger.get_run_config(r, keys, run.id) == RunConfig()
    cfg = RunConfig(lease_ttl_s=5, crm_write_path="api")
    await ledger.set_run_config(r, keys, run.id, cfg, actor="api")
    assert await ledger.get_run_config(r, keys, run.id) == cfg
    assert await r.hget(keys.run_config(run.id), "config_hash") == cfg.config_hash()
    ev = (await read_events(r, keys, run_id=run.id))[-1]
    assert ev.type == EventType.RUN_CONFIG_UPDATED and ev.payload["diff"]["lease_ttl_s"] == {"from": 15, "to": 5}
