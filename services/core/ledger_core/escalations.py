"""Human escalations: store, list, answer (plans/01 §4 Review decision, §9; E3-E5).

    esc = await escalate(r, keys, step, fence=..., actor="meta-reviewer", question=..., options=[...],
                         tried=[...], confidence=0.52, threshold=0.80)
    await list_escalations(r, keys, run_id=None, status="open")
    await answer(r, keys, esc.id, "link_account:a-inc", by="human", save_as_rule=True)

Storage (keys.py): `escalation:{id}` Hash {"json": Escalation}, `escalations:open`
Set of open ids. The step's inputs carry `escalation_id` while it waits.

escalate():  leased -> review_required -> input_required in ONE transaction
             (review.requested + input.requested events), then the record,
             the open-set entry, a task envelope on queue:human and
             review.escalated. Only that lane waits; every other lane goes on.
answer():    validate the answer against the escalation's options, commit the
             decision fact (`review:<lane>`, or the step's decision_key) with
             decided_by=human, mark the escalation answered, emit
             input.answered and move the step input_required -> ready
             (requeued on queue:review with inputs.human_decision). The
             meta-reviewer then claims that decision without an LLM call and
             the verifier commits the step (review.decided), which releases
             the lane in the orchestrator. save_as_rule appends a rule to the
             run's playbook (playbook.append_rule) so the same case resolves
             without a human next time.
"""

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any

import redis.asyncio as aioredis

from . import bus, ledger, playbook
from .events import append_event, encode_event
from .keys import Keys
from .protocol import (
    Escalation,
    Event,
    EventType,
    ReviewDecision,
    ReviewOption,
    Skill,
    Step,
    StepKind,
    StepStatus,
    now_ms,
)

S = StepStatus
RULES_SECTION = "Escalation rules"
# Machine-readable tail of a saved rule: [rule reason=<r> company=<c> domain=<d> -> <option>]
RULE_TAG = re.compile(r"\[rule reason=(?P<reason>\S+) company=(?P<company>\S*) domain=(?P<domain>\S*) -> (?P<answer>[^\]\s]+)\]")


class EscalationError(ValueError):
    """Bad request: unknown escalation, already answered, answer not an option."""


def decision_key(step: Step) -> str:
    """Fact key a review step's decision is committed under."""
    key = (step.postcondition.args or {}).get("decision_key")
    if key:
        return str(key)
    prefix = "approval" if step.kind == StepKind.REVIEW_APPROVAL else "review"
    return f"{prefix}:{step.lane or step.id}"


def split_option(value: str) -> tuple[str, str | None]:
    """"match_existing:c1" -> ("match_existing", "c1"); "skip" -> ("skip", None)."""
    dec, sep, val = str(value).partition(":")
    return dec, (val if sep else None)


def decision_record(d: ReviewDecision, *, option: str | None = None, label: str | None = None,
                    extra: dict[str, Any] | None = None) -> dict[str, Any]:
    """The JSON committed as the decision fact and claimed by the meta-reviewer
    (keys the orchestrator, report and GUI read)."""
    out = d.model_dump(mode="json")
    out["option"] = option or (f"{d.decision}:{d.value}" if d.value else d.decision)
    if label:
        out["label"] = label
    out.update(extra or {})
    return out


async def get(r: aioredis.Redis, keys: Keys, esc_id: str) -> Escalation | None:
    raw = await r.hget(keys.escalation(esc_id), "json")
    return Escalation.model_validate_json(raw) if raw else None


async def _save(r: aioredis.Redis, keys: Keys, esc: Escalation, pipe: Any = None) -> None:
    target = pipe if pipe is not None else r
    await target.hset(keys.escalation(esc.id), mapping={"json": esc.model_dump_json(), "run_id": esc.run_id,
                                                         "step_id": esc.step_id, "status": esc.status})


async def list_escalations(r: aioredis.Redis, keys: Keys, *, run_id: str | None = None,
                           status: str | None = "open") -> list[Escalation]:
    """Escalations, oldest first. status "open" (default) reads the open set;
    None / "all" / "answered" scan escalation:* (fine at demo scale)."""
    if status == "open":
        ids = sorted(await r.smembers(keys.escalations_open))
    else:
        ids = []
        async for k in r.scan_iter(match=keys.escalation("*"), count=500):
            ids.append(k.rsplit(":", 1)[-1])
    out = []
    for esc_id in ids:
        esc = await get(r, keys, esc_id)
        if esc is None:
            continue
        if run_id and esc.run_id != run_id:
            continue
        if status not in (None, "all", "open") and esc.status != status:
            continue
        out.append(esc)
    return sorted(out, key=lambda e: (e.created_at, e.id))


async def for_step(r: aioredis.Redis, keys: Keys, step_id: str, *, open_only: bool = True) -> Escalation | None:
    step = await ledger.get_step(r, keys, step_id)
    esc_id = (step.inputs or {}).get("escalation_id") if step else None
    if esc_id:
        esc = await get(r, keys, esc_id)
        if esc and (esc.status == "open" or not open_only):
            return esc
    for esc in await list_escalations(r, keys, status="open" if open_only else "all"):
        if esc.step_id == step_id:
            return esc
    return None


async def escalate(
    r: aioredis.Redis, keys: Keys, step: Step, *, fence: int, actor: str, question: str,
    options: list[ReviewOption], tried: list[str], confidence: float, threshold: float,
    context: str = "", decision: dict[str, Any] | None = None,
) -> Escalation:
    """The meta-reviewer, holding the lease (fence), hands the step to a human."""
    esc = Escalation(run_id=step.run_id, step_id=step.id, lane=step.lane, question=question, context=context,
                     options=options, tried=tried, confidence=round(float(confidence), 4),
                     threshold=float(threshold))
    inputs = {**step.inputs, "escalation_id": esc.id}
    chain = [
        (S.REVIEW_REQUIRED, ledger._kw(reason=f"confidence {esc.confidence:.2f} < threshold {esc.threshold:.2f}",
                                       payload={"confidence": esc.confidence, "threshold": esc.threshold})),
        (S.INPUT_REQUIRED, ledger._kw(payload={"escalation_id": esc.id, "lane": step.lane, "question": question},
                                      updates={"inputs": inputs})),
    ]
    # One MULTI/EXEC for both moves (fenced: only the current lease holder may escalate).
    await ledger._mutate(r, keys, step.id, chain, actor=actor, actor_role="meta_reviewer", fence=fence)
    payload = {
        "escalation_id": esc.id, "lane": step.lane, "kind": "approval" if step.kind == StepKind.REVIEW_APPROVAL
        else "ambiguity", "title": step.title, "question": question, "confidence": esc.confidence,
        "threshold": esc.threshold, "tried": tried, "evidence": tried,
        "options": [o.model_dump() for o in options], **(decision or {}),
    }
    payload["confidence"], payload["threshold"] = esc.confidence, esc.threshold
    pipe = r.pipeline(transaction=True)
    await _save(r, keys, esc, pipe)
    pipe.sadd(keys.escalations_open, esc.id)
    env = bus.task_envelope(step.id, step.run_id, Skill.HUMAN, sender=actor)
    env.state = "input-required"
    pipe.xadd(keys.queue(Skill.HUMAN.value), bus.envelope_fields(env))
    pipe.xadd(keys.events, encode_event(Event(run_id=step.run_id, step_id=step.id, actor=actor,
                                               type=EventType.REVIEW_ESCALATED, payload=payload)))
    await pipe.execute()
    return esc


def _option(esc: Escalation, answer: str) -> ReviewOption | None:
    want = answer.strip()
    for o in esc.options:
        if o.value == want or o.label.strip().lower() == want.lower():
            return o
    return None


def rule_text(esc: Escalation, inputs: dict[str, Any], option: ReviewOption) -> str:
    """Human-readable rule + a machine-readable tag the meta-reviewer matches next time.
    `inputs` are the review step's inputs with fact refs resolved."""
    lead = inputs.get("lead") if isinstance(inputs.get("lead"), dict) else {}
    reason = str(inputs.get("reason") or "review")
    company = re.sub(r"\s+", "_", str(lead.get("company") or "").strip().lower())
    domain = str(lead.get("email") or "").rpartition("@")[2].strip().lower()
    who = lead.get("company") or lead.get("name") or esc.lane or "this case"
    human = f"For {who!r} ({reason.replace('_', ' ')}{', domain ' + domain if domain else ''}): {option.label}."
    return f"{human} [rule reason={reason} company={company} domain={domain} -> {option.value}]"


def saved_rule_for(pb_text: str, *, reason: str, lead: dict[str, Any]) -> tuple[str, str] | None:
    """(answer, rule line) of a saved playbook rule that matches this case, else None.
    A rule matches when the reason is equal and its company (if any) and its
    domain (if any) equal the lead's."""
    company = re.sub(r"\s+", "_", str(lead.get("company") or "").strip().lower())
    domain = str(lead.get("email") or "").rpartition("@")[2].strip().lower()
    for line in pb_text.splitlines():
        m = RULE_TAG.search(line)
        if not m or m["reason"] != reason:
            continue
        if m["company"] and m["company"] != company:
            continue
        if m["domain"] and m["domain"] != domain:
            continue
        if not (m["company"] or m["domain"]):
            continue
        return m["answer"], line.strip().lstrip("- ").strip()
    return None


async def answer(
    r: aioredis.Redis, keys: Keys, esc_id: str, answer_value: str, *, by: str = "human",
    save_as_rule: bool = False, note: str | None = None, playbook_dir: str | Path | None = None,
) -> dict[str, Any]:
    """Answer an open escalation (API POST /escalations/{id}, CLI approvals answer).
    Returns {escalation, decision, fact_key, step_status, rule}."""
    esc = await get(r, keys, esc_id)
    if esc is None:
        raise EscalationError(f"escalation {esc_id} not found")
    if esc.status != "open":
        raise EscalationError(f"escalation {esc_id} is already answered ({esc.answer})")
    option = _option(esc, answer_value)
    if option is None:
        raise EscalationError(f"answer {answer_value!r} is not one of the options: "
                              + ", ".join(o.value for o in esc.options))
    step = await ledger.get_step(r, keys, esc.step_id)
    if step is None:
        raise EscalationError(f"step {esc.step_id} of escalation {esc_id} not found")
    dec, val = split_option(option.value)
    actor = f"human:{by}" if by and by != "human" else "human"
    decision = ReviewDecision(
        decision=dec, value=val, confidence=1.0, threshold=esc.threshold,
        evidence=[f"human answer: {option.label}"] + ([f"note: {note}"] if note else []) + list(esc.tried),
        options=esc.options, decided_by="human", model=None)
    record = decision_record(decision, option=option.value, label=option.label,
                             extra={"escalation_id": esc.id, "answered_by": by, "reason": note or None})
    key = decision_key(step)

    rule = None
    if save_as_rule:
        run = await ledger.get_run(r, keys, esc.run_id)
        resolved = await ledger.resolve_inputs(r, keys, step, strict=False)
        text = rule_text(esc, resolved, option)
        pb = playbook.append_rule(RULES_SECTION, text, name=(run.playbook if run else playbook.DEFAULT_PLAYBOOK),
                                  playbook_dir=playbook_dir, source=esc.id)
        rule = {"section": RULES_SECTION, "text": text, "playbook": pb.name, "version": pb.version,
                "hash": pb.content_hash}

    await ledger.commit_fact(r, keys, esc.run_id, key, record, source_step=step.id, actor=actor)
    esc.status, esc.answer, esc.save_as_rule = "answered", option.value, bool(save_as_rule)
    pipe = r.pipeline(transaction=True)
    await _save(r, keys, esc, pipe)
    pipe.hset(keys.escalation(esc.id), mapping={"answered_at": str(now_ms()), "answered_by": by})
    pipe.srem(keys.escalations_open, esc.id)
    await pipe.execute()
    await append_event(r, keys, Event(run_id=esc.run_id, step_id=step.id, actor=actor, type=EventType.INPUT_ANSWERED,
                                      payload={"escalation_id": esc.id, "lane": esc.lane, "answer": option.value,
                                               "label": option.label, "value": val, "decision": dec,
                                               "fact": key, "save_as_rule": bool(save_as_rule), "note": note}))
    if rule:
        await append_event(r, keys, Event(run_id=esc.run_id, step_id=step.id, actor=actor,
                                          type=EventType.CONFIG_UPDATED,
                                          payload={"scope": "playbook", "action": "rule_added", **rule}))
    status = step.status
    if step.status == S.INPUT_REQUIRED:
        inputs = {**step.inputs, "human_decision": record}
        step = await ledger.transition(r, keys, step.id, S.READY, actor=actor, actor_role="human",
                                       reason="answered", payload={"escalation_id": esc.id, "answer": option.value},
                                       inputs=inputs)
        status = step.status
    return {"escalation": json.loads(esc.model_dump_json()), "decision": record, "fact_key": key,
            "step_status": status.value, "rule": rule}
