"""StubLLM, ScriptedBackend, env backend selection and the JSON helpers."""

from __future__ import annotations

import json

import pytest
from pydantic import BaseModel

from ledger_core import llm
from ledger_core.events import read_events
from ledger_core.llm import LLMBudgetExhausted, LLMError
from ledger_core.llm_json import extract_json, parse_as, strict_schema
from ledger_core.llm_scripted import ScriptedBackend, fixture_hint
from ledger_core.llm_testing import StubLLM
from ledger_core.prompts import assemble
from ledger_core.protocol import EventType
from ledger_core.settings import get_settings


class Plan(BaseModel):
    steps: list[str]


class Decision(BaseModel):
    decision: str
    confidence: float


class Loose(BaseModel):
    data: dict[str, object] = {}


SYS = [{"role": "system", "content": "sys"}, {"role": "user", "content": "go"}]


# ---- StubLLM ------------------------------------------------------------------


async def test_stub_matches_role_and_schema_and_records_calls():
    stub = StubLLM()
    stub.on("orchestrator", Plan, response={"steps": ["a"]})
    stub.on(None, Decision, fn=lambda call: {"decision": call.role, "confidence": 0.9})
    with stub.installed():
        plan = await llm.complete("orchestrator", SYS, Plan, run_id="run_1")
        dec = await llm.complete("meta_reviewer", SYS, Decision)
    assert plan.steps == ["a"] and dec.decision == "meta_reviewer"
    assert [c.role for c in stub.calls] == ["orchestrator", "meta_reviewer"]
    assert stub.calls[0].run_id == "run_1" and "go" in stub.calls[0].text
    assert llm.last_call().model == "stub"
    assert llm.get_backend() is None  # installed() restored the previous backend


async def test_stub_sequences_exceptions_and_specificity():
    stub = StubLLM()
    stub.on(None, Plan, response={"steps": ["generic"]})
    stub.on("worker", Plan, responses=[{"steps": ["first"]}, LLMBudgetExhausted("gone"), {"steps": ["last"]}])
    assert (await stub.complete("orchestrator", SYS, Plan)).steps == ["generic"]
    assert (await stub.complete("worker", SYS, Plan)).steps == ["first"]
    with pytest.raises(LLMBudgetExhausted):
        await stub.complete("worker", SYS, Plan)
    assert (await stub.complete("worker", SYS, Plan)).steps == ["last"]
    assert (await stub.complete("worker", SYS, Plan)).steps == ["last"]  # last one repeats
    with pytest.raises(LLMError, match="no response for role='worker' schema='Decision'"):
        await stub.complete("worker", SYS, Decision)
    assert len(stub.calls_for("worker", Plan)) == 4


# ---- ScriptedBackend ----------------------------------------------------------


def write(dir_, name, data):
    (dir_ / name).write_text(json.dumps(data))


@pytest.fixture
def fixtures(tmp_path):
    write(tmp_path, "plan.json", {"role": "orchestrator", "schema": "Plan", "response": {"steps": ["default"]}})
    write(tmp_path, "reviews.json", [
        {"role": "meta_reviewer", "schema": "Decision", "fixture_key": "lead:7",
         "responses": [{"decision": "match_existing", "confidence": 0.62}, {"decision": "create_new", "confidence": 0.9}]},
        {"role": "meta_reviewer", "schema": "Decision", "match": "Ben Ortiz",
         "response": {"decision": "match_existing", "confidence": 0.93}},
        {"role": "meta_reviewer", "schema": "Decision", "response": {"decision": "skip", "confidence": 0.85}},
        {"role": "worker", "schema": "Plan", "error": "scripted outage"},
    ])
    return tmp_path


async def test_scripted_lookup_order(fixtures, r, keys):
    sb = ScriptedBackend(fixtures, r=r, keys=keys)
    keyed = [{"role": "system", "content": "x\n\n[fixture_key: lead:7]"}, {"role": "user", "content": "Ben Ortiz"}]
    assert fixture_hint(keyed) == "lead:7"
    first = await sb.complete("meta_reviewer", keyed, Decision, run_id="run_s")
    second = await sb.complete("meta_reviewer", keyed, Decision)
    assert (first.decision, second.decision) == ("match_existing", "create_new")  # fixture_key wins, sequence
    by_match = await sb.complete("meta_reviewer", [{"role": "user", "content": "row: Ben Ortiz, Acme"}], Decision)
    assert by_match.confidence == 0.93
    default = await sb.complete("meta_reviewer", SYS, Decision)
    assert default.decision == "skip"
    assert (await sb.complete("orchestrator", SYS, Plan)).steps == ["default"]
    ev = [e for e in await read_events(r, keys) if e.type == EventType.LLM_CALL]
    assert len(ev) == 5 and ev[0].run_id == "run_s" and ev[0].payload["scripted"] is True
    assert ev[0].payload["model"] == "scripted:reviews.json"


async def test_scripted_misses_and_errors_are_explicit(fixtures):
    sb = ScriptedBackend(fixtures, emit_events=False)
    with pytest.raises(LLMError, match="scripted outage"):
        await sb.complete("worker", SYS, Plan)
    with pytest.raises(LLMError, match="no scripted fixture for role='verifier' schema='Plan'"):
        await sb.complete("verifier", SYS, Plan)


async def test_scripted_works_with_assembled_prompts(fixtures):
    sb = ScriptedBackend(fixtures, emit_events=False)
    prompt = assemble("meta_reviewer", {"kind": "review.ambiguity", "skill": "review", "postcondition": {"check": "review.decided"}},
                      {"lead": {"name": "X"}}, [], [], Decision, fixture_key="lead:7")
    assert (await sb.complete("meta_reviewer", prompt.messages, Decision)).confidence == 0.62


def test_scripted_rejects_malformed_fixture(tmp_path):
    write(tmp_path, "bad.json", {"role": "worker", "schema": "Plan"})
    with pytest.raises(LLMError, match="needs 'response'"):
        ScriptedBackend(tmp_path, emit_events=False)


def test_repo_fixture_dir_loads(repo):
    from pathlib import Path

    sb = ScriptedBackend(Path(repo, "tests/fixtures/llm"), emit_events=False)
    assert all({"role", "schema"} <= f.keys() for f in sb.fixtures)


# ---- env backend selection ----------------------------------------------------


@pytest.fixture
def fresh_backend(monkeypatch, ns):
    monkeypatch.setenv("LEDGER_NS", ns)
    llm.set_backend(None)
    get_settings.cache_clear()
    yield monkeypatch
    llm.set_backend(None)
    get_settings.cache_clear()


async def test_complete_lazily_installs_scripted_backend(fresh_backend, fixtures, r, keys):
    fresh_backend.setenv("LLM_BACKEND", "scripted")
    fresh_backend.setenv("LLM_FIXTURES_DIR", str(fixtures))
    get_settings.cache_clear()  # the r fixture already cached settings
    plan = await llm.complete("orchestrator", SYS, Plan, run_id="run_env")
    assert plan.steps == ["default"] and isinstance(llm.get_backend(), ScriptedBackend)
    assert any(e.run_id == "run_env" for e in await read_events(r, keys))  # namespaced by LEDGER_NS


async def test_env_backend_names(fresh_backend):
    from ledger_core.llm_openrouter import OpenRouterBackend

    assert isinstance(llm.backend_from_env("stub"), StubLLM)
    assert isinstance(llm.backend_from_env("openrouter"), OpenRouterBackend)
    fresh_backend.delenv("LLM_BACKEND", raising=False)
    assert isinstance(llm.backend_from_env(), OpenRouterBackend)  # default
    with pytest.raises(LLMError, match="unknown LLM_BACKEND"):
        llm.backend_from_env("bogus")
    fresh_backend.setenv("LLM_BACKEND", "stub")
    with pytest.raises(LLMError, match="StubLLM has no response"):
        await llm.complete("worker", SYS, Plan)


# ---- JSON helpers -------------------------------------------------------------


def test_extract_json_variants():
    assert extract_json('{"a": 1}') == {"a": 1}
    assert extract_json('Sure:\n```json\n{"a": 2}\n```\nanything else?') == {"a": 2}
    assert extract_json('<think>maybe {"a": 0}</think>The answer is {"a": {"b": "}"}} ok') == {"a": {"b": "}"}}
    for bad in ("", "no json here", "{broken"):
        with pytest.raises(ValueError):
            extract_json(bad)
    with pytest.raises(ValueError):
        parse_as(Decision, '{"decision": "x"}')  # missing confidence


def test_strict_schema_closes_objects_or_gives_up():
    strict = strict_schema(Decision.model_json_schema())
    assert strict["additionalProperties"] is False and set(strict["required"]) == {"decision", "confidence"}
    assert strict_schema(Loose.model_json_schema()) is None  # free-form dict can't be strict
