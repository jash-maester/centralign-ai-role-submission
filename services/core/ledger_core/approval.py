"""Email approval: the batch contract between drafter, meta-reviewer and mailer
(plans/01 §9, plans/02 C9 + E1; Track K).

Facts (all committed through the ledger, never written by a worker):

    lead:<n>.draft       the verified draft (email.draft step's fact extractor):
                         {lane, to, subject, body, owner, owner_name, from_email,
                          first_name, event_name, draft_step, word_count, judge?}
    approval:<lane>      per-email decision: {decision: "approve" | "reject",
                          draft_step, to, subject, score?, flags?, decided_by,
                          confidence?, threshold?, reason?}
    approval:emails      the batch decision: {decision: "approve" | "reject" | "partial",
                          items: {<lane>: <per-email decision>}, approved: [lanes],
                          rejected: [lanes], decided_by, model?, threshold, step}

One `review.approval` step per run (skill `review`, lane None) covers every
draft that is ready when the CRM work has settled; its inputs list the items
(lane, draft fact ref, approval key). The meta-reviewer (Track J) decides it:
deterministic policy first, then ONE `judge.judge_drafts()` call for the whole
batch, auto-approving a draft only when its score >= approval_auto_threshold
and there are no policy flags (and `always_ask_human_email` is off); otherwise
the step escalates to a human. Whoever decides commits the facts above with
`commit_decisions()` (ledger.commit_fact).

The mailer sends a draft only when `approval_for(facts, lane)` says approve
AND the approved recipient/subject/draft step match what it is about to send.
The orchestrator creates `email.send` steps only for approved lanes.
"""

from __future__ import annotations

from typing import Any

import redis.asyncio as aioredis

from . import ledger
from .config import RunConfig
from .keys import Keys

BATCH_KEY = "approval:emails"
APPROVE, REJECT = "approve", "reject"
_APPROVE_WORDS = frozenset({"approve", "approved", "auto_approve", "yes", "send"})
_REJECT_WORDS = frozenset({"reject", "rejected", "no", "skip", "hold"})


def approval_key(lane: str) -> str:
    return f"approval:{lane}"


def draft_key(lane: str) -> str:
    return f"{lane}.draft"


def _norm(raw: Any) -> dict[str, Any] | None:
    """Normalise one decision record: bool, "approve", {"decision": ...} or {"approved": bool}."""
    if raw is None:
        return None
    if isinstance(raw, bool):
        return {"decision": APPROVE if raw else REJECT}
    if isinstance(raw, str):
        word = raw.strip().lower().split(":", 1)[0]
        if word in _APPROVE_WORDS:
            return {"decision": APPROVE}
        if word in _REJECT_WORDS:
            return {"decision": REJECT}
        return None
    if isinstance(raw, dict):
        d = dict(raw)
        if "decision" in d or "answer" in d:
            n = _norm(str(d.get("decision") or d.get("answer") or ""))
        elif "approved" in d:
            n = _norm(bool(d["approved"]))
        else:
            n = None
        if n is None:
            return None
        d["decision"] = n["decision"]
        return d
    return None


def _from_batch(batch: Any, lane: str) -> dict[str, Any] | None:
    if not isinstance(batch, dict):
        return None
    items = batch.get("items") if batch.get("items") is not None else batch.get("decisions")
    if isinstance(items, dict) and lane in items:
        return _norm(items[lane])
    if isinstance(items, list):
        for it in items:
            if isinstance(it, dict) and lane in (it.get("lane"), it.get("id")):
                return _norm(it)
    for field, decision in (("approved", APPROVE), ("rejected", REJECT)):
        if isinstance(batch.get(field), list) and lane in batch[field]:
            return {"decision": decision, "decided_by": batch.get("decided_by")}
    top = _norm({k: v for k, v in batch.items() if k in ("decision", "answer")})
    lanes = batch.get("lanes")
    if top is not None and top["decision"] in (APPROVE, REJECT) and isinstance(lanes, list) and lane in lanes:
        return {**top, "decided_by": batch.get("decided_by"), "batch": True}
    return None


def approval_for(facts: dict[str, Any], lane: str, *, approval_step: str | None = None) -> dict[str, Any] | None:
    """The committed decision for one email, or None (no decision on record).
    Looks at `approval:<lane>`, then the batch fact, then the approval step's
    default fact (`step:<id>`: the decider's claim data, committed by the verifier)."""
    rec = _norm(facts.get(approval_key(lane)))
    if rec is not None:
        return {**rec, "source": approval_key(lane)}
    rec = _from_batch(facts.get(BATCH_KEY), lane)
    if rec is not None:
        return {**rec, "source": BATCH_KEY}
    if approval_step:
        rec = _from_batch(facts.get(f"step:{approval_step}"), lane)
        if rec is not None:
            return {**rec, "source": f"step:{approval_step}"}
    return None


def is_approved(rec: dict[str, Any] | None) -> bool:
    return bool(rec) and rec.get("decision") == APPROVE


def mismatch(rec: dict[str, Any], draft: dict[str, Any]) -> str | None:
    """Why `draft` is not the draft that was approved (None when it is, or the
    decision did not pin recipient/subject/draft step)."""
    for k in ("to", "subject", "draft_step"):
        want, got = rec.get(k), draft.get(k)
        if want and got and str(want).strip().lower() != str(got).strip().lower():
            return f"approved {k} {want!r} != {got!r}"
    return None


# ---------------------------------------------------------------------------
# deciding a batch (used by the meta-reviewer, Track J, and by tests)
# ---------------------------------------------------------------------------


def policy_decisions(
    drafts: dict[str, dict[str, Any]], verdicts: dict[str, Any], cfg: RunConfig, *, model: str | None = None,
    decided_by: str = "meta-reviewer",
) -> tuple[dict[str, dict[str, Any]], list[str]]:
    """Apply the playbook approval policy to a batch.

    drafts:   {lane: draft fact}   (each already passed email.draft_valid)
    verdicts: {lane: judge.JudgeVerdict | {"score", "flags", "reasoning"}} from ONE judge_drafts() call
    Returns ({lane: decision record}, escalate) where `escalate` lists the lanes
    that need a human (score below approval_auto_threshold, flags, no score,
    or always_ask_human_email)."""
    out: dict[str, dict[str, Any]] = {}
    escalate: list[str] = []
    thr = cfg.approval_auto_threshold
    for lane, d in drafts.items():
        v = verdicts.get(lane)
        v = v.model_dump() if hasattr(v, "model_dump") else (v or {})
        score, flags = v.get("score"), list(v.get("flags") or [])
        flags += [f.get("policy") for f in (d.get("flags") or []) if isinstance(f, dict)]
        rec = {"draft_step": d.get("draft_step"), "to": d.get("to"), "subject": d.get("subject"),
               "score": score, "flags": flags, "threshold": thr, "decided_by": decided_by, "model": model,
               "reasoning": v.get("reasoning")}
        if cfg.always_ask_human_email:
            escalate.append(lane)
            rec.update(decision=None, reason="always_ask_human_email")
        elif score is None:
            escalate.append(lane)
            rec.update(decision=None, reason="no judge score")
        elif flags or score < thr:
            escalate.append(lane)
            rec.update(decision=None, reason=("policy flags " + ", ".join(map(str, flags))) if flags
                       else f"judge {score:.2f} < {thr}")
        else:
            rec.update(decision=APPROVE, confidence=score, reason=f"judge {score:.2f} >= {thr}, no flags")
        out[lane] = rec
    return out, escalate


def batch_record(items: dict[str, dict[str, Any]], *, decided_by: str, step_id: str | None = None,
                 model: str | None = None, threshold: float | None = None) -> dict[str, Any]:
    approved = sorted(k for k, v in items.items() if v.get("decision") == APPROVE)
    rejected = sorted(k for k, v in items.items() if v.get("decision") == REJECT)
    decision = APPROVE if len(approved) == len(items) else (REJECT if len(rejected) == len(items) else "partial")
    return {"decision": decision, "items": items, "approved": approved, "rejected": rejected,
            "decided_by": decided_by, "model": model, "threshold": threshold, "step": step_id}


async def commit_decisions(
    r: aioredis.Redis, keys: Keys, run_id: str, step_id: str, items: dict[str, dict[str, Any]], *,
    decided_by: str, actor: str, model: str | None = None, threshold: float | None = None,
) -> dict[str, Any]:
    """Commit `approval:<lane>` for every decided item and the batch fact
    `approval:emails` (merged with earlier batches). Callers: the meta-reviewer
    at/above threshold, a human answer, tests. Items with decision None
    (escalated) are left out of the per-lane facts."""
    facts = await ledger.get_facts(r, keys, run_id)
    decided = {k: v for k, v in items.items() if v.get("decision") in (APPROVE, REJECT)}
    for lane, rec in sorted(decided.items()):
        await ledger.commit_fact(r, keys, run_id, approval_key(lane), {**rec, "lane": lane, "step": step_id},
                                 source_step=step_id, actor=actor)
    prev = facts.get(BATCH_KEY) if isinstance(facts.get(BATCH_KEY), dict) else {}
    merged_items = {**(prev.get("items") or {}), **decided}
    batch = batch_record(merged_items, decided_by=decided_by, step_id=step_id, model=model, threshold=threshold)
    await ledger.commit_fact(r, keys, run_id, BATCH_KEY, batch, source_step=step_id, actor=actor)
    return batch
