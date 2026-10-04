"""Run configuration: the Run controls panel (plans/01-architecture.md §6a)."""

from __future__ import annotations

import hashlib
import json
from typing import Literal

from pydantic import BaseModel, Field

# Temperature multipliers per role at determinism 0 (§6). Verifier is always 0.
ROLE_TEMPERATURE_SCALE: dict[str, float] = {
    "orchestrator": 0.8,
    "worker": 1.0,
    "meta_reviewer": 0.5,
    "verifier": 0.0,
}


class RunConfig(BaseModel):
    # autonomy
    review_auto_threshold: float = Field(0.80, ge=0, le=1)
    approval_auto_threshold: float = Field(0.90, ge=0, le=1)
    always_ask_human_email: bool = False
    llm_judge_enabled: bool = True
    # reliability
    lease_ttl_s: int = Field(15, ge=3, le=300)
    max_attempts: int = Field(3, ge=1, le=10)
    check_then_act: bool = True
    replan_after_rejections: int = Field(2, ge=0, le=10)
    model_fallback: bool = True
    # execution
    determinism: float = Field(0.80, ge=0, le=1)
    seed: int = 42
    seed_pinned: bool = True
    browser_concurrency: int = Field(2, ge=1, le=2)
    crm_write_path: Literal["browser", "auto", "api"] = "browser"
    # safety
    spend_cap_usd: float = Field(2.00, ge=0)
    fuzzy_match_threshold: float = Field(0.85, ge=0, le=1)
    dry_run: bool = False

    @property
    def heartbeat_s(self) -> float:
        return self.lease_ttl_s / 3

    def temperature(self, role: str) -> float:
        return round(ROLE_TEMPERATURE_SCALE[role] * (1 - self.determinism), 4)

    def config_hash(self) -> str:
        canonical = json.dumps(self.model_dump(mode="json"), sort_keys=True, separators=(",", ":"))
        return hashlib.sha256(canonical.encode()).hexdigest()


def defaults_from(playbook_front_matter: dict | None, env: dict | None = None) -> RunConfig:
    """Playbook front matter wins over env defaults; unknown keys are ignored."""
    merged: dict = {}
    for source in (env or {}, playbook_front_matter or {}):
        merged.update({k: v for k, v in source.items() if k in RunConfig.model_fields})
    return RunConfig(**merged)
