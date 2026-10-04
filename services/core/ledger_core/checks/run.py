"""run.criteria_met: the final run-level sweep (plans/01 §8, D5).

A run's success criteria (protocol.Criterion) each name a check from the
postcondition registry. At the end of a run the orchestrator evaluates every
criterion *at run level*: the criterion's check is applied to every lead lane
it covers, through the same read-only channels the verifier uses (CRM REST via
ctx.crm, committed facts), never through worker claims.

    results = await evaluate_criteria(criteria, lanes, ctx)
    # -> [CriterionResult(id, status, evidence, observed)]

`lanes` are lane outcome dicts built by the orchestrator from ledger state
(orchestrator_lanes.lane_outcomes):

    {"lane": "lead:3", "row": 3, "name": ..., "email": ...,
     "status": "done" | "skipped" | "waiting" | "in_progress" | "failed",
     "action": "created" | "updated" | ..., "contact_id": ..., "owner": <expected owner or None>,
     "emails": [<lead email>], "task": {"subject", "due", "owner"} | None,
     "review": {"kind", "status", "decision"} | None,
     "kinds": {<step kind>: <latest status>}, "reason": ...}

Statuses: a criterion is `verified` when its check passed for every lane it
applies to, `failed` when any applicable lane failed it (or the lane itself
failed), and `pending` while applicable lanes are still waiting (review,
escalation, or work not built yet). The run may only complete when every
criterion is verified or waived (D5).

Run-level evaluators are pluggable: `@register_criterion("email.sent")` lets a
later track (email lanes, W3) refine how its check is swept.

The registered check `run.criteria_met` wraps the sweep:
    args   {"criteria": [Criterion dicts], "lanes": [lane outcomes]}
    ok     every criterion verified or waived; observed["criteria"] lists results.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from dataclasses import asdict, dataclass, field
from typing import Any

from ..postconditions import CheckContext, CheckResult, register, run_check
from ..protocol import Criterion, StepKind, StepStatus

VERIFIED, FAILED, PENDING, WAIVED = "verified", "failed", "pending", "waived"
CONTACT_KINDS = (StepKind.CRM_CREATE_CONTACT.value, StepKind.CRM_UPDATE_CONTACT.value)


@dataclass
class CriterionResult:
    id: str
    status: str
    evidence: str
    observed: dict[str, Any] = field(default_factory=dict)

    def as_dict(self) -> dict[str, Any]:
        return asdict(self)


Evaluator = Callable[[Criterion, list[dict[str, Any]], CheckContext], Awaitable[CriterionResult]]
EVALUATORS: dict[str, Evaluator] = {}


def register_criterion(check: str) -> Callable[[Evaluator], Evaluator]:
    def deco(fn: Evaluator) -> Evaluator:
        EVALUATORS[check] = fn
        return fn

    return deco


def _lanes_with_contact(lanes: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Lanes that should end with a CRM contact (not skipped)."""
    return [x for x in lanes if x.get("status") != "skipped"]


async def _sweep_per_lane(
    crit: Criterion, lanes: list[dict[str, Any]], ctx: CheckContext,
    make: Callable[[dict[str, Any]], tuple[dict[str, Any], dict[str, Any]] | None], noun: str,
) -> CriterionResult:
    """Run `crit.check` once per applicable lane. `make(lane)` returns (args,
    expect) or None when the lane has nothing to check yet."""
    applicable = _lanes_with_contact(lanes)
    passed, failed, pending = [], [], []
    observed: dict[str, Any] = {}
    for lane in applicable:
        name = lane["lane"]
        if lane.get("status") == "failed":
            failed.append(f"{name}: lane failed ({lane.get('reason') or 'dead step'})")
            continue
        made = make(lane) if lane.get("status") == "done" else None
        if made is None:
            pending.append(name)
            continue
        args, expect = made
        res = await run_check(crit.check, args, expect, ctx)
        observed[name] = {"ok": res.ok, "reason": res.reason}
        (passed if res.ok else failed).append(name if res.ok else f"{name}: {res.reason}")
    total = len(applicable)
    if failed:
        return CriterionResult(crit.id, FAILED, f"{len(failed)}/{total} {noun} failed: " + "; ".join(failed[:4]),
                               observed)
    if pending:
        return CriterionResult(crit.id, PENDING, f"{len(passed)}/{total} {noun} verified; waiting on "
                               + ", ".join(pending), observed)
    if not total:
        return CriterionResult(crit.id, VERIFIED, f"no {noun} to check", observed)
    return CriterionResult(crit.id, VERIFIED, f"{len(passed)}/{total} {noun} verified via {crit.check}", observed)


@register_criterion("crm.no_duplicate")
async def _no_duplicate(crit: Criterion, lanes: list[dict[str, Any]], ctx: CheckContext) -> CriterionResult:
    def make(lane: dict[str, Any]):
        emails = [e for e in (lane.get("emails") or [lane.get("email")]) if e]
        return ({"emails": emails}, {}) if emails else None

    return await _sweep_per_lane(crit, lanes, ctx, make, "contacts (one per email)")


@register_criterion("crm.contact_exists")
async def _contact_exists(crit: Criterion, lanes: list[dict[str, Any]], ctx: CheckContext) -> CriterionResult:
    def make(lane: dict[str, Any]):
        if not lane.get("email"):
            return None
        expect = {k: lane[k] for k in ("owner", "contact_id") if lane.get(k)}
        return {"email": lane["email"]}, expect

    return await _sweep_per_lane(crit, lanes, ctx, make, "contacts (owner per routing)")


@register_criterion("crm.task_exists")
async def _task_exists(crit: Criterion, lanes: list[dict[str, Any]], ctx: CheckContext) -> CriterionResult:
    def make(lane: dict[str, Any]):
        task = lane.get("task")
        if not task or not lane.get("contact_id"):
            return None
        expect = {k: task[k] for k in ("subject", "due", "owner") if task.get(k)}
        return {"contact_id": lane["contact_id"]}, expect

    return await _sweep_per_lane(crit, lanes, ctx, make, "follow-up tasks")


def _kind_sweep(kind: str, noun: str, *, none_status: str = VERIFIED) -> Evaluator:
    """Criterion holds when every step of `kind` in the run's lanes is committed
    (each was already verified by its own postcondition)."""

    async def ev(crit: Criterion, lanes: list[dict[str, Any]], ctx: CheckContext) -> CriterionResult:
        having = [x for x in lanes if kind in (x.get("kinds") or {})]
        done = [x["lane"] for x in having if x["kinds"][kind] == StepStatus.COMMITTED.value]
        dead = [x["lane"] for x in having if x["kinds"][kind] == StepStatus.DEAD.value]
        waiting = [x["lane"] for x in having if x["lane"] not in done and x["lane"] not in dead]
        obs = {"committed": done, "dead": dead, "waiting": waiting}
        if dead:
            return CriterionResult(crit.id, FAILED, f"{kind} dead in " + ", ".join(dead), obs)
        if waiting:
            return CriterionResult(crit.id, PENDING, f"{len(done)}/{len(having)} {noun}; waiting on "
                                   + ", ".join(waiting), obs)
        if not having:
            return CriterionResult(crit.id, none_status, f"no {kind} steps in this run" + (
                "" if none_status == VERIFIED else " yet"), obs)
        return CriterionResult(crit.id, VERIFIED, f"{len(done)}/{len(having)} {noun} verified", obs)

    return ev


register_criterion("crm.lookup_matches")(_kind_sweep(StepKind.CRM_SEARCH_CONTACT.value, "CRM lookups"))
# Email lanes are added by a later track (W3); until then there is nothing sent,
# so the email criteria stay pending rather than pass vacuously.
register_criterion("email.sent")(_kind_sweep(StepKind.EMAIL_SEND.value, "emails sent", none_status=PENDING))
register_criterion("email.draft_valid")(_kind_sweep(StepKind.EMAIL_DRAFT.value, "drafts valid", none_status=PENDING))


@register_criterion("review.decided")
async def _review_decided(crit: Criterion, lanes: list[dict[str, Any]], ctx: CheckContext) -> CriterionResult:
    having = [x for x in lanes if x.get("review")]
    decided = [x["lane"] for x in having if (x["review"] or {}).get("decision")]
    waiting = [x["lane"] for x in having if x["lane"] not in decided]
    obs = {"decided": decided, "waiting": waiting}
    if waiting:
        return CriterionResult(crit.id, PENDING, f"{len(decided)}/{len(having)} review(s) decided; waiting on "
                               + ", ".join(waiting), obs)
    if not having:
        return CriterionResult(crit.id, VERIFIED, "no rows needed review", obs)
    return CriterionResult(crit.id, VERIFIED, f"{len(decided)} review(s) decided with a recorded decision", obs)


@register_criterion("file.parsed_rows")
async def _parsed(crit: Criterion, lanes: list[dict[str, Any]], ctx: CheckContext) -> CriterionResult:
    summary = ctx.facts.get("parse.summary")
    if not summary:
        return CriterionResult(crit.id, PENDING, "parse step not committed")
    return CriterionResult(crit.id, VERIFIED, f"{summary.get('total_rows')} rows parsed: "
                           f"{len(summary.get('usable_rows') or [])} usable, {len(summary.get('flagged') or [])} flagged",
                           {"summary": {k: summary.get(k) for k in ("total_rows", "usable_rows")}})


async def evaluate_criteria(
    criteria: list[Criterion] | list[dict[str, Any]], lanes: list[dict[str, Any]], ctx: CheckContext,
) -> list[CriterionResult]:
    out: list[CriterionResult] = []
    for raw in criteria:
        crit = raw if isinstance(raw, Criterion) else Criterion.model_validate(raw)
        if crit.status == WAIVED:
            out.append(CriterionResult(crit.id, WAIVED, crit.evidence or "waived"))
            continue
        ev = EVALUATORS.get(crit.check)
        if ev is None:
            out.append(CriterionResult(crit.id, PENDING, f"no run-level evaluator for check {crit.check!r}"))
            continue
        try:
            out.append(await ev(crit, lanes, ctx))
        except Exception as exc:  # noqa: BLE001 - a crashing sweep is a failed criterion, never a pass
            out.append(CriterionResult(crit.id, FAILED, f"sweep of {crit.check} raised {type(exc).__name__}: {exc}"))
    return out


def all_met(results: list[CriterionResult]) -> bool:
    return bool(results) and all(x.status in (VERIFIED, WAIVED) for x in results)


@register("run.criteria_met")
async def criteria_met(args: dict[str, Any], expect: dict[str, Any], ctx: CheckContext) -> CheckResult:
    criteria = args.get("criteria") or []
    if not criteria:
        return CheckResult(False, "run.criteria_met needs args.criteria")
    results = await evaluate_criteria(criteria, args.get("lanes") or [], ctx)
    observed = {"criteria": [x.as_dict() for x in results]}
    open_ = [f"{x.id} {x.status}: {x.evidence}" for x in results if x.status not in (VERIFIED, WAIVED)]
    if open_:
        return CheckResult(False, "criteria not met: " + "; ".join(open_), observed)
    return CheckResult(True, f"all {len(results)} criteria verified or waived", observed)
