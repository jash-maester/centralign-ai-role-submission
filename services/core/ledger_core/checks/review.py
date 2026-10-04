"""review.decided (plans/01 §8): a decision fact exists, from the meta-reviewer
at/above threshold or from a human answer. Plus the fact extractor for review kinds.

args: {lane, decision_key}   (orchestrator_lanes.review_step; decision_key defaults
                              to review:<lane> / approval:<lane>)

Passes when
- the decision fact `decision_key` is committed and agrees with the claim
  (same decision and value), and
- it was decided by a human answering an escalation of this step (the
  escalation is answered with that option), or by the meta-reviewer with
  confidence >= the run's threshold (review_auto_threshold for ambiguity,
  approval_auto_threshold for approvals) and no forced escalation, and
- the chosen option is one of the step's options (when it has any).
Reads only ledger state (facts, escalation records, RunConfig), never the
reviewer's word alone.
"""

from __future__ import annotations

from typing import Any

from .. import escalations
from ..config import RunConfig
from ..orchestrator_lanes import parse_decision
from ..postconditions import CheckContext, CheckResult, register
from ..protocol import Claim, Step, StepKind
from ..verifier import register_facts


def _norm(d: dict[str, Any] | None) -> tuple[str | None, str | None]:
    p = parse_decision(d) if d else None
    if not p:
        return None, None
    val = p.get("value")
    if val in (None, "") or val == p.get("decision"):  # option "skip" carried as the value
        return p.get("decision"), None
    return p.get("decision"), str(val)


@register("review.decided")
async def review_decided(args: dict[str, Any], expect: dict[str, Any], ctx: CheckContext) -> CheckResult:
    step: Step | None = ctx.extra.get("step")
    lane = args.get("lane") or (step.lane if step else None)
    key = args.get("decision_key") or (escalations.decision_key(step) if step else f"review:{lane}")
    fact = ctx.facts.get(key)
    claim = ctx.claim or {}
    observed: dict[str, Any] = {"decision_key": key, "fact": fact}
    if not isinstance(fact, dict) or not fact.get("decision"):
        return CheckResult(False, f"no decision fact {key!r} committed", observed)
    f_dec, f_val = _norm(fact)
    c_dec, c_val = _norm(claim) if claim.get("decision") else (f_dec, f_val)
    if (f_dec, f_val) != (c_dec, c_val):
        return CheckResult(False, f"claim says {c_dec}:{c_val} but fact {key} says {f_dec}:{f_val}", observed)

    options = [o.get("value") for o in (step.inputs.get("options") or []) if isinstance(o, dict)] if step else []
    approval = step is not None and step.kind == StepKind.REVIEW_APPROVAL
    if options and not approval:
        chosen = f"{f_dec}:{f_val}" if f_val else f_dec
        if chosen not in options and f_dec not in options:
            return CheckResult(False, f"decision {chosen} is not one of the options {options}", observed)

    by = fact.get("decided_by") or "meta-reviewer"
    observed.update(decision=f_dec, value=f_val, decided_by=by, confidence=fact.get("confidence"),
                    threshold=fact.get("threshold"), model=fact.get("model"))
    if by == "human":
        r, keys = ctx.extra.get("r"), ctx.extra.get("keys")
        esc_id = fact.get("escalation_id")
        if r is None or keys is None or not esc_id:
            return CheckResult(False, "human decision without an escalation on record", observed)
        esc = await escalations.get(r, keys, esc_id)
        if esc is None or (step is not None and esc.step_id != step.id):
            return CheckResult(False, f"escalation {esc_id} is not this step's", observed)
        if esc.status != "answered" or _norm({"decision": esc.answer})[0] != f_dec:
            return CheckResult(False, f"escalation {esc_id} is {esc.status} with answer {esc.answer}", observed)
        observed["escalation_id"] = esc_id
        return CheckResult(True, f"human answered escalation {esc_id}: {esc.answer}", observed)

    cfg: RunConfig = ctx.extra.get("config") or RunConfig()
    threshold = cfg.approval_auto_threshold if approval else cfg.review_auto_threshold
    conf = float(fact.get("confidence") or 0.0)
    observed["run_threshold"] = threshold
    if fact.get("forced_reason"):
        return CheckResult(False, f"auto decision despite a forced escalation: {fact['forced_reason']}", observed)
    if approval and isinstance(fact.get("escalated"), dict):
        return _per_email(fact, cfg, threshold, observed)
    if approval and (cfg.always_ask_human_email or not cfg.llm_judge_enabled):
        return CheckResult(False, "approval needs a human (always_ask_human_email / judge off)", observed)
    if conf < threshold:
        return CheckResult(False, f"confidence {conf:.2f} < threshold {threshold:.2f}: must escalate", observed)
    return CheckResult(True, f"{by} decided {f_dec}{':' + f_val if f_val else ''} at confidence {conf:.2f} "
                             f">= {threshold:.2f}", observed)


def _per_email(fact: dict[str, Any], cfg: RunConfig, threshold: float, observed: dict[str, Any]) -> CheckResult:
    """Track N: an approval batch decided per email. Every email this step
    auto-approved (fact `lanes`) must have its own judge score >= the run's
    approval_auto_threshold and no flags, with the judge on and
    always_ask_human_email off; the others are listed as escalated (each gets
    its own review.approval step) and none of them may be approved here."""
    items = fact.get("items") or {}
    lanes = list(fact.get("lanes") or [])
    escalated = fact.get("escalated") or {}
    approved = [x for x in lanes if (items.get(x) or {}).get("decision") == "approve"]
    observed.update(auto_approved=approved, escalated=sorted(escalated))
    if approved and (cfg.always_ask_human_email or not cfg.llm_judge_enabled):
        return CheckResult(False, "approval needs a human (always_ask_human_email / judge off)", observed)
    for lane in approved:
        rec = items[lane]
        score = rec.get("score")
        if score is None or float(score) < threshold or rec.get("flags"):
            return CheckResult(False, f"{lane} auto-approved with judge {score} (flags {rec.get('flags') or []}) "
                                      f"below threshold {threshold:.2f}: must escalate", observed)
    both = sorted(set(approved) & set(escalated))
    if both:
        return CheckResult(False, f"{', '.join(both)} both approved and escalated", observed)
    return CheckResult(True, f"{len(approved)} email(s) auto-approved, each judged >= {threshold:.2f} with no flags; "
                             f"{len(escalated)} escalated one by one", observed)


def _review_facts(step: Step, claim: Claim, result: CheckResult) -> dict[str, Any]:
    key = (result.observed or {}).get("decision_key") or escalations.decision_key(step)
    fact = (result.observed or {}).get("fact")
    record = dict(fact) if isinstance(fact, dict) else dict(claim.data)
    return {key: record, f"step:{step.id}": claim.data}


for _kind in (StepKind.REVIEW_AMBIGUITY, StepKind.REVIEW_APPROVAL, StepKind.HUMAN_DECIDE):
    register_facts(_kind)(_review_facts)
