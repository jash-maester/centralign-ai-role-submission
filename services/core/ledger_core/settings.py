"""Environment settings shared by all services."""

from __future__ import annotations

from functools import lru_cache

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=None, extra="ignore")

    agent_id: str = "local"
    redis_url: str = "redis://redis:6379/0"
    # Key namespace; tests set a unique one so parallel suites never collide.
    ledger_ns: str = ""

    playbook_dir: str = "/app/playbooks"
    data_dir: str = "/app/data"
    evidence_dir: str = "/evidence"

    openrouter_api_key: str = ""
    openrouter_base_url: str = "https://openrouter.ai/api/v1"
    model_orchestrator: str = ""
    model_worker: str = ""
    model_verifier: str = ""
    model_meta_reviewer: str = ""
    llm_daily_request_budget: int = 45
    llm_cache: str = "on"  # on | off | replay-only
    llm_live_tests: bool = False

    review_auto_threshold: float = 0.80
    approval_auto_threshold: float = 0.90
    determinism: float = 0.8

    espo_admin_user: str = "admin"
    espo_admin_password: str = ""
    espo_operator_user: str = "ledger.operator"
    espo_operator_password: str = ""
    crm_internal_url: str = "http://espocrm"
    crm_public_url: str = "http://localhost:8080"

    smtp_host: str = "mailpit"
    smtp_port: int = 1025
    mailpit_api_url: str = "http://mailpit:8025"

    def models_for(self, role: str) -> list[str]:
        raw = getattr(self, f"model_{role}", "")
        return [m.strip() for m in raw.split(",") if m.strip()]


@lru_cache
def get_settings() -> Settings:
    return Settings()
