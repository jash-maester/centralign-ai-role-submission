"""OpenRouterBackend against a respx-mocked OpenRouter (never the real API)."""

from __future__ import annotations

import json

import httpx
import pytest
from pydantic import BaseModel

from ledger_core import llm, run_config
from ledger_core.config import RunConfig
from ledger_core.events import read_events
from ledger_core.llm import LLMBudgetExhausted, LLMCacheMiss, LLMError, LLMSpendCapReached
from ledger_core.llm_openrouter import OUTAGE_MODEL, OpenRouterBackend, utc_day
from ledger_core.protocol import EventType, FaultName
from ledger_core.settings import Settings

respx = pytest.importorskip("respx")  # absent in the browser image (make test-browser collects all tests)

BASE = "https://openrouter.test/api/v1"
M1, M2 = "acme/primary:free", "other/fallback:free"
MSGS = [{"role": "system", "content": "Be terse."}, {"role": "user", "content": "Say hi"}]


class Answer(BaseModel):
    word: str
    n: int


def ok(content: str, *, cost: float = 0.0, pt: int = 10, ct: int = 5) -> httpx.Response:
    return httpx.Response(200, json={
        "choices": [{"message": {"role": "assistant", "content": content}}],
        "usage": {"prompt_tokens": pt, "completion_tokens": ct, "cost": cost},
    })


def err(status: int, message: str = "boom") -> httpx.Response:
    return httpx.Response(status, json={"error": {"code": status, "message": message}})


def body(call) -> dict:
    return json.loads(call.request.content)


def models_called(route) -> list[str]:
    return [body(c)["model"] for c in route.calls]


async def types(r, keys) -> list[str]:
    return [e.type.value for e in await read_events(r, keys)]


@pytest.fixture
def settings() -> Settings:
    return Settings(
        openrouter_api_key="sk-test-not-real", openrouter_base_url=BASE,
        model_worker=f"{M1},{M2}", model_orchestrator=M1, model_verifier=M2, model_meta_reviewer="",
        llm_daily_request_budget=10, agent_id="test-llm",
    )


@pytest.fixture
def api():
    with respx.mock(base_url=BASE, assert_all_called=False) as mock:
        mock.get("/models").respond(json={"data": [
            {"id": M1, "supported_parameters": ["response_format", "structured_outputs", "seed", "temperature"]},
            {"id": M2, "supported_parameters": ["temperature"]},
        ]})
        yield mock


@pytest.fixture
async def make(settings, r, keys, tmp_path):
    made: list[OpenRouterBackend] = []

    def factory(**kw) -> OpenRouterBackend:
        OpenRouterBackend.reset_capabilities()
        args = dict(settings=settings, r=r, keys=keys, cache_dir=str(tmp_path / "cache"),
                    cache_mode="on", backoff_s=0, max_backoff_s=0)
        args.update(kw)
        b = OpenRouterBackend(**args)
        made.append(b)
        return b

    yield factory
    for b in made:
        await b.aclose()


async def test_success_with_strict_schema_seed_and_usage(api, make, r, keys):
    route = api.post("/chat/completions").mock(side_effect=[ok('{"word": "hi", "n": 1}', pt=12, ct=4)])
    backend = make()
    res = await backend.complete("worker", MSGS, Answer, run_id="run_t1", step_id="stp_t1",
                                 config=RunConfig(determinism=0.5))
    assert res == Answer(word="hi", n=1)
    sent = body(route.calls[0])
    assert sent["model"] == M1
    assert sent["temperature"] == 0.5 and sent["seed"] == 42
    assert sent["usage"] == {"include": True}
    rf = sent["response_format"]
    assert rf["type"] == "json_schema" and rf["json_schema"]["strict"] is True
    assert rf["json_schema"]["schema"]["additionalProperties"] is False
    assert route.calls[0].request.headers["authorization"].startswith("Bearer ")

    events = await read_events(r, keys)
    call = next(e for e in events if e.type == EventType.LLM_CALL)
    assert call.run_id == "run_t1" and call.step_id == "stp_t1" and call.actor == "test-llm"
    assert call.payload["model"] == M1 and call.payload["ok"] is True and call.payload["cached"] is False
    assert call.payload["prompt_tokens"] == 12 and call.payload["completion_tokens"] == 4
    spend = await r.hgetall(keys.llm_spend("run_t1"))
    assert spend["requests"] == "1" and spend["requests:worker"] == "1" and spend["tokens:worker"] == "16"
    assert await r.get(keys.llm_budget(utc_day())) == "1"
    assert llm.last_call().model == M1 and llm.last_call().cached is False


async def test_fallback_after_one_retry_on_5xx(api, make, r, keys):
    def reply(request):
        model = json.loads(request.content)["model"]
        return err(503, "upstream down") if model == M1 else ok('Here:\n```json\n{"word": "x", "n": 2}\n```')

    route = api.post("/chat/completions").mock(side_effect=reply)
    res = await make().complete("worker", MSGS, Answer)
    assert res.n == 2
    assert models_called(route) == [M1, M1, M2]  # retry once, then fall back
    fallback_body = body(route.calls[2])
    assert "response_format" not in fallback_body  # M2 lacks structured output
    assert "seed" not in fallback_body  # M2 lacks seed
    assert "JSON Schema" in fallback_body["messages"][-1]["content"]
    fb = [e for e in await read_events(r, keys) if e.type == EventType.MODEL_FALLBACK]
    assert len(fb) == 1 and fb[0].payload["from"] == M1 and fb[0].payload["to"] == M2
    assert "503" in fb[0].payload["reason"]
    assert llm.last_call().fallback_from == [M1]


async def test_429_is_retried_once_then_succeeds(api, make, r, keys):
    route = api.post("/chat/completions").mock(side_effect=[err(429, "rate limited"), ok('{"word": "y", "n": 3}')])
    res = await make().complete("worker", MSGS, Answer)
    assert res.word == "y"
    assert models_called(route) == [M1, M1]
    assert EventType.MODEL_FALLBACK.value not in await types(r, keys)
    calls = [e.payload for e in await read_events(r, keys) if e.type == EventType.LLM_CALL]
    assert [c["ok"] for c in calls] == [False, True] and calls[0]["status"] == 429
    assert await r.get(keys.llm_budget(utc_day())) == "2"  # every HTTP request is budgeted


async def test_invalid_json_reasks_once(api, make):
    route = api.post("/chat/completions").mock(side_effect=[ok("Sure! The word is hi."), ok('{"word": "ok", "n": 4}')])
    res = await make().complete("worker", MSGS, Answer)
    assert res == Answer(word="ok", n=4)
    assert models_called(route) == [M1, M1]
    reask = body(route.calls[1])["messages"]
    assert reask[-2] == {"role": "assistant", "content": "Sure! The word is hi."}
    assert "could not be used" in reask[-1]["content"]


async def test_invalid_twice_falls_back(api, make, r, keys):
    route = api.post("/chat/completions").mock(side_effect=[
        ok('{"word": 1}'), ok("still not json"), ok('{"word": "z", "n": 5}')])
    res = await make().complete("worker", MSGS, Answer)
    assert res.n == 5 and models_called(route) == [M1, M1, M2]
    fb = next(e for e in await read_events(r, keys) if e.type == EventType.MODEL_FALLBACK)
    assert "invalid output" in fb.payload["reason"]


async def test_cache_hit_makes_no_http_call(api, make, r, keys):
    route = api.post("/chat/completions").mock(side_effect=[ok('{"word": "c", "n": 6}')])
    first = await make().complete("worker", MSGS, Answer, run_id="run_c")
    second = await make().complete("worker", MSGS, Answer, run_id="run_c")  # fresh instance, same cache dir
    assert first == second and route.call_count == 1
    hits = [e for e in await read_events(r, keys) if e.type == EventType.LLM_CACHE_HIT]
    assert len(hits) == 1 and hits[0].payload["model"] == M1 and hits[0].payload["cached"] is True
    assert await r.get(keys.llm_budget(utc_day())) == "1"  # hits cost no budget
    assert llm.last_call().cached is True
    # A different temperature is a different cache key.
    route.side_effect = [ok('{"word": "d", "n": 7}')]
    await make().complete("worker", MSGS, Answer, config=RunConfig(determinism=0.1))
    assert route.call_count == 2


async def test_cache_off_forces_live_call_and_replay_only_reads(api, make):
    route = api.post("/chat/completions").mock(side_effect=[ok('{"word": "a", "n": 1}'), ok('{"word": "b", "n": 2}')])
    await make().complete("worker", MSGS, Answer)
    forced = await make(cache_mode="off").complete("worker", MSGS, Answer)
    assert forced.word == "b" and route.call_count == 2
    replayed = await make(cache_mode="replay-only").complete("worker", MSGS, Answer)
    assert replayed.word == "b" and route.call_count == 2  # off still refreshed the cache


async def test_replay_only_miss_raises_without_http(api, make):
    route = api.post("/chat/completions").mock(side_effect=[ok('{"word": "n", "n": 0}')])
    with pytest.raises(LLMCacheMiss):
        await make(cache_mode="replay-only").complete("worker", MSGS, Answer)
    assert route.call_count == 0


async def test_budget_exhausted_refuses_before_http(api, make, r, keys):
    route = api.post("/chat/completions").mock(side_effect=[ok('{"word": "b", "n": 1}')])
    await r.set(keys.llm_budget(utc_day()), 10)
    with pytest.raises(LLMBudgetExhausted):
        await make().complete("worker", MSGS, Answer, run_id="run_b")
    assert route.call_count == 0
    assert await r.get(keys.llm_budget(utc_day())) == "10"
    ev = next(e for e in await read_events(r, keys) if e.type == EventType.LLM_BUDGET_EXHAUSTED)
    assert ev.payload["budget"] == 10 and ev.run_id == "run_b"
    assert EventType.MODEL_FALLBACK.value not in await types(r, keys)  # no fallback on budget


async def test_spend_cap_stops_new_calls(api, make, r, keys):
    route = api.post("/chat/completions").mock(side_effect=lambda req: ok('{"word": "s", "n": 1}', cost=1.5))
    backend, cfg = make(), RunConfig(spend_cap_usd=2.0)
    msgs = lambda i: [{"role": "user", "content": f"call {i}"}]  # distinct: no cache hits
    await backend.complete("worker", msgs(1), Answer, run_id="run_s", config=cfg)
    await backend.complete("worker", msgs(2), Answer, run_id="run_s", config=cfg)  # 1.5 < 2.0: allowed
    for i in (3, 4):
        with pytest.raises(LLMSpendCapReached):
            await backend.complete("worker", msgs(i), Answer, run_id="run_s", config=cfg)
    assert route.call_count == 2
    assert float(await r.hget(keys.llm_spend("run_s"), "usd")) == pytest.approx(3.0)
    caps = [e for e in await read_events(r, keys) if e.type == EventType.SPEND_CAP_REACHED]
    assert len(caps) == 1 and caps[0].payload["cap_usd"] == 2.0  # emitted once per run


async def test_model_outage_fault_forces_fallback(api, make, r, keys):
    def reply(request):
        model = json.loads(request.content)["model"]
        return err(400, f"{model} is not a valid model ID") if model == OUTAGE_MODEL else ok('{"word": "f", "n": 9}')

    route = api.post("/chat/completions").mock(side_effect=reply)
    await r.hset(keys.faults, FaultName.MODEL_OUTAGE.value, "1")
    backend = make()
    res = await backend.complete("worker", MSGS, Answer)
    assert res.n == 9
    assert models_called(route) == [OUTAGE_MODEL, M2]  # 400 is not retried
    fb = next(e for e in await read_events(r, keys) if e.type == EventType.MODEL_FALLBACK)
    assert fb.payload["from"] == OUTAGE_MODEL and "model_outage" in fb.payload["reason"]
    assert await r.hget(keys.faults, FaultName.MODEL_OUTAGE.value) is None  # one shot consumed
    await backend.complete("worker", [{"role": "user", "content": "again"}], Answer)
    assert models_called(route)[-1] == M1


async def test_model_outage_only_hits_worker_role(api, make, r, keys):
    route = api.post("/chat/completions").mock(side_effect=[ok('{"word": "o", "n": 1}')])
    await r.hset(keys.faults, FaultName.MODEL_OUTAGE.value, "on")
    await make().complete("orchestrator", MSGS, Answer)
    assert models_called(route) == [M1]
    assert await r.hget(keys.faults, FaultName.MODEL_OUTAGE.value) == "on"


async def test_fallback_disabled_raises(api, make, r, keys):
    route = api.post("/chat/completions").mock(side_effect=lambda req: err(500))
    with pytest.raises(LLMError) as exc:
        await make().complete("worker", MSGS, Answer, config=RunConfig(model_fallback=False))
    assert not isinstance(exc.value, (LLMBudgetExhausted, LLMSpendCapReached))
    assert models_called(route) == [M1, M1]
    assert EventType.MODEL_FALLBACK.value not in await types(r, keys)


async def test_stored_run_config_drives_temperature(api, make, r, keys):
    route = api.post("/chat/completions").mock(side_effect=[ok('{"word": "t", "n": 1}')])
    await run_config.put(r, keys, "run_cfg", {"determinism": 0.25, "seed": 7})
    await make().complete("worker", MSGS, Answer, run_id="run_cfg")
    sent = body(route.calls[0])
    assert sent["temperature"] == 0.75 and sent["seed"] == 7


async def test_verifier_temperature_is_zero_and_unpinned_seed_omitted(api, make):
    route = api.post("/chat/completions").mock(side_effect=[ok('{"word": "v", "n": 1}')])
    await make().complete("verifier", MSGS, Answer, config=RunConfig(determinism=0.0, seed_pinned=False))
    sent = body(route.calls[0])
    assert sent["temperature"] == 0 and "seed" not in sent


async def test_missing_key_or_models_raise(api, make, settings):
    with pytest.raises(LLMError, match="no models configured"):
        await make().complete("meta_reviewer", MSGS, Answer)
    keyless = settings.model_copy(update={"openrouter_api_key": ""})
    with pytest.raises(LLMError, match="OPENROUTER_API_KEY"):
        await make(settings=keyless).complete("worker", MSGS, Answer, config=RunConfig(model_fallback=False))
