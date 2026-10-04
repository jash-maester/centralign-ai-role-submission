"""Facts and run-level criteria for the email lanes (Track K; plans/01 §8, §9).

Fact extractors (committed by the verifier with the step):

  email.draft   lead:<n>.draft   {lane, to, subject, body, owner, owner_name, from_email,
                                  first_name, event_name, word_count, draft_step, judge?}
  email.send    lead:<n>.email   {to, subject, message_id, mailpit_id, created, action, dry_run}

The draft fact is what the approval batch is about and what the mailer sends,
unchanged. The judge's per-draft score (when the verifier ran it) rides along
for the meta-reviewer; the approval itself still re-judges the batch in one call.

Run-level evaluator for `email.sent` (overrides the generic kind sweep from
checks/run.py): every lane that should get an email (done, has an email, not
excluded by the playbook: open deal / no email / skipped) must have a
committed email.send; a draft the approval rejected counts as handled (the
playbook only allows sending with an approval). Exclusions come from
orchestrator_email (run hash field `email`).
"""

from __future__ import annotations

from typing import Any

from ..protocol import Claim, Criterion, Step, StepKind, StepStatus
from ..postconditions import CheckContext, CheckResult
from ..verifier import register_facts
from . import run as run_checks  # noqa: F401 - registered first, so this module's evaluators win
from .run import FAILED, PENDING, VERIFIED, CriterionResult, register_criterion

DRAFT_FIELDS = ("lane", "to", "subject", "body", "owner", "owner_name", "from_email", "first_name", "event_name",
                "word_count")


def _lane(step: Step) -> str:
    return step.lane if step.lane else f"step:{step.id}"


@register_facts(StepKind.EMAIL_DRAFT)
def draft_facts(step: Step, claim: Claim, result: CheckResult) -> dict[str, Any]:
    data, obs = claim.data or {}, result.observed or {}
    draft = {k: data.get(k) for k in DRAFT_FIELDS if data.get(k) is not None}
    draft["to"] = obs.get("recipient") or draft.get("to")
    draft["draft_step"] = step.id
    if isinstance(obs.get("judge"), dict):
        draft["judge"] = obs["judge"]
    if obs.get("flags"):
        draft["flags"] = obs["flags"]
    return {f"{_lane(step)}.draft": draft}


@register_facts(StepKind.EMAIL_SEND)
def send_facts(step: Step, claim: Claim, result: CheckResult) -> dict[str, Any]:
    data, obs = claim.data or {}, result.observed or {}
    rec = {"to": obs.get("to") or data.get("to"), "subject": obs.get("subject") or data.get("subject"),
           "message_id": data.get("message_id"), "mailpit_id": obs.get("message_id") or data.get("mailpit_id"),
           "created": obs.get("created"), "action": data.get("action"), "dry_run": bool(obs.get("dry_run"))}
    return {f"{_lane(step)}.email": {k: v for k, v in rec.items() if v is not None}}


def _exclusions(ctx: CheckContext) -> dict[str, str]:
    from .. import orchestrator_email

    return orchestrator_email.exclusions(ctx.run_id)


@register_criterion("email.sent")
async def _email_sent(crit: Criterion, lanes: list[dict[str, Any]], ctx: CheckContext) -> CriterionResult:
    excluded = _exclusions(ctx)
    sent, failed, pending, held, skipped, by_human = [], [], [], [], [], []
    for x in lanes:
        lane, kinds = x["lane"], x.get("kinds") or {}
        if x.get("status") in ("skipped", "handed_off") or not x.get("email") or lane in excluded:
            skipped.append(f"{lane} ({excluded.get(lane) or x.get('reason') or 'no email'})")
            continue
        hand = x.get("handoff") or {}
        if hand.get("email"):  # Track N: an email step went dead and a human was asked
            if not hand.get("decision"):
                pending.append(lane)
            else:
                by_human.append(f"{lane} ({'sent manually' if hand['decision'] == 'manual' else 'skipped'} by you "
                                f"after {hand.get('kind')} failed)")
            continue
        send, draft = kinds.get(StepKind.EMAIL_SEND.value), kinds.get(StepKind.EMAIL_DRAFT.value)
        if send == StepStatus.COMMITTED.value:
            sent.append(lane)
        elif StepStatus.DEAD.value in (send, draft) or x.get("status") == "failed":
            failed.append(lane)
        elif draft == StepStatus.COMMITTED.value and ctx.facts:
            from ..approval import REJECT, approval_for

            rec = approval_for(ctx.facts, lane)
            (held if rec and rec.get("decision") == REJECT else pending).append(lane)
        else:
            pending.append(lane)
    obs = {"sent": sent, "failed": failed, "pending": pending, "rejected_at_approval": held, "not_emailed": skipped,
           "handled_by_human": by_human}
    if failed:
        return CriterionResult(crit.id, FAILED, "email failed for " + ", ".join(failed), obs)
    if pending:
        return CriterionResult(crit.id, PENDING, f"{len(sent)} sent with approval; waiting on " + ", ".join(pending),
                               obs)
    tail = f"; not emailed per playbook: {', '.join(skipped)}" if skipped else ""
    held_s = f"; held at approval: {', '.join(held)}" if held else ""
    human_s = f"; handled by you: {', '.join(by_human)}" if by_human else ""
    return CriterionResult(crit.id, VERIFIED, f"{len(sent)} email(s) sent, each with a committed approval"
                           + held_s + human_s + tail, obs)
