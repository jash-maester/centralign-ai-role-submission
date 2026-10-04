"""Email lanes: draft -> one approval batch per run -> send (Track K; plans/02 C8,
C9, E1; playbook "Follow-up policy" + "Approval policy").

A run-level orchestrator stage (orchestrator_lanes.register_stage), called on
every progress round. Deterministic, no LLM; it only creates planned steps,
the orchestrator releases them when their dependencies commit.

1. Drafts. When a lane's contact and follow-up task are committed, the lane
   gets one `email.draft` step (skill email.draft, depends on the task), unless
   the playbook excludes it:
     - no email on the lead (phone-only rows never get this far anyway);
     - the contact has an **open deal** (an opportunity not Closed Won/Lost):
       the owner follows up personally; the task still exists. Known from the
       search (exact email match) or, for contacts matched through review /
       check-then-act, read once through the CRM REST API (read-only key);
     - skipped or unresolved rows (no committed contact: they never qualify).
   Exclusions are recorded on the run hash (field `email`) and as a
   plan.revised event, and the report / criteria read them from there.
2. Approval. When drafts are committed and no upstream work that could still
   produce a draft is moving (CRM steps, drafts, reviews a live reviewer will
   take), ONE `review.approval` step (skill review, lane None) is created for
   all drafts not yet covered (normally exactly one per run; a lane resolved
   later by a human gets a second, smaller batch). The meta-reviewer decides
   it per email (approval.py documents the facts): emails that clear the
   policy are approved at once; each one that does not gets its own
   `review.approval` step for its lane (inputs.prejudged: the batch's reason
   and score, no second judge call), which escalates to a human (Track N).
3. Sends. For every lane whose committed approval fact says approve, one
   `email.send` step (skill email.send, depends on the draft and the approval
   step), postcondition `email.sent` (Mailpit, exactly once).
"""

from __future__ import annotations

import json
import logging
import re
from typing import Any

from . import approval, ledger
from .checks import email_lanes  # noqa: F401 - facts + the email.sent run evaluator
from .checks.email import MAX_WORDS, template_subject
from .config import RunConfig
from .events import append_event
from .orchestrator_lanes import (
    CONTACT_KINDS,
    LANE_PREFIX,
    group_lanes,
    latest,
    live,
    lookup_for,
    new_step,
    register_stage,
)
from .protocol import CRM_KINDS, Event, EventType, Run, Step, StepKind, StepStatus

log = logging.getLogger("ledger.orchestrator.email")
S = StepStatus
K = StepKind
STATE_FIELD = "email"
FROM_DOMAIN = "ledger-demo.test"  # the seeded CRM users' mail domain
UPSTREAM = frozenset({K.FILE_PARSE, *CRM_KINDS, K.EMAIL_DRAFT, K.REVIEW_AMBIGUITY, K.HUMAN_DECIDE})
_OWNER_RE = re.compile(r"`([A-Za-z0-9_.-]+)`\s*\(([^)]+)\)")

_STATE: dict[str, dict[str, Any]] = {}  # run_id -> {"excluded": {lane: reason}}


def exclusions(run_id: str) -> dict[str, str]:
    """Lanes the playbook keeps from getting an email ({lane: reason}); as of the
    orchestrator's last pass over the run."""
    return dict((_STATE.get(run_id) or {}).get("excluded") or {})


async def load_state(r, keys, run_id: str) -> dict[str, Any]:
    raw = await r.hget(keys.run(run_id), STATE_FIELD)
    try:
        state = json.loads(raw) if raw else {}
    except ValueError:
        state = {}
    state.setdefault("excluded", {})
    _STATE[run_id] = state
    return state


async def _save_state(r, keys, run_id: str, state: dict[str, Any]) -> None:
    _STATE[run_id] = state
    await r.hset(keys.run(run_id), STATE_FIELD, json.dumps(state, sort_keys=True))


# ---------------------------------------------------------------------------
# helpers (overridable on the orchestrator instance for tests:
#   orch.open_deal_lookup = async fn(contact_id) -> bool | None
#   orch.owner_directory  = async fn(owner) -> {"name", "email"})
# ---------------------------------------------------------------------------


async def _crm_reader(orch):
    if getattr(orch, "_crm", None) is None:
        try:
            from .crm_api import reader_from_redis

            orch._crm = await reader_from_redis(orch.r)
        except Exception as exc:  # noqa: BLE001
            log.warning("no CRM reader for the email stage: %s", exc)
            return None
    return orch._crm


async def open_deal(orch, contact_id: str) -> bool | None:
    """True / False, or None when it cannot be known right now (CRM down)."""
    fn = getattr(orch, "open_deal_lookup", None)
    if fn is not None:
        return await fn(contact_id)
    reader = await _crm_reader(orch)
    if reader is None:
        return None
    try:
        return bool(await reader.open_opportunities_for_contact(contact_id))
    except Exception as exc:  # noqa: BLE001
        log.warning("open-deal lookup for %s failed: %s", contact_id, exc)
        return None


async def owner_info(orch, run: Run, owner: str | None) -> dict[str, str | None]:
    if not owner:
        return {"name": None, "email": None}
    fn = getattr(orch, "owner_directory", None)
    if fn is not None:
        return await fn(owner)
    name = None
    try:
        name = dict(_OWNER_RE.findall(orch._playbook(run).text)).get(owner)
    except Exception:  # noqa: BLE001
        pass
    if name is None:
        reader = await _crm_reader(orch)
        if reader is not None:
            try:
                name = ((await reader.users()).get(owner) or {}).get("name")
            except Exception:  # noqa: BLE001
                name = None
    return {"name": name, "email": f"{owner}@{FROM_DOMAIN}"}


def _first_name(lead: dict[str, Any]) -> str | None:
    if lead.get("first_name"):
        return str(lead["first_name"])
    name = (lead.get("name") or "").split()
    return name[0] if name else None


def _out(step: Step) -> dict[str, Any]:
    out = dict(step.claim.data) if step.claim else {}
    if step.verdict:
        out.update({k: v for k, v in step.verdict.observed.items() if v not in (None, "")})
    return out


# ---------------------------------------------------------------------------
# 1. drafts
# ---------------------------------------------------------------------------


async def _lane_open_deal(orch, lane: str, lane_steps: list[Step], facts: dict[str, Any],
                          contact: Step) -> bool | None:
    out = _out(contact)
    contact_id = out.get("contact_id")
    if contact.kind == K.CRM_CREATE_CONTACT and out.get("action") != "exists":
        return False  # a contact this run created has no opportunities
    _, lookup = lookup_for(lane_steps, facts)
    if lookup.get("result") == "matched" and lookup.get("contact_id") == contact_id and \
            lookup.get("open_deal") is not None:
        return bool(lookup["open_deal"])
    if facts.get(f"{lane}.open_deal") is not None and facts.get(f"{lane}.contact_id") == contact_id:
        return bool(facts[f"{lane}.open_deal"])
    if not contact_id:
        return None
    return await open_deal(orch, contact_id)


async def draft_step_for(orch, run: Run, lane: str, lane_steps: list[Step], facts: dict[str, Any],
                         cfg: RunConfig, contact: Step, task: Step) -> Step | str | None:
    """The lane's email.draft step, an exclusion reason (str), or None (not decidable yet)."""
    lead = facts.get(lane) if isinstance(facts.get(lane), dict) else {}
    email = (lead.get("email") or "").strip().lower()
    if not email:
        return "no_email"
    deal = await _lane_open_deal(orch, lane, lane_steps, facts, contact)
    if deal is None:
        return None
    if deal:
        return "open_deal"
    out = _out(contact)
    owner = out.get("owner") or contact.postcondition.expect.get("owner") or facts.get(f"{lane}.owner")
    who = await owner_info(orch, run, owner)
    ev = await orch._event(run)
    first = _first_name(lead)
    name = lead.get("name") or email
    inputs: dict[str, Any] = {
        "lead": f"fact:{lane}" if facts.get(lane) is not None else lead, "lane": lane, "to": email,
        "first_name": first, "event_name": ev.name, "event_date": ev.date, "owner": owner,
        "owner_name": who.get("name"), "from_email": who.get("email"), "playbook": run.playbook,
        "idempotency_key": f"email.draft:{email}:{ev.name}",
    }
    args: dict[str, Any] = {"recipient": email, "first_name": first, "event_name": ev.name, "max_words": MAX_WORDS}
    if who.get("name"):
        args["owner_name"] = who["name"]
    expect = {"subject": template_subject(ev.name, first)} if first else {}
    return new_step(
        run.id, K.EMAIL_DRAFT, cfg, lane=lane, title=f"Draft follow-up email to {name}", inputs=inputs,
        check="email.draft_valid", args=args, expect=expect, depends_on=[task.id],
        idempotency_key=inputs["idempotency_key"],
    )


async def _draft_stage(orch, run: Run, steps: list[Step], facts: dict[str, Any], cfg: RunConfig,
                       state: dict[str, Any]) -> tuple[list[Step], dict[str, str]]:
    new: list[Step] = []
    excluded: dict[str, str] = {}
    for lane, lane_steps in group_lanes(steps).items():
        if lane in state["excluded"]:
            continue
        cur = live(lane_steps)
        if any(s.kind in (K.EMAIL_DRAFT, K.EMAIL_SEND) for s in lane_steps):
            continue  # one draft per lane; a dead draft fails the lane, it is not redrafted here
        contact = latest(cur, CONTACT_KINDS, status=S.COMMITTED)
        task = latest(cur, K.CRM_CREATE_TASK, status=S.COMMITTED)
        if contact is None or task is None:
            continue
        got = await draft_step_for(orch, run, lane, lane_steps, facts, cfg, contact, task)
        if isinstance(got, Step):
            new.append(got)
        elif isinstance(got, str):
            excluded[lane] = got
    return new, excluded


# ---------------------------------------------------------------------------
# 2. approval batch
# ---------------------------------------------------------------------------


def covered_lanes(steps: list[Step]) -> set[str]:
    return {it.get("lane") for s in steps if s.kind == K.REVIEW_APPROVAL and s.status != S.REPLANNED
            for it in (s.inputs.get("items") or []) if isinstance(it, dict)}


def approval_step(run: Run, drafts: list[Step], cfg: RunConfig, event_name: str, batch_no: int,
                  excluded: dict[str, str]) -> Step:
    items = []
    for d in sorted(drafts, key=lambda s: int(s.lane[len(LANE_PREFIX):]) if s.lane else 0):
        data = _out(d)
        items.append({"lane": d.lane, "draft_step": d.id, "draft": f"fact:{approval.draft_key(d.lane)}",
                      "to": data.get("to") or d.inputs.get("to"), "subject": data.get("subject"),
                      "owner": d.inputs.get("owner"), "approval_key": approval.approval_key(d.lane)})
    lanes = [it["lane"] for it in items]
    n = len(items)
    inputs = {
        "reason": "external_email", "kind": "email_approval", "batch": batch_no, "items": items,
        "decision_key": approval.BATCH_KEY, "threshold": cfg.approval_auto_threshold,
        "always_ask_human": cfg.always_ask_human_email,
        "question": f"Approve {n} follow-up email{'s' if n != 1 else ''} for {event_name}?",
        "options": [{"label": f"Approve all {n}", "value": "approve", "detail": "send each draft unchanged"},
                    {"label": "Reject", "value": "reject", "detail": "send nothing; the owners follow up"}],
        "policy": ("Approve a draft only when its deterministic checks passed (right recipient, merge fields, "
                   "no placeholders, <= 120 words) and the tone judge scores it >= approval_auto_threshold with "
                   "no policy flags (pricing, promises, attachments, links); otherwise that email alone escalates. "
                   "Judge the whole batch in one judge.judge_drafts() call. Commit approval:<lane> per email and "
                   f"{approval.BATCH_KEY} (approval.commit_decisions)."),
        "context": {"event": event_name, "not_emailed": excluded},
    }
    return new_step(
        run.id, K.REVIEW_APPROVAL, cfg, lane=None, title=f"Approve {n} follow-up email{'s' if n != 1 else ''}",
        inputs=inputs, check="review.decided",
        args={"decision_key": approval.BATCH_KEY, "lanes": lanes, "kind": "approval"},
        depends_on=[d.id for d in drafts], idempotency_key=f"review.approval:{run.id}:{batch_no}",
    )


async def _approval_stage(orch, run: Run, steps: list[Step], cfg: RunConfig, state: dict[str, Any]) -> list[Step]:
    covered = covered_lanes(steps)
    ready = [s for s in steps if s.kind == K.EMAIL_DRAFT and s.status == S.COMMITTED and s.lane not in covered]
    if not ready:
        return []
    _waiting, moving = await orch.waiting_and_moving(steps)
    if any(s.id in moving and s.kind in UPSTREAM for s in steps):
        return []  # more drafts may still come: batch them all together
    ev = await orch._event(run)
    batch_no = sum(1 for s in steps if s.kind == K.REVIEW_APPROVAL) + 1
    return [approval_step(run, ready, cfg, ev.name, batch_no, state["excluded"])]


# ---------------------------------------------------------------------------
# 3. sends
# ---------------------------------------------------------------------------


def send_step(run: Run, draft: Step, appr: Step, rec: dict[str, Any], cfg: RunConfig) -> Step:
    data = _out(draft)
    lane = draft.lane or ""
    to, subject = data.get("to") or draft.inputs.get("to"), data.get("subject")
    inputs = {"lane": lane, "draft": f"fact:{approval.draft_key(lane)}", "approval_step": appr.id,
              "approval_key": approval.approval_key(lane), "batch_key": approval.BATCH_KEY,
              "to": to, "subject": subject, "idempotency_key": f"email.send:{run.id}:{lane}:{draft.id}"}
    return new_step(
        run.id, K.EMAIL_SEND, cfg, lane=lane, title=f"Send follow-up email to {to}", inputs=inputs,
        check="email.sent", args={"to": to, "subject": subject}, expect={"subject": subject, "exactly_once": True},
        depends_on=[draft.id, appr.id], side_effect=True, idempotency_key=inputs["idempotency_key"],
    )


def _send_stage(run: Run, steps: list[Step], facts: dict[str, Any], cfg: RunConfig) -> list[Step]:
    new: list[Step] = []
    sending = {s.lane for s in steps if s.kind == K.EMAIL_SEND}
    drafts = {s.id: s for s in steps if s.kind == K.EMAIL_DRAFT and s.status == S.COMMITTED}
    # Per lane, the newest approval step covering it decides (Track N: an email the
    # batch escalated has its own, newer review.approval step; the send waits on it).
    latest_for: dict[str, tuple[Step, dict[str, Any]]] = {}
    for appr in sorted(steps, key=lambda s: s.created_at):
        if appr.kind != K.REVIEW_APPROVAL or appr.status in (S.REPLANNED, S.DEAD):
            continue
        for it in appr.inputs.get("items") or []:
            if isinstance(it, dict) and it.get("lane"):
                latest_for[it["lane"]] = (appr, it)
    for lane, (appr, it) in sorted(latest_for.items()):
        draft = drafts.get(it.get("draft_step"))
        if lane in sending or draft is None:
            continue
        rec = approval.approval_for(facts, lane, approval_step=appr.id)
        if approval.is_approved(rec) and not approval.mismatch(rec, {**_out(draft), "draft_step": draft.id}):
            new.append(send_step(run, draft, appr, rec, cfg))
            sending.add(lane)
    return new


# ---------------------------------------------------------------------------
# 2b. emails the batch escalated: one review.approval step each (Track N)
# ---------------------------------------------------------------------------


def escalated_lanes(appr: Step, facts: dict[str, Any]) -> dict[str, dict[str, Any]]:
    """{lane: why} for the emails a committed approval batch did not auto-approve
    (the meta-reviewer's claim, committed as `step:<id>`)."""
    if appr.status != S.COMMITTED:
        return {}
    out = facts.get(f"step:{appr.id}")
    if not isinstance(out, dict):
        out = _out(appr)
    esc = out.get("escalated")
    return {k: v for k, v in esc.items() if isinstance(v, dict)} if isinstance(esc, dict) else {}


def lane_approval_step(run: Run, appr: Step, item: dict[str, Any], why: dict[str, Any], cfg: RunConfig,
                       name: str | None) -> Step:
    lane = item["lane"]
    to = item.get("to") or why.get("to")
    score = why.get("score")
    judged = f"the judge scored it {float(score):.2f}, threshold {cfg.approval_auto_threshold:.2f}" \
        if score is not None else "it has no judge score"
    inputs = {
        "reason": "external_email", "kind": "email_approval", "items": [item],
        "decision_key": approval.approval_key(lane), "threshold": cfg.approval_auto_threshold,
        "always_ask_human": cfg.always_ask_human_email, "parent_step": appr.id,
        "prejudged": {**why, "batch_step": appr.id},
        "question": f"Send the follow-up email to {name or to} ({to})? Not auto-approved: "
                    f"{why.get('reason') or 'below threshold'}; {judged}.",
        "options": [{"label": "Approve and send", "value": "approve", "detail": "send this draft unchanged"},
                    {"label": "Reject (do not send)", "value": "reject", "detail": "the owner follows up"}],
    }
    return new_step(
        run.id, K.REVIEW_APPROVAL, cfg, lane=lane, title=f"Approve the follow-up email to {name or to}",
        inputs=inputs, check="review.decided",
        args={"decision_key": approval.approval_key(lane), "lanes": [lane], "kind": "approval", "lane": lane},
        depends_on=[appr.id], idempotency_key=f"review.approval:{run.id}:{lane}:{appr.id}",
    )


def _lane_approval_stage(run: Run, steps: list[Step], facts: dict[str, Any], cfg: RunConfig) -> list[Step]:
    have = {(s.inputs.get("parent_step"), s.lane) for s in steps if s.kind == K.REVIEW_APPROVAL and s.lane}
    new: list[Step] = []
    for appr in steps:
        if appr.kind != K.REVIEW_APPROVAL or appr.lane:
            continue
        for lane, why in sorted(escalated_lanes(appr, facts).items()):
            item = next((it for it in appr.inputs.get("items") or [] if isinstance(it, dict)
                         and it.get("lane") == lane), None)
            if item is None or (appr.id, lane) in have:
                continue
            lead = facts.get(lane) if isinstance(facts.get(lane), dict) else {}
            new.append(lane_approval_step(run, appr, item, why, cfg, lead.get("name")))
    return new


# ---------------------------------------------------------------------------
# the stage
# ---------------------------------------------------------------------------


async def email_stage(orch, run: Run, steps: list[Step], facts: dict[str, Any], cfg: RunConfig) -> bool:
    if not any(s.lane for s in steps):
        return False
    state = await load_state(orch.r, orch.keys, run.id)
    new, excluded = await _draft_stage(orch, run, steps, facts, cfg, state)
    if excluded:
        state["excluded"].update(excluded)
        await _save_state(orch.r, orch.keys, run.id, state)
        await append_event(orch.r, orch.keys, Event(
            run_id=run.id, actor=orch.agent_id, type=EventType.PLAN_REVISED,
            payload={"reason": "email: not drafted per playbook (Follow-up policy)", "excluded": excluded,
                     "steps": []}))
    if new:
        await ledger.create_steps(orch.r, orch.keys, new, actor=orch.agent_id, event_type=EventType.PLAN_REVISED,
                                  payload={"reason": "email: draft follow-ups", "lanes": [s.lane for s in new]})
        return True  # batch only once the drafts exist and the CRM work has settled
    sends = _send_stage(run, steps, facts, cfg)
    if sends:
        await ledger.create_steps(orch.r, orch.keys, sends, actor=orch.agent_id, event_type=EventType.PLAN_REVISED,
                                  payload={"reason": "email: approved, send", "lanes": [s.lane for s in sends]})
        return True
    singles = _lane_approval_stage(run, steps, facts, cfg)
    if singles:
        await ledger.create_steps(orch.r, orch.keys, singles, actor=orch.agent_id, event_type=EventType.PLAN_REVISED,
                                  payload={"reason": "email: not auto-approved, ask a human per email",
                                           "lanes": [s.lane for s in singles]})
        return True
    batch = await _approval_stage(orch, run, steps, cfg, state)
    if batch:
        await ledger.create_steps(orch.r, orch.keys, batch, actor=orch.agent_id, event_type=EventType.PLAN_REVISED,
                                  payload={"reason": "email: one approval for the batch",
                                           "lanes": batch[0].postcondition.args.get("lanes")})
        return True
    return bool(excluded)


register_stage(email_stage)
