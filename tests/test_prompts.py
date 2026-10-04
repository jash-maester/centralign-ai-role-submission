"""Prompt assembly (plans/01 §7): five layers, rejection reasons in, ids out (D3)."""

from __future__ import annotations

import re
from pathlib import Path

from pydantic import BaseModel

from ledger_core.playbook import load
from ledger_core.prompts import LAYERS, assemble, resolve_inputs
from ledger_core.protocol import Attempt, Claim, Fact, Postcondition, Skill, Step, StepKind, Verdict


class Draft(BaseModel):
    subject: str
    body: str


REASON = "placeholder text '[Name]' found in body"


def make_step(run_id: str = "run_0123456789", step_id: str = "stp_abcdef0123") -> Step:
    return Step(
        id=step_id, run_id=run_id, kind=StepKind.EMAIL_DRAFT, skill=Skill.EMAIL_DRAFT,
        title="Draft follow-up for Ben Ortiz", inputs={"lead": "fact:lead:7", "event": "fact:event"},
        postcondition=Postcondition(check="email.draft_valid", args={"lead": "lead:7"}, expect={"max_words": 120}),
        lane="lead:7", side_effect=False,
    )


def attempts(step_id: str = "stp_abcdef0123", ts: int = 1759000000000) -> list[Attempt]:
    return [Attempt(
        attempt=1, worker="worker-drafter", fence=3, model="qwen/qwen3.8-27b:free",
        observations=[{"ts": ts, "note": f"drafted for {step_id}"}],
        claim=Claim(worker="worker-drafter", fence=3, summary="drafted email", data={"subject": "Hi [Name]"}, ts=ts),
        verdict=Verdict(ok=False, check="email.draft_valid", reason=REASON, observed={"placeholders": ["[Name]"]}, ts=ts),
        outcome="rejected", started_at=ts, ended_at=ts + 5,
    )]


def facts(step_id: str = "stp_abcdef0123") -> dict[str, Fact]:
    return {
        "lead:7": Fact(key="lead:7", value={"first_name": "Ben", "email": "ben@acme.test", "company": "Acme"},
                       source_step=step_id, committed_at=1759000000001),
        "event": Fact(key="event", value="DevSummit 2026", source_step=step_id),
    }


def build(repo, step=None, prior=None, fx=None, **kw):
    step = step or make_step()
    pb = load(playbook_dir=Path(repo, "playbooks"))
    return assemble("worker", step, resolve_inputs(step.inputs, fx or facts()), attempts() if prior is None else prior,
                    pb.sections_for(step.kind), Draft, **kw)


def test_five_layers_with_token_counts(repo):
    p = build(repo)
    assert [layer.id for layer in p.layers] == list(LAYERS)
    assert all(t > 0 for t in p.tokens.values()) and p.total_tokens == sum(p.tokens.values())
    system, user = p.messages[0]["content"], p.messages[1]["content"]
    assert p.messages[0]["role"] == "system" and p.messages[1]["role"] == "user"
    assert "Ledger worker" in system and "Skill email.draft" in system
    assert "## Follow-up policy" in system and "## Dedupe rules" not in system  # B6
    assert "Step kind: email.draft" in user and "ben@acme.test" in user and "DevSummit 2026" in user
    assert "email.draft_valid" in user and "max_words" in user
    assert "JSON Schema" in user and '"properties"' in user  # schema layer
    assert p.layer("history").locked and not p.layer("playbook").locked


def test_rejection_reason_feeds_next_prompt(repo):
    user = build(repo).messages[1]["content"]
    assert REASON in user and "REJECTED" in user and "Hi [Name]" in user
    first = build(repo, prior=[]).messages[1]["content"]
    assert "first attempt" in first


def test_no_ids_or_timestamps_and_stable_across_runs(repo):
    p = build(repo)
    text = "\n".join(m["content"] for m in p.messages)
    for needle in ("run_0123456789", "stp_abcdef0123", "worker-drafter", "1759000000"):
        assert needle not in text, needle
    assert not re.search(r"\b1\d{12}\b", text)  # no epoch-ms timestamps
    other = build(repo, step=make_step("run_ffffffffff", "stp_9999999999"),
                  prior=attempts("stp_9999999999", ts=1760000000000), fx=facts("stp_9999999999"))
    assert other.messages == p.messages  # same inputs -> same prompt -> same cache key


def test_layers_2_and_5_toggle_but_history_is_locked(repo):
    p = build(repo, layers={"playbook": False, "schema": False, "history": False, "role": False})
    system, user = p.messages[0]["content"], p.messages[1]["content"]
    assert "Follow-up policy" not in system and "JSON Schema" not in user
    assert REASON in user and "Ledger worker" in system  # history and role stay on
    assert p.tokens["playbook"] == 0 and p.tokens["schema"] == 0 and p.tokens["history"] > 0


def test_custom_instructions_and_fixture_key(repo):
    p = build(repo, instructions="You write short emails.", fixture_key="lead:7")
    assert p.messages[0]["content"].startswith("You write short emails.")
    assert "[fixture_key: lead:7]" in p.messages[0]["content"]
    assert p.layer("role").source == "agent prompt"


def test_resolve_inputs_marks_missing_facts():
    out = resolve_inputs({"a": "fact:x", "b": ["fact:y", 2], "c": "literal"}, {"x": 1})
    assert out == {"a": 1, "b": ["<missing fact: y>", 2], "c": "literal"}
