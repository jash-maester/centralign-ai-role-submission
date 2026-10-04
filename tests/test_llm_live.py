"""Live OpenRouter smoke test: one real call with a tiny schema.

Runs only with `make test-live` (LLM_LIVE_TESTS=1); uses 1-2 of the shared
50/day free requests (2 only if the model's reply needs a re-ask). Cache is
off for reads so the call is really live; the budget counter lives in the
test namespace.
"""

from __future__ import annotations

import pytest
from pydantic import BaseModel

from ledger_core.config import RunConfig
from ledger_core.events import read_events
from ledger_core.llm_openrouter import OpenRouterBackend
from ledger_core.protocol import EventType
from ledger_core.settings import Settings

MODEL = "nvidia/nemotron-3-super-120b-a12b:free"


class Capital(BaseModel):
    country: str
    capital: str


@pytest.mark.live_llm
async def test_live_structured_call(r, keys, tmp_path):
    base = Settings()
    assert base.openrouter_api_key, "OPENROUTER_API_KEY missing"
    settings = base.model_copy(update={"model_orchestrator": MODEL, "agent_id": "live-smoke"})
    backend = OpenRouterBackend(settings=settings, r=r, keys=keys, cache_dir=str(tmp_path), cache_mode="off",
                                daily_budget=2)
    try:
        out = await backend.complete(
            "orchestrator",
            [{"role": "user", "content": "What is the capital of France? Answer in JSON."}],
            Capital, run_id="run_live", config=RunConfig(determinism=1.0, model_fallback=False),
        )
    finally:
        await backend.aclose()
    assert "paris" in out.capital.lower()
    calls = [e.payload for e in await read_events(r, keys) if e.type == EventType.LLM_CALL]
    assert calls and calls[-1]["ok"] and calls[-1]["model"] == MODEL
    print(f"live call ok: {out!r}; requests={len(calls)}; tokens={calls[-1]['total_tokens']}; "
          f"latency_ms={calls[-1]['latency_ms']}")
