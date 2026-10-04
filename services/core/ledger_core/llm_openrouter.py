"""OpenRouter backend for llm.complete() (plans/01-architecture.md §6, §6b).

One call walks the role's model list (Settings.models_for(role)):

    for each model:
        cache hit?            -> llm.cache_hit, return (no budget used)
        LLM_CACHE=replay-only -> next model, finally LLMCacheMiss
        spend cap / budget    -> LLMSpendCapReached / LLMBudgetExhausted (no fallback)
        POST /chat/completions (429/5xx/network: retry once with backoff)
        parse + validate      (invalid: re-ask once)
        failure               -> model.fallback to the next model (if RunConfig.model_fallback)

Every HTTP request is budgeted first (llm_budget.Budget: OpenRouter's own
GET /key count when the key is set, a persistent per-day file counter under
LLM_CACHE_DIR otherwise; a reserve is never spent), counted on the stack's
Redis counter (Keys.llm_budget(day), informational) and against the run
(Keys.llm_spend(run_id)), and logged as an llm.call event.

Fault F4: when Keys.faults has `model_outage` set ("on" or a shot count), the
primary model of the roles in MODEL_OUTAGE_ROLES is replaced by an invalid id,
so the provider rejects it and the call falls back.
"""

from __future__ import annotations

import asyncio
import time
from typing import Any

import httpx
import redis.asyncio as aioredis
from pydantic import BaseModel

from . import llm_json
from .config import RunConfig
from .events import append_event
from .keys import Keys
from .llm import (
    CallInfo,
    LLMBudgetExhausted,
    LLMCacheMiss,
    LLMError,
    LLMSpendCapReached,
    Role,
    record_call,
)
from .llm_budget import Budget, BudgetRefused
from .llm_budget import utc_day as utc_day
from .llm_cache import ResponseCache, digest
from .protocol import Event, EventType, FaultName
from .settings import Settings, get_settings

OUTAGE_MODEL = "ledger/injected-model-outage"
MODEL_OUTAGE_ROLES: tuple[str, ...] = ("worker",)  # `make chaos-model-outage`: primary worker model
RETRYABLE_STATUS = frozenset({408, 429, 500, 502, 503, 504, 520, 522, 524, 529})
REASK = (
    "Your previous reply could not be used: {error}. "
    "Reply again with only a JSON object that matches the schema."
)


class _ModelFailed(Exception):
    """This model can't answer this call; the caller may fall back."""


class LedgerSink:
    """Redis handle + event helper shared by the LLM backends."""

    def __init__(self, r: aioredis.Redis | None, keys: Keys | None, settings: Settings) -> None:
        self._r = r
        self.keys = keys or Keys(settings.ledger_ns)
        self.settings = settings
        self.actor = settings.agent_id or "llm"

    @property
    def r(self) -> aioredis.Redis:
        if self._r is None:
            from .redis_conn import connect

            self._r = connect(self.settings.redis_url)
        return self._r

    async def emit(self, type_: EventType, payload: dict[str, Any], run_id: str | None, step_id: str | None) -> None:
        await append_event(self.r, self.keys, Event(actor=self.actor, type=type_, run_id=run_id, step_id=step_id,
                                                    payload=payload))


class OpenRouterBackend:
    # /models capability cache shared by every instance: base_url -> {model: params}
    _capabilities: dict[str, dict[str, frozenset[str]]] = {}

    def __init__(
        self,
        *,
        settings: Settings | None = None,
        r: aioredis.Redis | None = None,
        keys: Keys | None = None,
        cache_dir: str | None = None,
        cache_mode: str | None = None,
        daily_budget: int | None = None,
        backoff_s: float = 2.0,
        max_backoff_s: float = 10.0,
        http: httpx.AsyncClient | None = None,
        budget: Budget | None = None,
    ) -> None:
        self.settings = settings or get_settings()
        self.sink = LedgerSink(r, keys, self.settings)
        self.cache = ResponseCache(cache_dir or self.settings.llm_cache_dir, cache_mode or self.settings.llm_cache)
        self.daily_budget = self.settings.llm_daily_request_budget if daily_budget is None else daily_budget
        self.budget = budget or Budget(self.settings, cache_dir=cache_dir or self.settings.llm_cache_dir,
                                       daily_budget=self.daily_budget)
        self.backoff_s = backoff_s
        self.max_backoff_s = max_backoff_s
        self.base_url = self.settings.openrouter_base_url.rstrip("/")
        self._http = http

    # ---- public -----------------------------------------------------------

    async def complete[T: BaseModel](
        self,
        role: Role,
        messages: list[dict[str, Any]],
        schema: type[T],
        *,
        run_id: str | None = None,
        step_id: str | None = None,
        config: RunConfig | None = None,
    ) -> T:
        cfg = config or await self._run_config(run_id)
        models = self.settings.models_for(role)
        if not models:
            raise LLMError(f"no models configured for role {role!r} (MODEL_{role.upper()})")
        outage = role in MODEL_OUTAGE_ROLES and await self._consume_outage()
        if outage:
            models = [OUTAGE_MODEL, *models[1:]]
            from . import faults

            await faults.attach_pending(self.sink.r, self.sink.keys, FaultName.MODEL_OUTAGE, run_id=run_id,
                                        step_id=step_id)
            await self.sink.emit(EventType.FAULT_INJECTED, {
                "fault": FaultName.MODEL_OUTAGE.value, "switch": FaultName.MODEL_OUTAGE.value, "phase": "consumed",
                "effect": f"primary {role} model replaced by {OUTAGE_MODEL}", "role": role,
            }, run_id, step_id)
        if not cfg.model_fallback:
            models = models[:1]

        temperature = cfg.temperature(role)
        seed = cfg.seed if cfg.seed_pinned else None
        schema_dict = llm_json.json_schema(schema)
        errors: list[str] = []
        fell_back: list[str] = []

        for i, model in enumerate(models):
            key = digest(role, model, messages, schema_dict, temperature, seed)
            hit = self._from_cache(key, schema)
            if hit is not None:
                result, record = hit
                info = CallInfo(role=role, model=model, cached=True,
                                prompt_tokens=record.get("prompt_tokens", 0),
                                completion_tokens=record.get("completion_tokens", 0),
                                fallback_from=fell_back or None)
                await self.sink.emit(EventType.LLM_CACHE_HIT, {
                    "role": role, "model": model, "cached": True, "key": key[:16],
                    "prompt_tokens": info.prompt_tokens, "completion_tokens": info.completion_tokens,
                    "cost_usd": 0.0, "schema": schema.__name__,
                }, run_id, step_id)
                record_call(info)
                return result
            if self.cache.mode == "replay-only":
                errors.append(f"{model}: cache miss")
                continue
            try:
                result, info = await self._call_model(role, model, messages, schema, schema_dict,
                                                      temperature, seed, cfg, run_id, step_id)
            except _ModelFailed as exc:
                reason = f"{'fault model_outage: ' if model == OUTAGE_MODEL else ''}{exc}"
                errors.append(f"{model}: {reason}")
                if i + 1 < len(models):
                    await self.sink.emit(EventType.MODEL_FALLBACK, {
                        "role": role, "from": model, "to": models[i + 1], "reason": reason[:500],
                    }, run_id, step_id)
                    fell_back.append(model)
                    continue
                break
            self.cache.write(key, {
                "role": role, "model": model, "schema": schema.__name__,
                "response": result.model_dump(mode="json"),
                "prompt_tokens": info.prompt_tokens, "completion_tokens": info.completion_tokens,
            })
            info.fallback_from = fell_back or None
            record_call(info)
            return result

        if self.cache.mode == "replay-only":
            raise LLMCacheMiss(f"LLM_CACHE=replay-only and no cached {schema.__name__} for role {role}: {'; '.join(errors)}")
        raise LLMError(f"all models failed for role {role}: {'; '.join(errors)}")

    async def aclose(self) -> None:
        if self._http is not None:
            await self._http.aclose()
            self._http = None

    # ---- one model --------------------------------------------------------

    async def _call_model(
        self, role: str, model: str, messages: list[dict[str, Any]], schema: type[BaseModel],
        schema_dict: dict[str, Any], temperature: float, seed: int | None, cfg: RunConfig,
        run_id: str | None, step_id: str | None,
    ) -> tuple[Any, CallInfo]:
        if not self.settings.openrouter_api_key:
            raise LLMError("OPENROUTER_API_KEY is empty; set it in .env or use LLM_BACKEND=scripted")
        caps = await self._model_params(model)
        structured = caps is not None and bool({"response_format", "structured_outputs"} & caps)
        msgs = [dict(m) for m in messages]
        if not structured:
            msgs.append({"role": "system", "content": llm_json.instruction(schema_dict)})
        body: dict[str, Any] = {"model": model, "messages": msgs, "temperature": temperature,
                                "usage": {"include": True}}
        if seed is not None and (caps is None or "seed" in caps):
            body["seed"] = seed
        if structured:
            strict = llm_json.strict_schema(schema_dict)
            body["response_format"] = {"type": "json_schema", "json_schema": {
                "name": llm_json.schema_name(schema), "strict": strict is not None,
                "schema": strict if strict is not None else schema_dict}}

        info = CallInfo(role=role, model=model)
        content = await self._post(body, info, cfg, run_id, step_id)
        try:
            return llm_json.parse_as(schema, content), info
        except ValueError as first:
            error = str(first)[:800]
        reask = dict(body, messages=[*msgs, {"role": "assistant", "content": (content or "")[:4000]},
                                     {"role": "user", "content": REASK.format(error=error)}])
        content = await self._post(reask, info, cfg, run_id, step_id)
        try:
            return llm_json.parse_as(schema, content), info
        except ValueError as second:
            raise _ModelFailed(f"invalid output after re-ask: {str(second)[:300]}") from second

    async def _post(self, body: dict[str, Any], info: CallInfo, cfg: RunConfig,
                    run_id: str | None, step_id: str | None) -> str:
        """POST once, retrying once on 429/5xx/network errors. Returns message content."""
        last = "no attempt"
        for attempt in (1, 2):
            await self._guard(cfg, run_id, str(body.get("model") or ""))
            t0 = time.monotonic()
            retry_after: float | None = None
            status: int | None = None
            data: dict[str, Any] = {}
            try:
                resp = await self._client().post("/chat/completions", json=body)
                status = resp.status_code
                retry_after = _retry_after(resp)
                try:
                    data = resp.json()
                except ValueError:
                    data = {}
            except httpx.HTTPError as exc:
                last, retryable = f"{type(exc).__name__}: {exc}", True
            latency = int((time.monotonic() - t0) * 1000)

            err = data.get("error") if isinstance(data, dict) else None
            choices = data.get("choices") if isinstance(data, dict) else None
            if status is not None:
                if status == 200 and choices and not err:
                    retryable = False
                else:
                    code = _int((err or {}).get("code")) or status
                    message = (err or {}).get("message") or (resp.text[:200] if status != 200 else "no choices")
                    last = f"HTTP {code}: {message}"
                    retryable = code in RETRYABLE_STATUS
            ok = status == 200 and bool(choices) and not err
            usage = data.get("usage") or {} if ok else {}
            await self._account(info, usage, latency, ok, status, last if not ok else None, run_id, step_id)
            if ok:
                message = choices[0].get("message") or {}
                return message.get("content") or ""
            if not retryable or attempt == 2:
                break
            delay = max(self.backoff_s, retry_after or 0.0)
            await asyncio.sleep(min(delay, self.max_backoff_s))
        raise _ModelFailed(last)

    # ---- budget, spend, accounting ------------------------------------------

    async def _guard(self, cfg: RunConfig, run_id: str | None, model: str = "") -> None:
        """Refuse before a live request when the spend cap or the daily budget
        (llm_budget: OpenRouter's count minus the reserve, else the local
        counter) is used; otherwise count the request."""
        r, keys = self.sink.r, self.sink.keys
        if run_id:
            spent = float(await r.hget(keys.llm_spend(run_id), "usd") or 0.0)
            if spent > 0 and spent >= cfg.spend_cap_usd:
                if await r.hsetnx(keys.llm_spend(run_id), "cap_reached", "1"):
                    await self.sink.emit(EventType.SPEND_CAP_REACHED, {
                        "spent_usd": round(spent, 6), "cap_usd": cfg.spend_cap_usd,
                    }, run_id, None)
                raise LLMSpendCapReached(f"spend cap reached: ${spent:.4f} of ${cfg.spend_cap_usd:.2f}")
        try:
            await self.budget.check(model)
        except BudgetRefused as exc:
            snap = exc.snapshot
            await self.sink.emit(EventType.LLM_BUDGET_EXHAUSTED, {
                "day": snap.get("date"), "source": snap.get("source"), "budget": snap.get("limit"),
                "used": snap.get("used"), "remaining": snap.get("remaining"), "reserve": snap.get("reserve"),
                "model": model, "reason": str(exc),
            }, run_id, None)
            raise LLMBudgetExhausted(str(exc)) from exc
        self.budget.record()
        budget_key = keys.llm_budget(utc_day())  # this stack's count (informational; lost on down -v)
        if await r.incr(budget_key) == 1:
            await r.expire(budget_key, 3 * 86400)

    async def _account(self, info: CallInfo, usage: dict[str, Any], latency: int, ok: bool,
                       status: int | None, error: str | None, run_id: str | None, step_id: str | None) -> None:
        prompt = _int(usage.get("prompt_tokens"))
        completion = _int(usage.get("completion_tokens"))
        cost = float(usage.get("cost") or 0.0)
        info.prompt_tokens += prompt
        info.completion_tokens += completion
        info.cost_usd += cost
        info.latency_ms += latency
        if run_id:
            r, key = self.sink.r, self.sink.keys.llm_spend(run_id)
            pipe = r.pipeline(transaction=False)
            pipe.hincrby(key, "requests", 1)
            pipe.hincrby(key, f"requests:{info.role}", 1)
            pipe.hincrby(key, "prompt_tokens", prompt)
            pipe.hincrby(key, "completion_tokens", completion)
            pipe.hincrby(key, f"tokens:{info.role}", prompt + completion)
            if cost:
                pipe.hincrbyfloat(key, "usd", cost)
                pipe.hincrbyfloat(key, f"usd:{info.role}", cost)
            await pipe.execute()
        payload: dict[str, Any] = {
            "role": info.role, "model": info.model, "ok": ok, "status": status, "cached": False,
            "prompt_tokens": prompt, "completion_tokens": completion, "total_tokens": prompt + completion,
            "cost_usd": cost, "latency_ms": latency,
        }
        if error:
            payload["error"] = error[:500]
        await self.sink.emit(EventType.LLM_CALL, payload, run_id, step_id)

    # ---- helpers ------------------------------------------------------------

    def _from_cache(self, key: str, schema: type[BaseModel]) -> tuple[Any, dict[str, Any]] | None:
        record = self.cache.read(key)
        if not record or "response" not in record:
            return None
        try:
            return schema.model_validate(record["response"]), record
        except ValueError:
            return None

    async def _consume_outage(self) -> bool:
        """F4: one shot of the model_outage switch (read through ledger_core.faults)."""
        from . import faults

        return bool(await faults.consume(self.sink.r, self.sink.keys, FaultName.MODEL_OUTAGE))

    async def _run_config(self, run_id: str | None) -> RunConfig:
        from . import run_config

        if run_id:
            return await run_config.get(self.sink.r, self.sink.keys, run_id)
        return run_config.defaults(settings=self.settings)

    def _client(self) -> httpx.AsyncClient:
        if self._http is None:
            self._http = httpx.AsyncClient(
                base_url=self.base_url,
                timeout=httpx.Timeout(self.settings.llm_timeout_s, connect=10.0),
                headers={
                    "Authorization": f"Bearer {self.settings.openrouter_api_key}",
                    "HTTP-Referer": "https://github.com/ledger-poc",
                    "X-Title": "Ledger",
                },
            )
        return self._http

    async def _model_params(self, model: str) -> frozenset[str] | None:
        """supported_parameters for model from GET /models (fetched once), None if unknown."""
        table = self._capabilities.get(self.base_url)
        if table is None:
            # Concurrent first calls may both fetch; harmless and lock-free.
            table = {}
            try:
                resp = await self._client().get("/models")
                resp.raise_for_status()
                for row in resp.json().get("data", []):
                    table[row["id"]] = frozenset(row.get("supported_parameters") or [])
            except (httpx.HTTPError, ValueError, KeyError, TypeError):
                table = {}
            self._capabilities[self.base_url] = table
        return table.get(model)

    @classmethod
    def reset_capabilities(cls) -> None:
        """Forget fetched /models data (tests, or after the model list changes)."""
        cls._capabilities.clear()


def _int(value: Any) -> int:
    try:
        return int(value or 0)
    except (TypeError, ValueError):
        return 0


def _retry_after(resp: httpx.Response) -> float | None:
    raw = resp.headers.get("retry-after")
    try:
        return float(raw) if raw else None
    except ValueError:
        return None
