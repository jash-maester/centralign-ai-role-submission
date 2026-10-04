"""GET /llm/budget: free-model requests used / remaining today (plans/01 §6b, F8).

`used` is the ledger's own counter (Keys.llm_budget(UTC day)), the number the
LLM layer enforces. `live` is OpenRouter's view of the key
(GET /api/v1/key, not a model call, costs no request), cached 60 s per
process; it is null when no key is configured or the check fails.
"""

from __future__ import annotations

import time
from typing import Any

import httpx
from fastapi import APIRouter, Depends

from ledger_core.keys import Keys
from ledger_core.llm_openrouter import utc_day
from ledger_core.settings import get_settings

from ..deps import get_keys, get_r

router = APIRouter(tags=["llm"])

ROLES = ("orchestrator", "worker", "verifier", "meta_reviewer")
LIVE_TTL_S = 60.0
_live_cache: dict[str, Any] = {"at": 0.0, "value": None}


async def fetch_key_info() -> dict[str, Any] | None:
    s = get_settings()
    if not s.openrouter_api_key:
        return None
    try:
        async with httpx.AsyncClient(timeout=5) as client:
            resp = await client.get(f"{s.openrouter_base_url}/key",
                                    headers={"Authorization": f"Bearer {s.openrouter_api_key}"})
        if resp.status_code != 200:
            return {"ok": False, "status": resp.status_code}
        data = resp.json().get("data", {})
    except (httpx.HTTPError, ValueError) as exc:
        return {"ok": False, "error": type(exc).__name__}
    free = data.get("free_model_daily_requests") or {}
    return {"ok": True, "remaining": free.get("remaining"), "limit": free.get("limit"),
            "usage_usd": data.get("usage"), "is_free_tier": data.get("is_free_tier")}


async def live_key_info() -> dict[str, Any] | None:
    now = time.monotonic()
    if now - _live_cache["at"] < LIVE_TTL_S:
        return _live_cache["value"]
    value = await fetch_key_info()
    _live_cache.update(at=now, value=value)
    return value


@router.get("/llm/budget")
async def budget(run_id: str | None = None, live: bool = True, r=Depends(get_r),
                 keys: Keys = Depends(get_keys)) -> dict[str, Any]:
    s = get_settings()
    day = utc_day()
    used = int(await r.get(keys.llm_budget(day)) or 0)
    limit = s.llm_daily_request_budget
    out: dict[str, Any] = {
        "date": day, "used": used, "limit": limit, "remaining": max(0, limit - used),
        "models": {role: s.models_for(role) for role in ROLES},
        "cache": s.llm_cache, "backend": s.llm_backend,
        "live": await live_key_info() if live else None,
    }
    if run_id:
        spend = await r.hgetall(keys.llm_spend(run_id))
        out["run"] = {k: (float(v) if k.startswith("usd") else int(v) if v.lstrip("-").isdigit() else v)
                      for k, v in spend.items()}
        out["spent_usd"] = float(spend.get("usd", 0) or 0)
    return out
