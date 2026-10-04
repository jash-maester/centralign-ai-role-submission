"""LLM judge for soft checks (plans/01 §8, D4).

The judge runs with role "verifier" (MODEL_VERIFIER: a different model family
from the workers) and only ever *after* the deterministic check passed: it can
fail a step, never rescue one.

    judge = make_judge()                                  # Verifier(judge=judge)
    verdict = await judge_draft(draft, run_id=..., step_id=..., playbook_text=...)
    verdicts = await judge_drafts([{"id": "lead:1", "to":..., "subject":..., "body":...}, ...],
                                  run_id=...)             # ONE LLM call for a batch (Track K approval)

A judged draft passes when score >= PASS_THRESHOLD and the judge raised no
policy flags. The verifier records score, flags, reasoning and the model in
the verdict (Verdict.model, observed["judge"]).

When the LLM is unavailable (budget exhausted, spend cap, every model failed)
the deterministic verdict stands and observed["judge"] = {"skipped": reason}:
the meta-reviewer then has no score and escalates instead of auto-approving.
"""

from __future__ import annotations

import json
import logging
from typing import Any

from pydantic import BaseModel, Field

from . import llm, playbook
from .config import RunConfig
from .postconditions import CheckContext, CheckResult
from .protocol import Step, StepKind

log = logging.getLogger("ledger.judge")

PASS_THRESHOLD = 0.6
SOFT_CHECKS: frozenset[str] = frozenset({"email.draft_valid"})

PLAYBOOK_DIR: str | None = None  # None = Settings.playbook_dir (tests point it at /repo/playbooks)

POLICY_FLAGS = ("pricing", "promise", "attachment", "link", "tone", "personal_data", "off_topic", "inaccurate")


class JudgeVerdict(BaseModel):
    """One draft, judged against the playbook's tone rules."""

    score: float = Field(ge=0, le=1, description="0 = unusable, 1 = ready to send as is")
    flags: list[str] = Field(default_factory=list, description=f"policy problems, from: {', '.join(POLICY_FLAGS)}")
    reasoning: str = Field(description="one or two sentences")


class JudgedDraft(JudgeVerdict):
    id: str = Field(description="the draft id given in the input")


class BatchJudgeVerdict(BaseModel):
    items: list[JudgedDraft]


ROLE_TEXT = (
    "You are the verifier's tone judge for follow-up emails a sales team sends after an event. "
    "A deterministic check already confirmed recipient, subject, merge fields, length and placeholders. "
    "Judge only what a rule cannot: does the email follow the playbook's tone rules? "
    "It must be short, warm, specific to the event, signed by the owner, with no pricing, discounts, "
    "promises of features or dates, attachments, or links other than the company site. "
    "Score 0-1 (>= 0.9 ready to send, < 0.6 must be redrafted). List policy flags only for real violations, "
    f"using these names: {', '.join(POLICY_FLAGS)}. Answer in JSON."
)


def _playbook_text(name: str | None) -> str:
    try:
        pb = playbook.load(name or playbook.DEFAULT_PLAYBOOK, PLAYBOOK_DIR)
        return "\n\n".join(f"## {s.title}\n{s.body.strip()}" for s in pb.sections_for(StepKind.EMAIL_DRAFT))
    except Exception:  # noqa: BLE001 - the judge still works without the playbook text
        return ""


def _draft_view(d: dict[str, Any]) -> dict[str, Any]:
    return {k: d.get(k) for k in ("id", "to", "subject", "body") if d.get(k) is not None}


def _messages(payload: Any, playbook_text: str, instruction: str) -> list[dict[str, Any]]:
    msgs = [{"role": "system", "content": ROLE_TEXT}]
    if playbook_text:
        msgs.append({"role": "system", "content": "Playbook rules:\n" + playbook_text})
    msgs.append({"role": "user", "content": instruction + "\n\n" + json.dumps(payload, indent=2, sort_keys=True)})
    return msgs


def passes(v: JudgeVerdict, threshold: float = PASS_THRESHOLD) -> bool:
    return v.score >= threshold and not v.flags


async def judge_draft(
    draft: dict[str, Any], *, run_id: str | None = None, step_id: str | None = None,
    playbook_text: str | None = None, config: RunConfig | None = None,
) -> tuple[JudgeVerdict, str | None]:
    """Judge one draft. Returns (verdict, model)."""
    text = _playbook_text(None) if playbook_text is None else playbook_text
    v = await llm.complete("verifier", _messages(_draft_view(draft), text, "Judge this draft:"), JudgeVerdict,
                           run_id=run_id, step_id=step_id, config=config)
    info = llm.last_call()
    return v, info.model if info else None


async def judge_drafts(
    drafts: list[dict[str, Any]], *, run_id: str | None = None, step_id: str | None = None,
    playbook_text: str | None = None, config: RunConfig | None = None,
) -> tuple[dict[str, JudgeVerdict], str | None]:
    """Score a whole batch of drafts in ONE LLM call (used for email approval).
    Each draft needs an "id" (defaults to its index). Returns ({id: verdict}, model).
    A draft the model left out comes back with score 0 and flag "missing"."""
    if not drafts:
        return {}, None
    items = [{**_draft_view(d), "id": str(d.get("id", i))} for i, d in enumerate(drafts)]
    text = _playbook_text(None) if playbook_text is None else playbook_text
    out = await llm.complete(
        "verifier", _messages(items, text, f"Judge each of these {len(items)} drafts; return one item per id:"),
        BatchJudgeVerdict, run_id=run_id, step_id=step_id, config=config)
    info = llm.last_call()
    got = {j.id: JudgeVerdict(score=j.score, flags=j.flags, reasoning=j.reasoning) for j in out.items}
    result = {}
    for it in items:
        result[it["id"]] = got.get(it["id"]) or JudgeVerdict(score=0.0, flags=["missing"],
                                                             reasoning="the judge returned no verdict for this draft")
    return result, info.model if info else None


def make_judge(threshold: float = PASS_THRESHOLD):
    """A Verifier judge (D4): runs for soft checks only, after the deterministic
    check passed, when RunConfig.llm_judge_enabled. Never turns a failure into a pass."""

    async def judge(step: Step, result: CheckResult, ctx: CheckContext) -> CheckResult:
        if step.postcondition.check not in SOFT_CHECKS or not result.ok:
            return result
        from . import ledger

        cfg: RunConfig | None = ctx.extra.get("config")
        if cfg is None:
            r, keys = ctx.extra.get("r"), ctx.extra.get("keys")
            cfg = await ledger.get_run_config(r, keys, step.run_id) if r is not None and keys is not None \
                else RunConfig()
        if not cfg.llm_judge_enabled:
            return CheckResult(True, result.reason, {**result.observed, "judge": {"skipped": "llm_judge_enabled=false"}})
        draft = (step.postcondition.args or {}).get("draft") or ctx.claim or {}
        try:
            v, model = await judge_draft(draft, run_id=step.run_id, step_id=step.id,
                                         playbook_text=_playbook_text(ctx.extra.get("playbook")), config=cfg)
        except llm.LLMError as exc:
            log.warning("judge unavailable for %s: %s", step.id, exc)
            return CheckResult(True, result.reason + "; LLM judge skipped (" + type(exc).__name__ + ")",
                               {**result.observed, "judge": {"skipped": f"{type(exc).__name__}: {exc}"[:300]}})
        observed = {**result.observed, "judge": {"score": v.score, "flags": v.flags, "reasoning": v.reasoning,
                                                 "model": model, "threshold": threshold}}
        if passes(v, threshold):
            return CheckResult(True, f"{result.reason}; judge {v.score:.2f} ({model})", observed)
        why = f"score {v.score:.2f} < {threshold}" if v.score < threshold else "policy flags " + ", ".join(v.flags)
        return CheckResult(False, f"LLM judge ({model}) failed the draft: {why}: {v.reasoning}", observed)

    return judge
