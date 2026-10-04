"""Redis key names (plans/01-architecture.md §3). The only place keys are spelled.

Always build keys through a Keys instance so a namespace can isolate tests and
parallel stacks: Keys("t1").step("stp_1") -> "t1:step:stp_1".
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class Keys:
    ns: str = ""

    def _k(self, *parts: str) -> str:
        key = ":".join(parts)
        return f"{self.ns}:{key}" if self.ns else key

    # ledger
    @property
    def events(self) -> str:
        return self._k("ledger", "events")

    @property
    def runs(self) -> str:  # sorted set run_id -> created_at
        return self._k("runs")

    def run(self, run_id: str) -> str:
        return self._k("run", run_id)

    def run_steps(self, run_id: str) -> str:
        return self._k("run", run_id, "steps")

    def run_determinism(self, run_id: str) -> str:
        return self._k("run", run_id, "determinism")

    def run_config(self, run_id: str) -> str:
        return self._k("run", run_id, "config")

    def step(self, step_id: str) -> str:
        return self._k("step", step_id)

    def facts(self, run_id: str) -> str:
        return self._k("facts", run_id)

    # leases
    def lease(self, step_id: str) -> str:
        return self._k("lease", step_id)

    def fence(self, step_id: str) -> str:
        return self._k("fence", step_id)

    # queues (Streams)
    def queue(self, skill: str) -> str:
        return self._k("queue", skill)

    @property
    def verify_queue(self) -> str:  # steps in claimed_done awaiting the verifier
        return self._k("queue", "verify")

    # agents
    @property
    def agents(self) -> str:
        return self._k("agents")

    def agent_alive(self, agent_id: str) -> str:
        return self._k("agent", agent_id, "alive")

    def agent_config(self, agent_id: str) -> str:
        return self._k("agent", agent_id, "config")

    def agent_prompt(self, agent_id: str, version: int) -> str:
        return self._k("agent", agent_id, "prompt", str(version))

    # review / humans
    def escalation(self, esc_id: str) -> str:
        return self._k("escalation", esc_id)

    @property
    def escalations_open(self) -> str:
        return self._k("escalations", "open")

    # control
    @property
    def faults(self) -> str:
        return self._k("faults")

    @property
    def crm_secrets(self) -> str:  # API keys written by the seed
        return self._k("config", "crm")

    # llm
    def llm_cache(self, digest: str) -> str:
        return self._k("llm", "cache", digest)

    def llm_budget(self, day: str) -> str:
        return self._k("llm", "budget", day)

    def llm_spend(self, run_id: str) -> str:
        return self._k("llm", "spend", run_id)
