"""GET /llm/budget: LLM requests used / remaining today (plans/01 §6b, F8).

`used` / `limit` / `remaining` come from ledger_core.llm_budget, the same
numbers the OpenRouter backend enforces before every live call:
OpenRouter's own count for the key (GET /api/v1/key, not a model call,
cached 60 s) when OPENROUTER_API_KEY is set, else the persistent local
counter under LLM_CACHE_DIR. `source` says which. `usable` is what is left
after the reserve that is never spent. `ledger_used` is this stack's own
Redis counter (informational; lost on `docker compose down -v`). The key
itself is never returned.
"""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Depends

from ledger_core import llm_budget
from ledger_core.keys import Keys
from ledger_core.settings import get_settings

from ..deps import get_keys, get_r

router = APIRouter(tags=["llm"])

ROLES = ("orchestrator", "worker", "verifier", "meta_reviewer")


def get_budget() -> llm_budget.Budget:
    return llm_budget.get_budget()


@router.get("/llm/budget")
async def budget(run_id: str | None = None, live: bool = True, r=Depends(get_r),
                 keys: Keys = Depends(get_keys)) -> dict[str, Any]:
    s = get_settings()
    snap = await get_budget().snapshot(live=live)
    info = snap.get("openrouter")
    out: dict[str, Any] = {
        "date": snap["date"], "source": snap["source"], "used": snap["used"], "limit": snap["limit"],
        "remaining": snap["remaining"], "reserve": snap["reserve"], "usable": snap["usable"],
        "credit_remaining": snap.get("credit_remaining"), "local": snap["local"],
        "ledger_used": int(await r.get(keys.llm_budget(snap["date"])) or 0),
        "models": {role: s.models_for(role) for role in ROLES},
        "cache": s.llm_cache, "backend": s.llm_backend,
        # OpenRouter's raw view (no key, no label), null without a key or with live=false
        "live": ({**info, "remaining": info.get("free_remaining"), "limit": info.get("free_limit")}
                 if isinstance(info, dict) and info.get("ok") else info),
    }
    if run_id:
        spend = await r.hgetall(keys.llm_spend(run_id))
        out["run"] = {k: (float(v) if k.startswith("usd") else int(v) if v.lstrip("-").isdigit() else v)
                      for k, v in spend.items()}
        out["spent_usd"] = float(spend.get("usd", 0) or 0)
    return out
