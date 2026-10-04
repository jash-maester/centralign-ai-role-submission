"""Prompt assembly from ledger state (plans/01-architecture.md §7).

A prompt is rebuilt on every attempt from five layers:

1. role      Role + skill instructions (static, or the agent's versioned prompt)
2. playbook  Relevant playbook sections (selected by step kind)        [toggleable]
3. step      The step: kind, inputs resolved from committed facts, postcondition
4. history   Prior attempts: observations and verifier rejection reasons [locked on]
5. schema    Output schema                                              [toggleable]

Never included: other agents' messages, uncommitted claims, full run history,
and never run/step ids, fences, worker names or timestamps, so identical
inputs give identical prompts (and LLM cache keys) across runs.

Layers 1-2 form the system message, layers 3-5 the user message. Each layer
reports an approximate token count (chars / 4) for the GUI's Prompt tab.
"""

from __future__ import annotations

import json
import math
import re
from dataclasses import dataclass, field
from typing import Any

from pydantic import BaseModel

from .protocol import Attempt, Fact, Postcondition, Step

LAYERS: tuple[str, ...] = ("role", "playbook", "step", "history", "schema")
LAYER_NAMES: dict[str, str] = {
    "role": "Role + skill instructions",
    "playbook": "Playbook sections",
    "step": "Step + inputs from committed facts",
    "history": "Prior attempts + rejection reasons",
    "schema": "Output schema",
}
TOGGLEABLE_LAYERS: frozenset[str] = frozenset({"playbook", "schema"})
LOCKED_LAYERS: frozenset[str] = frozenset({"history"})  # D3 depends on it

# Keys dropped anywhere inside inputs/observations/claims before they reach a prompt.
# (Plain "id" stays: CRM record ids are stable and workers need them.)
VOLATILE_KEYS: frozenset[str] = frozenset({
    "run_id", "step_id", "task_id", "msg_id", "esc_id", "source_step", "fence", "worker",
    "lease_owner", "ts", "timestamp", "created_at", "updated_at", "committed_at", "started_at",
    "ended_at", "stored_at", "evidence",
})
_ID_PATTERN = re.compile(r"\b(run|stp|msg|esc)_[0-9a-f]{6,}\b")
_MAX_VALUE_CHARS = 1500

ROLE_INSTRUCTIONS: dict[str, str] = {
    "orchestrator": (
        "You are the orchestrator of Ledger, an autonomous operator that turns a one-line goal into "
        "verified work on a shared ledger. You plan; you never execute work and never name a worker, "
        "only skills. Every step you create needs a postcondition chosen from the registry so the "
        "verifier can check it through a different channel. Prefer few, concrete steps."
    ),
    "worker": (
        "You are a Ledger worker. Do exactly the one step you were given, using only the inputs shown. "
        "Never invent data that is not in the inputs or the playbook. Your claim is checked by an "
        "independent verifier; a false or partial claim is rejected and costs an attempt. If a prior "
        "attempt was rejected, fix the stated reason first."
    ),
    "verifier": (
        "You are the Ledger verifier's judge. Deterministic checks have already passed. Judge only what "
        "they cannot: tone and policy against the playbook rules shown. Be strict, cite the rule for "
        "every failure, and never assume facts that are not in the input."
    ),
    "meta_reviewer": (
        "You are the Ledger meta-reviewer. Decide review steps from the evidence and the playbook. Give "
        "a calibrated confidence between 0 and 1; when the evidence does not clearly support one option, "
        "say so with a low confidence so the case escalates to a human. Never merge or overwrite CRM data "
        "beyond what the playbook allows."
    ),
}

SKILL_INSTRUCTIONS: dict[str, str] = {
    "file.parse": "Skill file.parse: normalise rows exactly as the dedupe rules say; flag rows you cannot use and say why.",
    "browser.espocrm": "Skill browser.espocrm: act in the EspoCRM web UI. Check before acting: if the record already exists, do not create it again.",
    "api.espocrm": "Skill api.espocrm: act through the EspoCRM REST API. Check before acting: if the record already exists, do not create it again.",
    "email.draft": "Skill email.draft: write the follow-up email from the template and tone rules. Fill every merge field; never leave placeholder text.",
    "email.send": "Skill email.send: send only an approved draft, unchanged, to the approved recipient.",
    "review": "Skill review: choose one of the options given, with your confidence and the evidence for it.",
    "human": "Skill human: present the question, the options and what was tried, briefly.",
}


@dataclass
class PromptLayer:
    id: str
    name: str
    text: str
    enabled: bool = True
    locked: bool = False
    source: str = ""

    @property
    def tokens(self) -> int:
        return approx_tokens(self.text) if self.enabled else 0

    def as_dict(self) -> dict[str, Any]:
        return {"id": self.id, "name": self.name, "enabled": self.enabled, "locked": self.locked,
                "source": self.source, "tokens": self.tokens, "text": self.text}


@dataclass
class AssembledPrompt:
    messages: list[dict[str, str]]
    layers: list[PromptLayer] = field(default_factory=list)

    @property
    def tokens(self) -> dict[str, int]:
        return {layer.id: layer.tokens for layer in self.layers}

    @property
    def total_tokens(self) -> int:
        return sum(self.tokens.values())

    def layer(self, layer_id: str) -> PromptLayer:
        return next(layer for layer in self.layers if layer.id == layer_id)


def approx_tokens(text: str) -> int:
    return math.ceil(len(text) / 4)


def scrub(value: Any) -> Any:
    """Drop volatile keys and mask run/step/message ids, recursively."""
    if isinstance(value, BaseModel):
        value = value.model_dump(mode="json")
    if isinstance(value, dict):
        return {k: scrub(v) for k, v in value.items() if k not in VOLATILE_KEYS}
    if isinstance(value, (list, tuple)):
        return [scrub(v) for v in value]
    if isinstance(value, str):
        masked = _ID_PATTERN.sub(lambda m: f"<{m.group(1)}>", value)
        return masked if len(masked) <= _MAX_VALUE_CHARS else masked[:_MAX_VALUE_CHARS] + "…"
    return value


def _json(value: Any) -> str:
    return json.dumps(scrub(value), sort_keys=True, ensure_ascii=False, indent=1)


def resolve_inputs(inputs: dict[str, Any], facts: dict[str, Any]) -> dict[str, Any]:
    """Replace "fact:<key>" references with committed fact values (Fact or raw).

    Unresolved references stay visible as "<missing fact: key>" so a prompt
    never silently drops an input.
    """

    def one(v: Any) -> Any:
        if isinstance(v, str) and v.startswith("fact:"):
            key = v[5:]
            if key not in facts:
                return f"<missing fact: {key}>"
            f = facts[key]
            return f.value if isinstance(f, Fact) else f
        if isinstance(v, dict):
            return {k: one(x) for k, x in v.items()}
        if isinstance(v, list):
            return [one(x) for x in v]
        return v

    return {k: one(v) for k, v in inputs.items()}


def _role_layer(role: str, skill: str | None, instructions: str | None) -> str:
    base = instructions.strip() if instructions else ROLE_INSTRUCTIONS.get(role, ROLE_INSTRUCTIONS["worker"])
    extra = SKILL_INSTRUCTIONS.get(skill or "")
    return f"{base}\n\n{extra}" if extra else base


def _playbook_layer(sections: Any) -> str:
    if not sections:
        return "Playbook: no sections apply to this step."
    if isinstance(sections, dict):
        items = list(sections.items())
    else:
        items = [(s.title, s.body) if hasattr(s, "title") else (None, str(s)) for s in sections]
    parts = [f"## {t}\n{b.strip()}" if t else str(b).strip() for t, b in items]
    return "Playbook (company rules; they win over habit):\n\n" + "\n\n".join(parts)


def _step_layer(step: Step | dict[str, Any], inputs: dict[str, Any]) -> str:
    s = step.model_dump(mode="json") if isinstance(step, BaseModel) else dict(step)
    post = s.get("postcondition") or {}
    if isinstance(post, Postcondition):
        post = post.model_dump(mode="json")
    lines = [f"Step kind: {s.get('kind')}"]
    if s.get("title"):
        lines.append(f"Title: {scrub(s['title'])}")
    if s.get("side_effect"):
        lines.append("Side effect: yes (check whether it was already done before acting)")
    lines.append("Inputs (from committed facts):\n" + _json(inputs))
    lines.append("Done means (postcondition the verifier will check independently):\n"
                 + _json({"check": post.get("check"), "args": post.get("args", {}), "expect": post.get("expect", {})}))
    return "\n".join(lines)


def _history_layer(prior: list[Attempt] | list[dict[str, Any]]) -> str:
    if not prior:
        return "Prior attempts on this step: none. This is the first attempt."
    out = ["Prior attempts on this step (fix every rejection reason before claiming done):"]
    for i, a in enumerate(prior, 1):
        a = a.model_dump(mode="json") if isinstance(a, BaseModel) else dict(a)
        verdict = a.get("verdict") or {}
        claim = a.get("claim") or {}
        n = a.get("attempt") or i
        outcome = a.get("outcome") or ("rejected" if verdict and not verdict.get("ok") else "unknown")
        out.append(f"\nAttempt {n}: {outcome}")
        if verdict:
            status = "passed" if verdict.get("ok") else "REJECTED"
            out.append(f"- Verifier {status} by check {verdict.get('check')}: {scrub(verdict.get('reason', ''))}")
            if verdict.get("observed"):
                out.append(f"- Verifier observed: {_json(verdict['observed'])}")
        elif outcome == "lease_expired":
            out.append("- The worker stopped responding; the lease expired before a claim.")
        if claim:
            out.append(f"- Claimed: {scrub(claim.get('summary', ''))}")
            if claim.get("data"):
                out.append(f"- Claim data: {_json(claim['data'])}")
        for obs in a.get("observations") or []:
            out.append(f"- Observation: {_json(obs)}")
    return "\n".join(out)


def _schema_layer(schema: type[BaseModel] | dict[str, Any] | None) -> str:
    if schema is None:
        return "Output: plain JSON."
    sch = schema.model_json_schema() if isinstance(schema, type) and issubclass(schema, BaseModel) else schema
    return "Output: one JSON object matching this JSON Schema.\n" + json.dumps(sch, sort_keys=True, separators=(",", ":"))


def assemble(
    role: str,
    step: Step | dict[str, Any],
    resolved_inputs: dict[str, Any] | None,
    prior_attempts: list[Attempt] | list[dict[str, Any]] | None,
    playbook_sections: Any,
    output_schema: type[BaseModel] | dict[str, Any] | None,
    layers: dict[str, bool] | None = None,
    *,
    instructions: str | None = None,
    fixture_key: str | None = None,
) -> AssembledPrompt:
    """Build the five layers of §7 and the messages for llm.complete().

    `layers` toggles layers by id (agent config); only playbook and schema can
    be switched off. `instructions` replaces the default role text (the
    agent's versioned prompt). `fixture_key` adds a `[fixture_key: ...]` hint
    for the scripted backend; keep it stable (e.g. "lead:7"), never an id.
    """
    toggles = {**{k: True for k in LAYERS}, **(layers or {})}
    for locked in LOCKED_LAYERS | {"role", "step"}:
        toggles[locked] = True
    skill = step.get("skill") if isinstance(step, dict) else getattr(step, "skill", None)
    skill = str(skill.value if hasattr(skill, "value") else skill) if skill else None
    inputs = resolved_inputs if resolved_inputs is not None else (
        step.get("inputs", {}) if isinstance(step, dict) else step.inputs)

    built = [
        PromptLayer("role", LAYER_NAMES["role"], _role_layer(role, skill, instructions),
                    source="agent prompt" if instructions else "static"),
        PromptLayer("playbook", LAYER_NAMES["playbook"], _playbook_layer(playbook_sections),
                    enabled=toggles["playbook"], source="playbook"),
        PromptLayer("step", LAYER_NAMES["step"], _step_layer(step, inputs), source="facts"),
        PromptLayer("history", LAYER_NAMES["history"], _history_layer(list(prior_attempts or [])),
                    locked=True, source="step.history"),
        PromptLayer("schema", LAYER_NAMES["schema"], _schema_layer(output_schema),
                    enabled=toggles["schema"], source="schema"),
    ]
    on = {layer.id: layer.text for layer in built if layer.enabled}
    system = "\n\n".join(on[k] for k in ("role", "playbook") if k in on)
    if fixture_key:
        system += f"\n\n[fixture_key: {fixture_key}]"
    user = "\n\n".join(on[k] for k in ("step", "history", "schema") if k in on)
    return AssembledPrompt(messages=[{"role": "system", "content": system}, {"role": "user", "content": user}],
                           layers=built)
