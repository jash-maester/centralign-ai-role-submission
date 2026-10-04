"""Orchestrator LLM stages: understand and plan (plans/03 Phase 3, B1, B2).

    understanding = await understand(goal, playbook, input_file=..., run_id=..., config=...)
    plan          = await make_plan(goal, understanding, playbook, input_file=..., run_id=..., config=...)

Both call llm.complete(role="orchestrator") with a pydantic schema, validate
the answer against the registries (protocol.SKILL_KINDS / StepKind,
postconditions.CHECK_NAMES and the implemented checks) and re-ask the model
with the list of problems when it is invalid (at most MAX_ASKS calls per
stage). Messages are built only from the goal, the input file name, the
playbook and the registries, never run/step ids or timestamps, so the same
inputs give the same prompt and LLM cache key (replay, F6, F9).

Scripted fixtures (LLM_BACKEND=scripted) answer by schema name: `Understanding`
and `Plan` (tests/fixtures/llm/orchestrator.json).

The initial plan contains only run-level steps (normally one file.parse).
Per-lead steps are created by deterministic fan-out after the parse step
commits (B4), never by the LLM; a plan that tries to is invalid and re-asked.
"""

from __future__ import annotations

import json
from typing import Any

from pydantic import BaseModel, Field

from . import llm, postconditions
from .config import RunConfig
from .protocol import CRM_KINDS, DEFAULT_CHECK, SKILL_KINDS, Criterion, Skill, StepKind

MAX_ASKS = 3

# Checks a step of each kind may use (the default plus close relatives).
ALLOWED_CHECKS: dict[StepKind, set[str]] = {k: {v} for k, v in DEFAULT_CHECK.items()}
for _k in CRM_KINDS:
    ALLOWED_CHECKS[_k] |= {"crm.no_duplicate"}
ALLOWED_CHECKS[StepKind.CRM_UPDATE_CONTACT] |= {"crm.contact_exists"}

# Kinds the initial plan may contain; everything per-lead comes from fan-out.
INITIAL_KINDS: frozenset[StepKind] = frozenset({StepKind.FILE_PARSE})

CHECK_DESCRIPTIONS: dict[str, str] = {
    "file.parsed_rows": "every source row accounted for (usable or flagged with a reason), emails/names match the file",
    "crm.lookup_matches": "a CRM search result (matched / ambiguous / none) agrees with a REST lookup",
    "crm.contact_exists": "a CRM contact with the email exists, with the expected owner/account",
    "crm.no_duplicate": "exactly one CRM contact per normalised email (primary or secondary)",
    "crm.task_exists": "a follow-up task linked to the contact, with subject, due date and owner",
    "email.draft_valid": "a draft has the right recipient, filled merge fields, no placeholders, passes tone rules",
    "email.sent": "Mailpit shows the message delivered to the recipient exactly once",
    "review.decided": "a recorded decision exists for a review (meta-reviewer above threshold or a human answer)",
    "run.criteria_met": "the final sweep itself (not usable as a criterion)",
}


class InvalidOutput(llm.LLMError):
    """The model kept producing invalid output after MAX_ASKS attempts."""

    def __init__(self, stage: str, problems: list[str]) -> None:
        super().__init__(f"{stage}: invalid model output after {MAX_ASKS} attempts: " + "; ".join(problems[:6]))
        self.stage, self.problems = stage, problems


# ---------------------------------------------------------------------------
# schemas (names are the scripted-fixture keys)
# ---------------------------------------------------------------------------


class CriterionDraft(BaseModel):
    id: str = Field(description="short id, e.g. c1")
    text: str = Field(description="one checkable sentence")
    check: str = Field(description="postcondition check name from the registry")


class Understanding(BaseModel):
    summary: str = Field("", description="one sentence: what the goal means under this playbook")
    event_name: str | None = Field(None, description="event name if the goal or file names it")
    event_date: str | None = Field(None, description="YYYY-MM-DD if stated; null if relative (e.g. yesterday)")
    criteria: list[CriterionDraft]


class PostconditionDraft(BaseModel):
    check: str
    args: dict[str, Any] = Field(default_factory=dict)
    expect: dict[str, Any] = Field(default_factory=dict)


class PlanStepDraft(BaseModel):
    ref: str = Field(description="short unique reference used in depends_on")
    kind: str = Field(description="step kind, e.g. file.parse")
    skill: str | None = Field(None, description="skill that executes the kind; omit to use the default")
    title: str = ""
    inputs: dict[str, Any] = Field(default_factory=dict)
    postcondition: PostconditionDraft
    depends_on: list[str] = Field(default_factory=list)


class Plan(BaseModel):
    rationale: str = ""
    steps: list[PlanStepDraft]
    per_lead: list[str] = Field(default_factory=list,
                                description="step kinds each lead lane will get after fan-out (informational)")


# ---------------------------------------------------------------------------
# validation
# ---------------------------------------------------------------------------


def implemented_checks() -> set[str]:
    postconditions.load_all()
    return set(postconditions.REGISTRY)


def validate_understanding(u: Understanding) -> list[str]:
    problems: list[str] = []
    if not u.criteria:
        problems.append("criteria is empty: give at least one checkable success criterion")
    seen: set[str] = set()
    for c in u.criteria:
        if not c.id.strip():
            problems.append(f"criterion {c.text[:40]!r} has no id")
        elif c.id in seen:
            problems.append(f"criterion id {c.id!r} is used twice")
        seen.add(c.id)
        if not c.text.strip():
            problems.append(f"criterion {c.id} has no text")
        if c.check not in postconditions.CHECK_NAMES:
            problems.append(f"criterion {c.id}: unknown check {c.check!r} (known: {', '.join(postconditions.CHECK_NAMES)})")
        elif c.check == "run.criteria_met":
            problems.append(f"criterion {c.id}: run.criteria_met is the sweep itself; pick the check that verifies it")
    return problems


def skill_for(kind: StepKind, skill: str | None = None) -> Skill:
    if skill:
        return Skill(skill)
    for s, kinds in SKILL_KINDS.items():
        if kind in kinds:
            return s
    raise ValueError(f"no skill executes {kind.value}")


def validate_plan(plan: Plan, *, input_file: str | None) -> list[str]:
    problems: list[str] = []
    if not plan.steps:
        problems.append("steps is empty")
    refs = [s.ref for s in plan.steps]
    if len(set(refs)) != len(refs):
        problems.append(f"step refs must be unique: {refs}")
    implemented = implemented_checks()
    for s in plan.steps:
        try:
            kind = StepKind(s.kind)
        except ValueError:
            problems.append(f"step {s.ref}: unknown kind {s.kind!r} (known: {', '.join(k.value for k in StepKind)})")
            continue
        if kind not in INITIAL_KINDS:
            problems.append(f"step {s.ref}: {kind.value} is not allowed in the initial plan; per-lead steps are "
                            "created by deterministic fan-out after the parse step commits")
        if s.skill is not None:
            try:
                skill = Skill(s.skill)
            except ValueError:
                problems.append(f"step {s.ref}: unknown skill {s.skill!r} (known: {', '.join(x.value for x in Skill)})")
            else:
                if kind not in SKILL_KINDS[skill]:
                    problems.append(f"step {s.ref}: skill {skill.value} cannot execute {kind.value}")
        elif not any(kind in ks for ks in SKILL_KINDS.values()):
            problems.append(f"step {s.ref}: no skill executes {kind.value}")
        check = s.postcondition.check
        if check not in postconditions.CHECK_NAMES:
            problems.append(f"step {s.ref}: unknown check {check!r} (known: {', '.join(postconditions.CHECK_NAMES)})")
        elif check not in implemented:
            problems.append(f"step {s.ref}: check {check!r} is not implemented yet")
        elif check not in ALLOWED_CHECKS.get(kind, set()):
            problems.append(f"step {s.ref}: check {check!r} does not verify a {kind.value} step "
                            f"(use {sorted(ALLOWED_CHECKS.get(kind, set()))})")
        for d in s.depends_on:
            if d not in refs:
                problems.append(f"step {s.ref}: depends_on {d!r} is not a step ref")
            if d == s.ref:
                problems.append(f"step {s.ref}: depends on itself")
        if kind == StepKind.FILE_PARSE and input_file:
            f = s.inputs.get("file") or s.postcondition.args.get("file")
            if f and _base(f) != _base(input_file):
                problems.append(f"step {s.ref}: parses {f!r} but the run's input file is {input_file!r}")
    if input_file and not any(s.kind == StepKind.FILE_PARSE.value for s in plan.steps):
        problems.append(f"the run has input file {input_file!r}: the plan needs a file.parse step for it")
    if _has_cycle(plan):
        problems.append("depends_on has a cycle")
    for k in plan.per_lead:
        if k not in {x.value for x in StepKind}:
            problems.append(f"per_lead: unknown kind {k!r}")
    return problems


def _base(path: str) -> str:
    return str(path).rstrip("/").rsplit("/", 1)[-1]


def _has_cycle(plan: Plan) -> bool:
    deps = {s.ref: set(s.depends_on) for s in plan.steps}
    state: dict[str, int] = {}

    def visit(n: str) -> bool:
        if state.get(n) == 1:
            return True
        if state.get(n) == 2 or n not in deps:
            return False
        state[n] = 1
        if any(visit(d) for d in deps[n]):
            return True
        state[n] = 2
        return False

    return any(visit(n) for n in deps)


def normalise_plan(plan: Plan, *, input_file: str | None) -> Plan:
    """Fill harmless gaps after validation: the parse step's file arg."""
    for s in plan.steps:
        if s.kind == StepKind.FILE_PARSE.value and input_file:
            s.inputs.setdefault("file", input_file)
            s.postcondition.args.setdefault("file", s.inputs["file"])
    return plan


def criteria_from(u: Understanding) -> list[Criterion]:
    return [Criterion(id=c.id, text=c.text, check=c.check) for c in u.criteria]


# ---------------------------------------------------------------------------
# messages
# ---------------------------------------------------------------------------


def _registry_text() -> str:
    lines = ["Postcondition checks (registry):"]
    lines += [f"- {name}: {CHECK_DESCRIPTIONS.get(name, '')}" for name in postconditions.CHECK_NAMES]
    lines.append("Skills and the step kinds they execute:")
    lines += [f"- {s.value}: {', '.join(k.value for k in ks)}" for s, ks in SKILL_KINDS.items()]
    return "\n".join(lines)


def _system(playbook: Any, instructions: str | None, stage: str) -> str:
    from .prompts import ROLE_INSTRUCTIONS

    sections = playbook.sections_for(None) if hasattr(playbook, "sections_for") else []
    body = "\n\n".join(s.markdown() for s in sections) if sections else str(playbook or "")
    return "\n\n".join([
        instructions or ROLE_INSTRUCTIONS["orchestrator"],
        f"Playbook ({getattr(playbook, 'name', 'playbook')}):\n\n{body}".rstrip(),
        _registry_text(),
        f"[stage: {stage}]",
    ])


def _schema_text(schema: type[BaseModel]) -> str:
    return "Output: one JSON object matching this JSON Schema.\n" + json.dumps(
        schema.model_json_schema(), sort_keys=True, separators=(",", ":"))


def understand_messages(goal: str, playbook: Any, *, input_file: str | None, context: dict[str, Any] | None = None,
                        instructions: str | None = None) -> list[dict[str, Any]]:
    user = [
        "Stage: understand.",
        f"Goal: {goal}",
        f"Input file: {input_file or '(none)'}",
    ]
    if context:
        user.append("Input file metadata: " + json.dumps(context, sort_keys=True))
    user += [
        "Turn the goal into explicit, checkable success criteria using the playbook's definitions of done. "
        "Each criterion names exactly one check from the registry that can verify it from the outside "
        "(CRM REST, Mailpit, the source file, recorded decisions). Do not use run.criteria_met.",
        _schema_text(Understanding),
    ]
    return [{"role": "system", "content": _system(playbook, instructions, "understand")},
            {"role": "user", "content": "\n\n".join(user)}]


def plan_messages(goal: str, criteria: list[Criterion], playbook: Any, *, input_file: str | None,
                  instructions: str | None = None) -> list[dict[str, Any]]:
    crit = "\n".join(f"- {c.id}: {c.text} [check {c.check}]" for c in criteria)
    user = [
        "Stage: plan.",
        f"Goal: {goal}",
        f"Input file: {input_file or '(none)'}",
        f"Success criteria:\n{crit}",
        "Plan the initial steps only. The input file must be parsed first (kind file.parse, check "
        "file.parsed_rows with args.file). Do not plan per-lead steps: after the parse step commits, the "
        "orchestrator fans out one lane per usable lead deterministically (search -> create or update -> "
        "follow-up task; review steps for ambiguous rows). List those lane kinds in per_lead. Never name a "
        "worker, only skills.",
        _schema_text(Plan),
    ]
    return [{"role": "system", "content": _system(playbook, instructions, "plan")},
            {"role": "user", "content": "\n\n".join(user)}]


def _reask(messages: list[dict[str, Any]], answer: BaseModel | None, problems: list[str]) -> list[dict[str, Any]]:
    out = list(messages)
    if answer is not None:
        out.append({"role": "assistant", "content": answer.model_dump_json()})
    out.append({"role": "user", "content": "Your previous answer was invalid:\n" + "\n".join(f"- {p}" for p in problems)
                + "\nReturn a corrected JSON object that fixes every problem."})
    return out


# ---------------------------------------------------------------------------
# stages
# ---------------------------------------------------------------------------


async def _ask(stage: str, schema: type[BaseModel], messages: list[dict[str, Any]], validate, *,
               run_id: str | None, config: RunConfig | None, on_invalid=None):
    problems: list[str] = []
    answer = None
    for attempt in range(1, MAX_ASKS + 1):
        msgs = messages if attempt == 1 else _reask(messages, answer, problems)
        answer = await llm.complete("orchestrator", msgs, schema, run_id=run_id, config=config)
        problems = validate(answer)
        if not problems:
            return answer, attempt
        if on_invalid is not None:
            await on_invalid(stage, attempt, problems)
    raise InvalidOutput(stage, problems)


async def understand(goal: str, playbook: Any, *, input_file: str | None = None, context: dict[str, Any] | None = None,
                     run_id: str | None = None, config: RunConfig | None = None, instructions: str | None = None,
                     on_invalid=None) -> tuple[Understanding, int]:
    msgs = understand_messages(goal, playbook, input_file=input_file, context=context, instructions=instructions)
    return await _ask("understand", Understanding, msgs, validate_understanding, run_id=run_id, config=config,
                      on_invalid=on_invalid)


async def make_plan(goal: str, criteria: list[Criterion], playbook: Any, *, input_file: str | None = None,
                    run_id: str | None = None, config: RunConfig | None = None, instructions: str | None = None,
                    on_invalid=None) -> tuple[Plan, int]:
    msgs = plan_messages(goal, criteria, playbook, input_file=input_file, instructions=instructions)
    plan, asks = await _ask("plan", Plan, msgs, lambda p: validate_plan(p, input_file=input_file), run_id=run_id,
                            config=config, on_invalid=on_invalid)
    return normalise_plan(plan, input_file=input_file), asks
