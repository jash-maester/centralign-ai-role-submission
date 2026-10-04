"""Shared data contracts (plans/01-architecture.md §3-4).

Every service and build track imports these models; change them only with a
matching update to the plan. Behaviour (enforcement, persistence) lives in
ledger.py / leases.py; this module is data only.
"""

from __future__ import annotations

import time
import uuid
from enum import StrEnum
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field


def new_id(prefix: str) -> str:
    return f"{prefix}_{uuid.uuid4().hex[:10]}"


def now_ms() -> int:
    return int(time.time() * 1000)


# --------------------------------------------------------------------------
# Step state machine (§3)
# --------------------------------------------------------------------------


class StepStatus(StrEnum):
    PLANNED = "planned"
    READY = "ready"
    LEASED = "leased"
    CLAIMED_DONE = "claimed_done"
    VERIFIED = "verified"
    COMMITTED = "committed"
    REJECTED = "rejected"
    REPLANNED = "replanned"
    LEASE_EXPIRED = "lease_expired"
    REVIEW_REQUIRED = "review_required"
    INPUT_REQUIRED = "input_required"
    DEAD = "dead"


TERMINAL_STATUSES: frozenset[StepStatus] = frozenset(
    {StepStatus.COMMITTED, StepStatus.DEAD, StepStatus.REPLANNED}
)

S = StepStatus
LEGAL_TRANSITIONS: dict[StepStatus, frozenset[StepStatus]] = {
    S.PLANNED: frozenset({S.READY, S.REPLANNED}),
    S.READY: frozenset({S.LEASED, S.REPLANNED}),
    S.LEASED: frozenset({S.CLAIMED_DONE, S.LEASE_EXPIRED, S.REVIEW_REQUIRED}),
    S.CLAIMED_DONE: frozenset({S.VERIFIED, S.REJECTED}),
    S.VERIFIED: frozenset({S.COMMITTED}),
    S.REJECTED: frozenset({S.READY, S.REPLANNED}),
    S.LEASE_EXPIRED: frozenset({S.READY}),
    S.REVIEW_REQUIRED: frozenset({S.READY, S.INPUT_REQUIRED}),
    S.INPUT_REQUIRED: frozenset({S.READY}),
    S.COMMITTED: frozenset(),
    S.REPLANNED: frozenset(),
    S.DEAD: frozenset(),
}
# "any -> dead": every non-terminal status may also move to DEAD.
for _s, _targets in list(LEGAL_TRANSITIONS.items()):
    if _s not in TERMINAL_STATUSES:
        LEGAL_TRANSITIONS[_s] = _targets | {S.DEAD}
del S, _s, _targets

# Only the verifier may move a step into these (§3 rules).
VERIFIER_ONLY_STATUSES: frozenset[StepStatus] = frozenset(
    {StepStatus.VERIFIED, StepStatus.COMMITTED, StepStatus.REJECTED}
)


def is_legal_transition(src: StepStatus, dst: StepStatus) -> bool:
    return dst in LEGAL_TRANSITIONS[src]


class RunStatus(StrEnum):
    CREATED = "created"
    UNDERSTANDING = "understanding"
    PLANNING = "planning"
    RUNNING = "running"
    COMPLETED = "completed"
    COMPLETED_PENDING_INPUT = "completed_pending_input"
    FAILED = "failed"


# --------------------------------------------------------------------------
# Skills and step kinds (routing contract, B3)
# --------------------------------------------------------------------------


class Skill(StrEnum):
    FILE_PARSE = "file.parse"
    BROWSER_ESPOCRM = "browser.espocrm"
    API_ESPOCRM = "api.espocrm"  # C11 fallback; also the Phase 2 temporary handler
    EMAIL_DRAFT = "email.draft"
    EMAIL_SEND = "email.send"
    REVIEW = "review"
    HUMAN = "human"


class StepKind(StrEnum):
    FILE_PARSE = "file.parse"
    CRM_SEARCH_CONTACT = "crm.search_contact"
    CRM_CREATE_CONTACT = "crm.create_contact"
    CRM_UPDATE_CONTACT = "crm.update_contact"
    CRM_CREATE_TASK = "crm.create_task"
    EMAIL_DRAFT = "email.draft"
    EMAIL_SEND = "email.send"
    REVIEW_AMBIGUITY = "review.ambiguity"
    REVIEW_APPROVAL = "review.approval"
    HUMAN_DECIDE = "human.decide"
    RUN_VERIFY = "run.verify"


CRM_KINDS = (
    StepKind.CRM_SEARCH_CONTACT,
    StepKind.CRM_CREATE_CONTACT,
    StepKind.CRM_UPDATE_CONTACT,
    StepKind.CRM_CREATE_TASK,
)

# Which kinds each skill can execute. The orchestrator routes by skill only.
SKILL_KINDS: dict[Skill, tuple[StepKind, ...]] = {
    Skill.FILE_PARSE: (StepKind.FILE_PARSE,),
    Skill.BROWSER_ESPOCRM: CRM_KINDS,
    Skill.API_ESPOCRM: CRM_KINDS,
    Skill.EMAIL_DRAFT: (StepKind.EMAIL_DRAFT,),
    Skill.EMAIL_SEND: (StepKind.EMAIL_SEND,),
    Skill.REVIEW: (StepKind.REVIEW_AMBIGUITY, StepKind.REVIEW_APPROVAL),
    Skill.HUMAN: (StepKind.HUMAN_DECIDE,),
}

# Default postcondition check per kind (names must exist in postconditions.CHECK_NAMES).
DEFAULT_CHECK: dict[StepKind, str] = {
    StepKind.FILE_PARSE: "file.parsed_rows",
    StepKind.CRM_SEARCH_CONTACT: "crm.lookup_matches",
    StepKind.CRM_CREATE_CONTACT: "crm.contact_exists",
    StepKind.CRM_UPDATE_CONTACT: "crm.contact_exists",
    StepKind.CRM_CREATE_TASK: "crm.task_exists",
    StepKind.EMAIL_DRAFT: "email.draft_valid",
    StepKind.EMAIL_SEND: "email.sent",
    StepKind.REVIEW_AMBIGUITY: "review.decided",
    StepKind.REVIEW_APPROVAL: "review.decided",
    StepKind.HUMAN_DECIDE: "review.decided",
    StepKind.RUN_VERIFY: "run.criteria_met",
}


# --------------------------------------------------------------------------
# Records
# --------------------------------------------------------------------------


class Postcondition(BaseModel):
    check: str
    args: dict[str, Any] = Field(default_factory=dict)
    expect: dict[str, Any] = Field(default_factory=dict)


class Verdict(BaseModel):
    ok: bool
    check: str
    reason: str
    observed: dict[str, Any] = Field(default_factory=dict)
    verifier: str = "verifier"
    model: str | None = None  # set when an LLM judge contributed
    ts: int = Field(default_factory=now_ms)


class Claim(BaseModel):
    """What a worker says it did. Never trusted until verified."""

    worker: str
    fence: int
    summary: str
    data: dict[str, Any] = Field(default_factory=dict)
    evidence: list[str] = Field(default_factory=list)  # paths in the evidence volume
    acted: bool = True  # False when check-then-act found the work already done
    ts: int = Field(default_factory=now_ms)


class Attempt(BaseModel):
    """One entry in step.history: a lease through to its verdict."""

    attempt: int
    worker: str | None = None
    fence: int = 0
    model: str | None = None
    observations: list[dict[str, Any]] = Field(default_factory=list)
    claim: Claim | None = None
    verdict: Verdict | None = None
    outcome: str | None = None  # committed | rejected | lease_expired | ...
    started_at: int = Field(default_factory=now_ms)
    ended_at: int | None = None


class Step(BaseModel):
    model_config = ConfigDict(use_enum_values=False)

    id: str = Field(default_factory=lambda: new_id("stp"))
    run_id: str
    kind: StepKind
    skill: Skill
    title: str = ""
    inputs: dict[str, Any] = Field(default_factory=dict)  # values may be "fact:<key>" refs
    postcondition: Postcondition
    depends_on: list[str] = Field(default_factory=list)
    side_effect: bool = False
    idempotency_key: str | None = None
    lane: str | None = None  # e.g. "lead:7" for per-lead fan-out
    status: StepStatus = StepStatus.PLANNED
    attempt: int = 0
    max_attempts: int = 3
    fence: int = 0
    lease_owner: str | None = None
    claim: Claim | None = None
    verdict: Verdict | None = None
    history: list[Attempt] = Field(default_factory=list)
    created_at: int = Field(default_factory=now_ms)
    updated_at: int = Field(default_factory=now_ms)


class Run(BaseModel):
    id: str = Field(default_factory=lambda: new_id("run"))
    goal: str
    input_file: str | None = None  # path under DATA_DIR
    input_sha256: str | None = None
    playbook: str = "event-leads.md"
    playbook_hash: str | None = None
    status: RunStatus = RunStatus.CREATED
    criteria: list[Criterion] = Field(default_factory=list)
    config_hash: str | None = None
    replay_of: str | None = None
    created_at: int = Field(default_factory=now_ms)
    finished_at: int | None = None


class Criterion(BaseModel):
    id: str
    text: str
    check: str  # postcondition name used by the run.criteria_met sweep
    status: Literal["pending", "verified", "waived", "failed"] = "pending"
    evidence: str | None = None


class Fact(BaseModel):
    key: str  # e.g. "lead:7", "lead:7.contact_id", "approval:emails"
    value: Any
    source_step: str
    committed_at: int = Field(default_factory=now_ms)


# --------------------------------------------------------------------------
# Events (§3)
# --------------------------------------------------------------------------


class EventType(StrEnum):
    RUN_CREATED = "run.created"
    RUN_UNDERSTOOD = "run.understood"
    PLAN_CREATED = "plan.created"
    PLAN_REVISED = "plan.revised"
    STEP_READY = "step.ready"
    STEP_LEASED = "step.leased"
    STEP_HEARTBEAT_LOST = "step.heartbeat_lost"
    STEP_LEASE_EXPIRED = "step.lease_expired"
    STEP_OBSERVATION = "step.observation"
    STEP_CLAIMED = "step.claimed"
    STEP_VERIFIED = "step.verified"
    STEP_REJECTED = "step.rejected"
    STEP_COMMITTED = "step.committed"
    STEP_DEAD = "step.dead"
    STEP_REPLANNED = "step.replanned"
    STEP_STALE_FENCE = "step.stale_fence"
    FACT_COMMITTED = "fact.committed"
    INPUT_REQUESTED = "input.requested"
    INPUT_ANSWERED = "input.answered"
    REVIEW_REQUESTED = "review.requested"
    REVIEW_RESOLVED = "review.resolved"
    REVIEW_ESCALATED = "review.escalated"
    APPROVAL_AUTO = "approval.auto"
    CONFIG_UPDATED = "config.updated"
    RUN_CONFIG_UPDATED = "run.config_updated"
    PROMPT_UPDATED = "prompt.updated"
    TOOL_TOGGLED = "tool.toggled"
    SHELL_OPENED = "shell.opened"
    MODEL_FALLBACK = "model.fallback"
    LLM_CALL = "llm.call"
    LLM_CACHE_HIT = "llm.cache_hit"
    LLM_BUDGET_EXHAUSTED = "llm.budget_exhausted"
    SPEND_CAP_REACHED = "spend.cap_reached"
    AGENT_REGISTERED = "agent.registered"
    AGENT_LOST = "agent.lost"
    FAULT_INJECTED = "fault.injected"
    RUN_COMPLETED = "run.completed"
    RUN_COMPLETED_PENDING_INPUT = "run.completed_pending_input"
    RUN_FAILED = "run.failed"


class Event(BaseModel):
    id: str | None = None  # Redis stream id, set on append
    ts: int = Field(default_factory=now_ms)
    run_id: str | None = None
    step_id: str | None = None
    actor: str
    type: EventType
    payload: dict[str, Any] = Field(default_factory=dict)


# --------------------------------------------------------------------------
# Protocol (A2A-shaped, §4)
# --------------------------------------------------------------------------


class FaultName(StrEnum):
    """Fault switches in Keys().faults (F1-F5). Value in the hash = remaining shots / "on"."""

    FALSE_CLAIM = "false_claim"
    KILL_WORKER = "kill_worker"  # recorded for the timeline; the kill itself is docker
    EXPIRE_SESSION = "expire_session"
    MODEL_OUTAGE = "model_outage"
    UI_CHANGED = "ui_changed"


class AgentSkill(BaseModel):
    id: Skill
    kinds: list[StepKind]


class ToolSpec(BaseModel):
    id: str
    type: Literal["rest", "mcp", "function", "browser", "smtp", "llm"]
    name: str
    detail: str = ""
    enabled: bool = True
    locked_reason: str | None = None  # e.g. "Verifier-only channel"


class AgentCard(BaseModel):
    id: str
    name: str
    role: Literal["orchestrator", "verifier", "meta_reviewer", "worker", "human"]
    skills: list[AgentSkill] = Field(default_factory=list)
    model_role: Literal["orchestrator", "worker", "verifier", "meta_reviewer"] | None = None
    side_effects: bool = False
    tools: list[ToolSpec] = Field(default_factory=list)
    container: str | None = None  # compose service name, for shell/restart/kill


class Part(BaseModel):
    kind: Literal["data", "file", "text"]
    data: dict[str, Any] | None = None
    uri: str | None = None
    text: str | None = None


class Envelope(BaseModel):
    id: str = Field(default_factory=lambda: new_id("msg"))
    run_id: str
    task_id: str
    from_: str = Field(alias="from")
    to_skill: Skill
    type: Literal["task.submit", "task.status", "task.artifact", "task.cancel"]
    state: Literal["submitted", "working", "input-required", "completed", "failed"]
    fence: int = 0
    parts: list[Part] = Field(default_factory=list)
    ts: int = Field(default_factory=now_ms)

    model_config = ConfigDict(populate_by_name=True)


class ReviewOption(BaseModel):
    label: str
    value: str
    detail: str = ""


class ReviewDecision(BaseModel):
    decision: Literal["link_account", "match_existing", "create_new", "skip", "approve", "reject"]
    value: str | None = None
    confidence: float
    threshold: float
    evidence: list[str] = Field(default_factory=list)
    options: list[ReviewOption] = Field(default_factory=list)
    decided_by: str = "meta-reviewer"  # or "human"
    model: str | None = None


class Escalation(BaseModel):
    id: str = Field(default_factory=lambda: new_id("esc"))
    run_id: str
    step_id: str
    lane: str | None = None
    question: str
    context: str = ""
    options: list[ReviewOption]
    tried: list[str] = Field(default_factory=list)
    confidence: float
    threshold: float
    status: Literal["open", "answered"] = "open"
    answer: str | None = None
    save_as_rule: bool = False
    created_at: int = Field(default_factory=now_ms)


Run.model_rebuild()
