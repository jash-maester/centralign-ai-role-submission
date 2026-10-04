"""Playbook loading and sectioning on the real playbook (B6, E5)."""

from __future__ import annotations

import hashlib
import shutil
from pathlib import Path

import pytest

from ledger_core.playbook import SECTIONS_BY_KIND, PlaybookError, append_rule, load, parse
from ledger_core.protocol import StepKind

TITLES = ["Dedupe rules", "Owner routing", "Follow-up policy", "Approval policy", "Escalation rules", "Definitions of done"]


@pytest.fixture
def pb_dir(repo) -> Path:
    return Path(repo, "playbooks")


@pytest.fixture
def writable(pb_dir, tmp_path) -> Path:
    shutil.copy(pb_dir / "event-leads.md", tmp_path / "event-leads.md")
    return tmp_path


def test_real_playbook_sections_version_hash(pb_dir):
    pb = load("event-leads.md", pb_dir)
    assert pb.titles == TITLES
    assert pb.version == 3 and pb.front_matter["playbook"] == "event-leads"
    assert pb.content_hash == hashlib.sha256((pb_dir / "event-leads.md").read_bytes()).hexdigest()
    assert pb.preamble.startswith("# Event leads SOP")
    assert "Normalise before comparing" in pb.section("dedupe RULES").body
    assert not any("## " in s.body for s in pb.sections)
    assert load("event-leads", pb_dir).content_hash == pb.content_hash  # .md optional


def test_every_step_kind_maps_to_existing_sections(pb_dir):
    pb = load(playbook_dir=pb_dir)
    assert set(SECTIONS_BY_KIND) == set(StepKind)
    for kind, titles in SECTIONS_BY_KIND.items():
        assert [s.title for s in pb.sections_for(kind)] == list(titles), kind


def test_sections_for_selects_only_relevant(pb_dir):
    pb = load(playbook_dir=pb_dir)
    assert [s.title for s in pb.sections_for("email.draft")] == ["Follow-up policy"]
    assert [s.title for s in pb.sections_for(StepKind.CRM_CREATE_CONTACT)] == ["Dedupe rules", "Owner routing"]
    assert len(pb.sections_for(None)) == 6 and len(pb.sections_for("all")) == 6
    with pytest.raises(PlaybookError):
        pb.sections_for("crm.delete_everything")


def test_run_defaults_from_front_matter(pb_dir):
    cfg = load(playbook_dir=pb_dir).run_defaults()
    assert cfg.review_auto_threshold == 0.80 and cfg.approval_auto_threshold == 0.90
    assert cfg.lease_ttl_s == 15 and cfg.spend_cap_usd == 2.0 and cfg.replan_after_rejections == 2


def test_append_rule_to_existing_section(writable):
    before = load(playbook_dir=writable)
    after = append_rule("Escalation rules", "Acme Corp and Acme Ltd\n are the same account.",
                        playbook_dir=writable, source="esc_123")
    assert after.version == 4 and after.content_hash != before.content_hash
    body = after.section("Escalation rules").body.rstrip().splitlines()
    assert body[-1] == "- Acme Corp and Acme Ltd are the same account. _(saved rule: esc_123)_"
    assert after.titles == TITLES  # inserted before "Definitions of done", nothing else moved
    for title in TITLES:
        if title != "Escalation rules":
            assert after.section(title) == before.section(title)
    assert load(playbook_dir=writable).content_hash == after.content_hash  # persisted


def test_append_rule_creates_missing_section(writable):
    after = append_rule("Saved rules", "Skip badge scans without a company.", playbook_dir=writable)
    assert after.titles[-1] == "Saved rules"
    assert after.section("Saved rules").body.strip() == "- Skip badge scans without a company."
    with pytest.raises(PlaybookError):
        append_rule("Saved rules", "   ", playbook_dir=writable)


def test_rejects_paths_outside_playbook_dir(writable):
    with pytest.raises(PlaybookError):
        load("../etc/passwd", writable)


def test_parse_without_front_matter():
    pb = parse("# T\n\nintro\n\n## A\none\n\n## B\ntwo\n", "x.md")
    assert pb.front_matter == {} and pb.titles == ["A", "B"] and pb.version is None
