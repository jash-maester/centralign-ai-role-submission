"""W0: the shared contracts every build track relies on."""

import csv
from pathlib import Path

import yaml

from ledger_core import protocol as p
from ledger_core.config import RunConfig, defaults_from
from ledger_core.keys import Keys
from ledger_core.postconditions import CHECK_NAMES


def test_every_status_has_transition_entry():
    assert set(p.LEGAL_TRANSITIONS) == set(p.StepStatus)


def test_terminal_statuses_have_no_exits():
    for s in p.TERMINAL_STATUSES:
        assert p.LEGAL_TRANSITIONS[s] == frozenset()


def test_any_non_terminal_can_die():
    for s in set(p.StepStatus) - p.TERMINAL_STATUSES:
        assert p.is_legal_transition(s, p.StepStatus.DEAD)


def test_happy_path_is_legal():
    path = ["planned", "ready", "leased", "claimed_done", "verified", "committed"]
    for a, b in zip(path, path[1:], strict=False):
        assert p.is_legal_transition(p.StepStatus(a), p.StepStatus(b))


def test_workers_cannot_skip_verification():
    assert not p.is_legal_transition(p.StepStatus.LEASED, p.StepStatus.COMMITTED)
    assert not p.is_legal_transition(p.StepStatus.CLAIMED_DONE, p.StepStatus.COMMITTED)


def test_default_checks_exist_in_registry_names():
    assert set(p.DEFAULT_CHECK.values()) <= set(CHECK_NAMES)


def test_every_routable_kind_has_a_skill():
    routable = {k for kinds in p.SKILL_KINDS.values() for k in kinds}
    assert routable | {p.StepKind.RUN_VERIFY} == set(p.StepKind)


def test_temperature_mapping_matches_design():
    cfg = RunConfig(determinism=0.8)
    assert cfg.temperature("orchestrator") == 0.16
    assert cfg.temperature("worker") == 0.2
    assert cfg.temperature("meta_reviewer") == 0.1
    assert cfg.temperature("verifier") == 0
    assert RunConfig(determinism=1.0).temperature("worker") == 0


def test_config_hash_is_stable_and_sensitive():
    assert RunConfig().config_hash() == RunConfig().config_hash()
    assert RunConfig().config_hash() != RunConfig(seed=7).config_hash()


def test_playbook_front_matter_feeds_run_config(repo):
    text = Path(repo, "playbooks/event-leads.md").read_text()
    front = yaml.safe_load(text.split("---")[1])
    cfg = defaults_from(front)
    assert cfg.review_auto_threshold == 0.80 and cfg.approval_auto_threshold == 0.90
    headings = [line[3:] for line in text.splitlines() if line.startswith("## ")]
    assert headings == [
        "Dedupe rules", "Owner routing", "Follow-up policy",
        "Approval policy", "Escalation rules", "Definitions of done",
    ]


def test_demo_csv_has_twelve_messy_rows(repo):
    with open(Path(repo, "data/event_attendees.csv"), newline="") as f:
        rows = list(csv.DictReader(f))
    assert len(rows) == 12
    assert " E-mail Address " in rows[0]  # messy header kept on purpose


def test_keys_namespace():
    assert Keys().step("s1") == "step:s1"
    assert Keys("t1").step("s1") == "t1:step:s1"
    assert Keys("t1").queue("browser.espocrm") == "t1:queue:browser.espocrm"


async def test_redis_is_real_and_namespaced(r, keys):
    assert await r.ping()
    await r.set(keys.step("x"), "1")
    assert await r.get(keys.step("x")) == "1"


async def test_events_append_and_read(r, keys):
    from ledger_core.events import append_event, read_events
    from ledger_core.protocol import Event, EventType

    sid = await append_event(r, keys, Event(actor="test", type=EventType.RUN_CREATED, run_id="run_1"))
    await append_event(r, keys, Event(actor="test", type=EventType.RUN_CREATED, run_id="run_2"))
    got = await read_events(r, keys, run_id="run_1")
    assert [e.id for e in got] == [sid] and got[0].type == EventType.RUN_CREATED
    assert len(await read_events(r, keys, after=sid)) == 1


def test_check_loader_imports_check_modules():
    from ledger_core.postconditions import load_all

    assert isinstance(load_all(), dict)


def test_api_health():
    from fastapi.testclient import TestClient

    from app.main import app

    with TestClient(app) as c:
        assert c.get("/health").json() == {"status": "ok"}
