"""Dead lane steps go to a human instead of failing the run (Track N; plans/01 §9, E3).

When a step of one lead's lane goes `dead` (max attempts used, and replanning
has no other skill for it), the orchestrator adds a `human.decide` step to that
lane (skill `review`, so the meta-reviewer holds the lease and escalates it
through escalations.escalate(): one Escalation per lane, with the options and
every attempt that was tried). Nothing else in the run waits for it, and the
run ends `completed_pending_input` with the lane listed and its reason.

Options: "manual" (the human sends the email / fixes the CRM by hand) or
"skip". The answer is committed as the fact `handoff:<lane>` and the step is
verified like any review (review.decided). Lane outcomes then leave the dead
step (and the dependents that died with it) out of the sweep: an email step
handed off counts as handled by the human; a CRM step handed off makes the
lane `handed_off` (manual) or `skipped` (skip).
"""

from __future__ import annotations

from typing import Any

from . import ledger
from .config import RunConfig
from .orchestrator_lanes import group_lanes, live, new_step, next_skill
from .protocol import CRM_KINDS, EventType, ReviewOption, Skill, Step, StepKind, StepStatus

S = StepStatus
K = StepKind
EMAIL_KINDS = frozenset({K.EMAIL_DRAFT, K.EMAIL_SEND, K.REVIEW_APPROVAL})
WHAT = {
    K.EMAIL_DRAFT: "the follow-up email draft", K.EMAIL_SEND: "sending the follow-up email",
    K.REVIEW_APPROVAL: "the email approval", K.CRM_SEARCH_CONTACT: "the CRM lookup",
    K.CRM_CREATE_CONTACT: "creating the CRM contact", K.CRM_UPDATE_CONTACT: "updating the CRM contact",
    K.CRM_CREATE_TASK: "creating the follow-up task", K.REVIEW_AMBIGUITY: "the review",
}


def handoff_key(lane: str) -> str:
    return f"handoff:{lane}"


def is_handoff(step: Step) -> bool:
    return step.kind == K.HUMAN_DECIDE and bool(step.inputs.get("dead_step"))


def _superseded(step: Step, lane_steps: list[Step]) -> bool:
    return any(x.kind == step.kind and x.created_at > step.created_at and x.id != step.id
               and x.status != S.REPLANNED for x in lane_steps)


def root_dead(lane_steps: list[Step]) -> list[Step]:
    """Dead steps that failed on their own: not superseded by a newer step of
    the same kind (a replan) and not dead only because a dependency died."""
    by_id = {s.id: s for s in lane_steps}
    out = []
    for s in live(lane_steps):
        if s.status != S.DEAD or is_handoff(s) or _superseded(s, lane_steps):
            continue
        if any((d := by_id.get(dep)) is not None and d.status == S.DEAD for dep in s.depends_on):
            continue
        out.append(s)
    return out


def died_with(root: Step, lane_steps: list[Step]) -> set[str]:
    """root and the dead steps that (transitively) depend on it."""
    ids = {root.id}
    changed = True
    while changed:
        changed = False
        for s in lane_steps:
            if s.id not in ids and s.status == S.DEAD and any(d in ids for d in s.depends_on):
                ids.add(s.id)
                changed = True
    return ids


def handoffs(lane_steps: list[Step]) -> dict[str, Step]:
    """{dead step id: its (newest) human.decide handoff step}."""
    out: dict[str, Step] = {}
    for s in sorted(lane_steps, key=lambda x: x.created_at):
        if is_handoff(s) and s.status != S.REPLANNED:
            out[str(s.inputs["dead_step"])] = s
    return out


def covered(lane_steps: list[Step]) -> set[str]:
    """Ids the lane outcome leaves out: dead steps with a handoff (and the
    dependents that died with them) plus the handoff steps themselves."""
    hs = handoffs(lane_steps)
    by_id = {s.id: s for s in lane_steps}
    ids: set[str] = {h.id for h in hs.values()}
    for dead_id in hs:
        if dead_id in by_id:
            ids |= died_with(by_id[dead_id], lane_steps)
    return ids


def last_reason(step: Step) -> str:
    if step.verdict and step.verdict.reason:
        return step.verdict.reason
    for a in reversed(step.history):
        if a.verdict and a.verdict.reason:
            return a.verdict.reason
        if a.outcome:
            return a.outcome
    return f"{step.kind.value} gave up after {step.attempt} attempt(s)"


def tried(step: Step) -> list[str]:
    lines = []
    for a in step.history:
        line = f"attempt {a.attempt} by {a.worker or '?'}: {a.outcome or 'no outcome'}"
        if a.verdict and a.verdict.reason:
            line += f" ({a.verdict.reason[:200]})"
        lines.append(line)
    return lines or [f"{step.kind.value} went dead: {last_reason(step)}"]


def options_for(kind: StepKind) -> list[ReviewOption]:
    if kind in EMAIL_KINDS:
        return [ReviewOption(label="I'll send it manually", value="manual",
                             detail="the lane counts as handled by you; nothing is sent automatically"),
                ReviewOption(label="Skip the email for this lead", value="skip",
                             detail="no follow-up email; the CRM work stays as verified")]
    return [ReviewOption(label="I'll do it in the CRM by hand", value="manual",
                         detail="the lane is handed to you; the run stops working on it"),
            ReviewOption(label="Skip this lead", value="skip", detail="nothing more is done for this row")]


def handoff_step(run_id: str, lane: str, dead: Step, cfg: RunConfig, lead: dict[str, Any]) -> Step:
    name = lead.get("name") or lead.get("email") or lane
    what = WHAT.get(dead.kind, dead.kind.value)
    reason = last_reason(dead)
    attempts = len(dead.history) or dead.attempt
    opts = options_for(dead.kind)
    question = (f"{what[0].upper() + what[1:]} for {name} failed after {attempts} attempt(s): {reason[:240]}. "
                f"{' or '.join(o.label for o in opts)}?")
    return new_step(
        run_id, K.HUMAN_DECIDE, cfg, lane=lane, skill=Skill.REVIEW,
        title=f"{name}: {what} failed, needs you",
        inputs={"lane": lane, "reason": "dead_step", "dead_step": dead.id, "dead_kind": dead.kind.value,
                "question": question, "options": [o.model_dump() for o in opts], "tried": tried(dead),
                "last_error": reason, "lead": {k: lead.get(k) for k in ("name", "email", "company") if lead.get(k)},
                "threshold": cfg.review_auto_threshold},
        check="review.decided", args={"lane": lane, "decision_key": handoff_key(lane), "kind": "handoff"},
        idempotency_key=f"handoff:{lane}:{dead.id}",
    )


async def hand_off_dead_lanes(r, keys, run_id: str, cfg: RunConfig, *, actor: str) -> list[Step]:
    """Create a handoff step for every lane with an unhandled dead step (after
    replanning had its chance: a CRM step whose route has another skill is
    left to orchestrator_replan). Idempotent."""
    steps = await ledger.list_steps(r, keys, run_id)
    facts = await ledger.get_facts(r, keys, run_id)
    new: list[Step] = []
    for lane, lane_steps in group_lanes(steps).items():
        done = handoffs(lane_steps)
        for dead in root_dead(lane_steps):
            if dead.id in done:
                continue
            if dead.kind in CRM_KINDS and next_skill(dead.kind, dead.skill, cfg) is not None:
                continue  # replan moves the lane to the other skill first
            lead = facts.get(lane) if isinstance(facts.get(lane), dict) else {}
            if not lead:
                lead = next((s.inputs["lead"] for s in lane_steps if isinstance(s.inputs.get("lead"), dict)), {})
            new.append(handoff_step(run_id, lane, dead, cfg, lead))
            break  # one handoff per lane at a time
    if new:
        await ledger.create_steps(r, keys, new, actor=actor, event_type=EventType.PLAN_REVISED, payload={
            "reason": "lane step dead: handed to a human (the rest of the run goes on)",
            "lanes": [s.lane for s in new], "dead_steps": [s.inputs["dead_step"] for s in new]})
    return new
