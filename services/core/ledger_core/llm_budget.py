"""Daily LLM request budget: OpenRouter's own count first (plans/01 §6b, F8).

    budget = get_budget()                      # one per process (settings from env)
    snap = await budget.snapshot()             # {date, source, used, limit, remaining, reserve, usable, ...}
    await budget.check(model)                  # raises BudgetRefused before a live request
    budget.record()                            # after deciding to send: +1 on the local counter

Source of truth, in order:

1. **OpenRouter** (`GET {OPENROUTER_BASE_URL}/key`, not a model call, costs no
   request) when OPENROUTER_API_KEY is set: `data.free_model_daily_requests`
   {used, limit, remaining} for `:free` models, `data.limit_remaining` (paid
   credit, USD) for the rest. Cached `ttl_s` (60 s) per process; requests this
   stack made since the fetch (local counter delta) are added on top, so a
   burst inside the TTL is still counted. A reserve (`LLM_BUDGET_RESERVE`,
   default 3) is never spent: a live call is refused once `remaining <= reserve`.
2. **Local counter** when there is no key or the check fails: a file per UTC
   day under `LLM_CACHE_DIR/budget/` (bind mount ./.cache/llm, survives
   `make clean` / `down -v`, unlike Redis), enforced against
   `LLM_DAILY_REQUEST_BUDGET` (which already keeps headroom below 50).

The local counter is incremented for every live request either way, so it is
also the fallback's history. The API key is only ever put in the request
header: never logged, never returned (errors carry the exception type only).
"""

from __future__ import annotations

import contextlib
import datetime as dt
import json
import os
import tempfile
import time
from pathlib import Path
from typing import Any

import httpx

from .settings import Settings, get_settings

DEFAULT_TTL_S = 60.0


class BudgetRefused(Exception):
    """No live request may be made (budget used down to the reserve)."""

    def __init__(self, message: str, snapshot: dict[str, Any]) -> None:
        super().__init__(message)
        self.snapshot = snapshot


def utc_day(now: float | None = None) -> str:
    return dt.datetime.fromtimestamp(now or time.time(), dt.UTC).strftime("%Y-%m-%d")


def is_free_model(model: str) -> bool:
    return model.endswith(":free")


# ---------------------------------------------------------------------------
# OpenRouter key info
# ---------------------------------------------------------------------------


def _num(v: Any) -> float | None:
    try:
        return None if v is None else float(v)
    except (TypeError, ValueError):
        return None


def _int(v: Any) -> int | None:
    n = _num(v)
    return None if n is None else int(n)


def parse_key_info(data: dict[str, Any]) -> dict[str, Any]:
    """The fields we use from GET /key `data` (never the key or its label)."""
    free = data.get("free_model_daily_requests") or {}
    return {
        "ok": True,
        "free_used": _int(free.get("used")), "free_limit": _int(free.get("limit")),
        "free_remaining": _int(free.get("remaining")),
        "credit_remaining": _num(data.get("limit_remaining")), "credit_limit": _num(data.get("limit")),
        "usage_usd": _num(data.get("usage")), "usage_daily_usd": _num(data.get("usage_daily")),
        "is_free_tier": data.get("is_free_tier"),
    }


async def fetch_key_info(settings: Settings, http: httpx.AsyncClient | None = None) -> dict[str, Any] | None:
    """OpenRouter's view of the key, or None without a key. Failures come back
    as {"ok": False, "status" | "error"} (type name only, never the key)."""
    if not settings.openrouter_api_key:
        return None
    url = f"{settings.openrouter_base_url.rstrip('/')}/key"
    headers = {"Authorization": f"Bearer {settings.openrouter_api_key}"}
    try:
        if http is not None:
            resp = await http.get(url, headers=headers)
        else:
            async with httpx.AsyncClient(timeout=5) as client:
                resp = await client.get(url, headers=headers)
        if resp.status_code != 200:
            return {"ok": False, "status": resp.status_code}
        data = resp.json().get("data") or {}
    except (httpx.HTTPError, ValueError, AttributeError) as exc:
        return {"ok": False, "error": type(exc).__name__}
    return parse_key_info(data)


# ---------------------------------------------------------------------------
# persistent local counter
# ---------------------------------------------------------------------------


class LocalCounter:
    """Requests per UTC day in `<dir>/budget/<YYYY-MM-DD>.json`, shared by every
    process that mounts the same cache dir. A write problem never fails a call."""

    def __init__(self, cache_dir: str | os.PathLike[str]) -> None:
        self.dir = Path(cache_dir) / "budget"

    def path(self, day: str) -> Path:
        return self.dir / f"{day}.json"

    def read(self, day: str | None = None) -> int:
        try:
            return int(json.loads(self.path(day or utc_day()).read_text()).get("used", 0))
        except (OSError, ValueError, AttributeError, TypeError):
            return 0

    def incr(self, day: str | None = None, n: int = 1) -> int:
        day = day or utc_day()
        try:
            self.dir.mkdir(parents=True, exist_ok=True)
            with open(self.dir / ".lock", "a+") as lock:
                with contextlib.suppress(OSError, ImportError):
                    import fcntl

                    fcntl.flock(lock, fcntl.LOCK_EX)
                used = self.read(day) + n
                fd, tmp = tempfile.mkstemp(dir=self.dir, suffix=".tmp")
                with os.fdopen(fd, "w") as f:
                    json.dump({"date": day, "used": used, "updated_at": int(time.time())}, f)
                os.replace(tmp, self.path(day))
                return used
        except OSError:
            return self.read(day)


# ---------------------------------------------------------------------------
# the budget
# ---------------------------------------------------------------------------


class Budget:
    def __init__(
        self, settings: Settings | None = None, *, cache_dir: str | None = None, http: httpx.AsyncClient | None = None,
        ttl_s: float = DEFAULT_TTL_S, reserve: int | None = None, daily_budget: int | None = None,
        clock=time.monotonic,
    ) -> None:
        self.settings = settings or get_settings()
        self.counter = LocalCounter(cache_dir or self.settings.llm_cache_dir)
        self.http = http
        self.ttl_s = ttl_s
        self.reserve = self.settings.llm_budget_reserve if reserve is None else reserve
        self.daily_budget = self.settings.llm_daily_request_budget if daily_budget is None else daily_budget
        self._clock = clock
        self._info: dict[str, Any] | None = None  # last fetch + {"at", "day", "local_at_fetch"}

    def forget(self) -> None:
        """Drop the cached OpenRouter view (next snapshot fetches again)."""
        self._info = None

    async def key_info(self, *, refresh: bool = False) -> dict[str, Any] | None:
        now = self._clock()
        cached = self._info
        if not refresh and cached is not None and now - cached["at"] < self.ttl_s and cached["day"] == utc_day():
            return cached["info"]
        info = await fetch_key_info(self.settings, self.http)
        self._info = {"at": now, "day": utc_day(), "info": info, "local_at_fetch": self.counter.read()}
        return info

    async def snapshot(self, *, live: bool = True, refresh: bool = False) -> dict[str, Any]:
        day = utc_day()
        local_used = self.counter.read(day)
        info = await self.key_info(refresh=refresh) if live else None
        out: dict[str, Any] = {
            "date": day, "reserve": self.reserve,
            "local": {"used": local_used, "limit": self.daily_budget,
                      "remaining": max(0, self.daily_budget - local_used)},
            "openrouter": info,
        }
        if info and info.get("ok") and info.get("free_remaining") is not None:
            since = max(0, local_used - int((self._info or {}).get("local_at_fetch") or 0))
            limit = info.get("free_limit") or 0
            remaining = max(0, int(info["free_remaining"]) - since)
            out.update(source="openrouter", used=(info.get("free_used") or max(0, limit - info["free_remaining"]))
                       + since, limit=limit, remaining=remaining, since_fetch=since,
                       usable=max(0, remaining - self.reserve), credit_remaining=info.get("credit_remaining"))
        else:
            remaining = max(0, self.daily_budget - local_used)
            out.update(source="local", used=local_used, limit=self.daily_budget, remaining=remaining,
                       usable=remaining, credit_remaining=None)
        return out

    async def check(self, model: str = "") -> dict[str, Any]:
        """Raise BudgetRefused when a live request to `model` must not be made."""
        snap = await self.snapshot()
        if snap["source"] == "openrouter":
            if is_free_model(model) or not model:
                if snap["remaining"] <= self.reserve:
                    raise BudgetRefused(
                        f"OpenRouter free-model requests: {snap['remaining']} left today, keeping a reserve of "
                        f"{self.reserve} ({snap['date']} UTC)", snap)
            else:
                credit = snap.get("credit_remaining")
                if credit is not None and credit <= 0:
                    raise BudgetRefused(f"OpenRouter credit used up (limit_remaining {credit})", snap)
        elif snap["remaining"] <= 0:
            raise BudgetRefused(f"daily LLM request budget used ({self.daily_budget} on {snap['date']}, "
                                "local counter)", snap)
        return snap

    def record(self, n: int = 1) -> int:
        return self.counter.incr(utc_day(), n)


_budgets: dict[tuple[str, str, str], Budget] = {}


def get_budget(settings: Settings | None = None) -> Budget:
    """The process-wide Budget for these settings (shares the 60 s key cache)."""
    s = settings or get_settings()
    k = (s.openrouter_base_url, s.llm_cache_dir, str(id(s)) if settings is not None else "env")
    if k not in _budgets:
        _budgets[k] = Budget(s)
    return _budgets[k]


def reset_budgets() -> None:
    _budgets.clear()
