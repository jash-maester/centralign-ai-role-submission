"""Run config store (plans/01 §6a): defaults, hash, diff events, determinism (F6, F7)."""

from __future__ import annotations

from pathlib import Path

import pytest

from ledger_core import run_config as rc
from ledger_core.config import RunConfig
from ledger_core.events import read_events
from ledger_core.playbook import load
from ledger_core.protocol import EventType
from ledger_core.settings import Settings


@pytest.fixture
def pb(repo):
    return load(playbook_dir=Path(repo, "playbooks"))


async def events_of(r, keys, type_):
    return [e for e in await read_events(r, keys) if e.type == type_]


def test_defaults_playbook_over_env(pb):
    env = Settings(review_auto_threshold=0.5, approval_auto_threshold=0.5, determinism=0.3)
    cfg = rc.defaults(pb, settings=env)
    assert cfg.review_auto_threshold == 0.80 and cfg.approval_auto_threshold == 0.90  # playbook wins
    assert cfg.determinism == 0.3  # env fills what the playbook leaves out
    assert rc.defaults(None, settings=env).review_auto_threshold == 0.5


async def test_get_without_record_returns_defaults_unstored(r, keys, pb):
    cfg = await rc.get(r, keys, "run_x", playbook=pb)
    assert cfg.max_attempts == 3 and await r.get(keys.run_config("run_x")) is None


async def test_init_then_put_emits_diff_and_hash(r, keys, pb):
    start = await rc.init(r, keys, "run_1", playbook=pb, actor="orchestrator")
    assert (await rc.init(r, keys, "run_1", RunConfig(seed=1))).seed == start.seed  # init is idempotent
    new = await rc.put(r, keys, "run_1", {"max_attempts": 5, "dry_run": True}, actor="gui")
    assert new.max_attempts == 5 and new.dry_run and (await rc.get(r, keys, "run_1")) == new
    record = await rc.get_record(r, keys, "run_1")
    assert record["config_hash"] == new.config_hash() != start.config_hash()
    assert record["version"] == 2 and record["updated_by"] == "gui"
    evs = await events_of(r, keys, EventType.RUN_CONFIG_UPDATED)
    assert len(evs) == 2 and evs[0].payload["initial"] and all(e.run_id == "run_1" for e in evs)
    assert evs[1].payload["diff"] == {"max_attempts": [3, 5], "dry_run": [False, True]}
    assert evs[1].payload["config_hash"] == new.config_hash() and evs[1].actor == "gui"
    await rc.put(r, keys, "run_1", {"max_attempts": 5})  # no change -> no event
    assert len(await events_of(r, keys, EventType.RUN_CONFIG_UPDATED)) == 2


async def test_put_rejects_unknown_and_invalid(r, keys):
    with pytest.raises(rc.RunConfigError, match="max_atempts"):
        await rc.put(r, keys, "run_2", {"max_atempts": 2})
    with pytest.raises(rc.RunConfigError):
        await rc.put(r, keys, "run_2", {"determinism": 1.5})
    assert await r.get(keys.run_config("run_2")) is None


async def test_set_determinism_maps_temperatures(r, keys):
    cfg = await rc.set_determinism(r, keys, "run_3", 1.0, seed=7)
    assert cfg.determinism == 1.0 and cfg.seed == 7
    assert await r.get(keys.run_determinism("run_3")) == "1.0"
    upd = (await events_of(r, keys, EventType.CONFIG_UPDATED))[0].payload
    assert upd["temperatures"] == {"orchestrator": 0.0, "worker": 0.0, "meta_reviewer": 0.0, "verifier": 0.0}
    cfg = await rc.set_determinism(r, keys, "run_3", 0.5)
    assert cfg.seed == 7  # seed kept
    upd = (await events_of(r, keys, EventType.CONFIG_UPDATED))[1].payload
    assert upd["temperatures"] == {"orchestrator": 0.4, "worker": 0.5, "meta_reviewer": 0.25, "verifier": 0.0}
    diff = (await events_of(r, keys, EventType.RUN_CONFIG_UPDATED))[-1].payload["diff"]
    assert diff == {"determinism": [1.0, 0.5]}


async def test_reset_to_playbook_defaults(r, keys, pb):
    await rc.put(r, keys, "run_4", {"review_auto_threshold": 0.4, "spend_cap_usd": 9})
    cfg = await rc.reset_to_playbook_defaults(r, keys, "run_4", pb)
    assert cfg.review_auto_threshold == 0.80 and cfg.spend_cap_usd == 2.0
    last = (await events_of(r, keys, EventType.RUN_CONFIG_UPDATED))[-1].payload
    assert last["reason"] == "reset_to_playbook_defaults" and "review_auto_threshold" in last["diff"]


async def test_concurrent_puts_do_not_lose_updates(r, keys):
    import asyncio

    await asyncio.gather(rc.put(r, keys, "run_5", {"max_attempts": 4}), rc.put(r, keys, "run_5", {"seed": 9}),
                         rc.put(r, keys, "run_5", {"dry_run": True}))
    cfg = await rc.get(r, keys, "run_5")
    assert (cfg.max_attempts, cfg.seed, cfg.dry_run) == (4, 9, True)
