"""Per-lead lanes: deterministic fan-out and lane progression (B3, B4; no LLM).

One lane per lead, `lane = "lead:<row>"` (row = 1-based data row of the input
file). A lane grows in stages, each built only from committed facts:

    fan-out (parse committed)    crm.search_contact                 (usable rows)
                                 review.ambiguity  reason=phone_only (rows without email)
    after lookup committed       matched              -> crm.update_contact -> crm.create_task [-> tail]
                                 none + owner routed  -> crm.create_contact -> crm.create_task [-> tail]
                                 ambiguous (probable fuzzy match)   -> review.ambiguity reason=probable_match
                                 none, no owner (ambiguous_account | unknown_region) -> review.ambiguity
    after review decided         match_existing <contact_id> -> update -> task [-> tail]
                                 create_new                  -> create -> task [-> tail]
                                 link_account <account_id>   -> create (linked) -> task [-> tail]
                                 skip                        -> lane done (skipped, with the reason)

Tail stages (W3 hook): `register_tail(builder)` adds steps after the contact
stage, e.g. Track K's email.draft -> review.approval -> email.send. A builder
gets a LaneContext (lead, lookup, owner, open_deal, event, the contact and task
steps just built) and returns Steps (status planned) whose depends_on point at
those steps; it may return [] (e.g. open deal: no automated email).

Routing (B3): CRM kinds go to the skill picked by RunConfig.crm_write_path
(browser -> browser.espocrm, api -> api.espocrm, auto -> browser.espocrm first,
api.espocrm on replan); other kinds to the one skill that executes them. The
orchestrator never names a worker.

Bindings: a planned step may hold values "bind:<selector>.<field>" in its
inputs and postcondition. They are resolved by the orchestrator when the step
is released (planned -> ready), from the lane's latest committed step matching
the selector (a step kind, or "contact" = create or update contact): first the
verifier's observation (verdict.observed, read through REST), then the claim
data. Workers only ever see concrete values and "fact:" refs.

Review decisions: the decision for a lane is read from the fact
"review:<lane>" (written by the meta-reviewer / human answer, W3), else from
the committed review step's claim data. Shape: {"decision": "skip" |
"match_existing" | "create_new" | "link_account", "value": <id>, "owner"?: ...}.
"""

from __future__ import annotations

import datetime as dt
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from typing import Any

from .config import RunConfig
from .dates import follow_up_due
from .protocol import (
    CRM_KINDS,
    SKILL_KINDS,
    Postcondition,
    ReviewOption,
    Skill,
    Step,
    StepKind,
    StepStatus,
)

S = StepStatus
K = StepKind
LANE_PREFIX = "lead:"
BIND_PREFIX = "bind:"
CONTACT_KINDS = (K.CRM_CREATE_CONTACT, K.CRM_UPDATE_CONTACT)
REVIEW_KINDS = (K.REVIEW_AMBIGUITY, K.REVIEW_APPROVAL, K.HUMAN_DECIDE)
SELECTORS: dict[str, tuple[StepKind, ...]] = {"contact": CONTACT_KINDS}


class BindError(LookupError):
    pass


# ---------------------------------------------------------------------------
# routing
# ---------------------------------------------------------------------------

CRM_ROUTES: dict[str, tuple[Skill, ...]] = {
    "browser": (Skill.BROWSER_ESPOCRM,),
    "api": (Skill.API_ESPOCRM,),
    "auto": (Skill.BROWSER_ESPOCRM, Skill.API_ESPOCRM),
}


def route(kind: StepKind, cfg: RunConfig) -> Skill:
    """The skill a new step of `kind` is routed to."""
    if kind in CRM_KINDS:
        return CRM_ROUTES[cfg.crm_write_path][0]
    for skill, kinds in SKILL_KINDS.items():
        if kind in kinds:
            return skill
    raise ValueError(f"no skill executes {kind.value}")


def next_skill(kind: StepKind, current: Skill, cfg: RunConfig) -> Skill | None:
    """Replan target: the next skill on the route after `current`, if any."""
    if kind not in CRM_KINDS:
        return None
    path = CRM_ROUTES[cfg.crm_write_path]
    if current in path and path.index(current) + 1 < len(path):
        return path[path.index(current) + 1]
    if current not in path:  # route changed mid-run (config edit): go to the configured route
        return path[0]
    return None


# ---------------------------------------------------------------------------
# event context
# ---------------------------------------------------------------------------


@dataclass
class EventInfo:
    name: str
    date: str  # YYYY-MM-DD
    due: str  # follow-up due date (2 business days after)

    @property
    def task_subject(self) -> str:
        return f"Follow up: {self.name}"

    def as_dict(self) -> dict[str, Any]:
        return {"name": self.name, "date": self.date, "due": self.due, "task_subject": self.task_subject}


def event_info(name: str | None, date: str | None, created_at_ms: int) -> EventInfo:
    """`date` None or unparsable means 'yesterday' relative to the run's creation (UTC)."""
    day: dt.date | None = None
    if date:
        try:
            day = dt.date.fromisoformat(str(date)[:10])
        except ValueError:
            day = None
    if day is None:
        day = dt.datetime.fromtimestamp(created_at_ms / 1000, dt.UTC).date() - dt.timedelta(days=1)
    return EventInfo(name=name or "the event", date=day.isoformat(), due=follow_up_due(day).isoformat())


# ---------------------------------------------------------------------------
# lane context and tail hooks
# ---------------------------------------------------------------------------


@dataclass
class LaneContext:
    run_id: str
    lane: str
    row: int
    lead: dict[str, Any]
    lead_ref: str | None  # "fact:lead:<row>" when the lead is a committed fact
    cfg: RunConfig
    event: EventInfo
    lookup: dict[str, Any] = field(default_factory=dict)
    decision: dict[str, Any] | None = None
    owner: str | None = None
    open_deal: bool = False
    contact_step: Step | None = None
    task_step: Step | None = None


TailBuilder = Callable[[LaneContext], list[Step]]
TAIL_BUILDERS: list[TailBuilder] = []


def register_tail(builder: TailBuilder) -> TailBuilder:
    """Add a stage after contact + task for every lane (W3: email lanes)."""
    TAIL_BUILDERS.append(builder)
    return builder


# Run-level stages (Track K, additive): async hooks the orchestrator calls on
# every progress round, after lane progression, with the orchestrator itself
# (for its CRM reader, event info and waiting/moving view):
#     async def stage(orch, run, steps, facts, cfg) -> bool   # True = created/changed steps
# Used for work that spans lanes or needs I/O to plan (email: open-deal check,
# one approval batch per run, send steps after approval).
RunStage = Callable[..., Awaitable[bool]]
RUN_STAGES: list[RunStage] = []


def register_stage(stage: RunStage) -> RunStage:
    if stage not in RUN_STAGES:
        RUN_STAGES.append(stage)
    return stage


# ---------------------------------------------------------------------------
# step builders
# ---------------------------------------------------------------------------


def _name(lead: dict[str, Any]) -> str:
    return lead.get("name") or " ".join(x for x in (lead.get("first_name"), lead.get("last_name")) if x) or "lead"


def new_step(run_id: str, kind: StepKind, cfg: RunConfig, *, lane: str | None, title: str, inputs: dict[str, Any],
             check: str, args: dict[str, Any], expect: dict[str, Any] | None = None,
             depends_on: list[str] | None = None, side_effect: bool = False,
             idempotency_key: str | None = None, skill: Skill | None = None) -> Step:
    return Step(
        run_id=run_id, kind=kind, skill=skill or route(kind, cfg), title=title, inputs=inputs,
        postcondition=Postcondition(check=check, args=args, expect=expect or {}),
        depends_on=list(depends_on or []), side_effect=side_effect, idempotency_key=idempotency_key,
        lane=lane, max_attempts=cfg.max_attempts,
    )


def search_step(run_id: str, row: int, lead: dict[str, Any], cfg: RunConfig, parse_id: str) -> Step:
    ref = f"fact:{LANE_PREFIX}{row}"
    thr = cfg.fuzzy_match_threshold
    return new_step(
        run_id, K.CRM_SEARCH_CONTACT, cfg, lane=f"{LANE_PREFIX}{row}",
        title=f"Search CRM for {_name(lead)}" + (f" ({lead['company']})" if lead.get("company") else ""),
        inputs={"lead": ref, "threshold": thr, "idempotency_key": f"search:{lead.get('email') or row}"},
        check="crm.lookup_matches", args={"lead": ref, "threshold": thr}, depends_on=[parse_id],
    )


def review_step(run_id: str, lane: str, cfg: RunConfig, *, reason: str, lead: dict[str, Any] | str,
                question: str, options: list[ReviewOption], depends_on: list[str],
                context: dict[str, Any] | None = None, name: str = "lead") -> Step:
    return new_step(
        run_id, K.REVIEW_AMBIGUITY, cfg, lane=lane, title=f"Review {name}: {reason.replace('_', ' ')}",
        inputs={"lane": lane, "reason": reason, "lead": lead, "question": question,
                "options": [o.model_dump() for o in options], "context": context or {},
                "threshold": cfg.review_auto_threshold},
        check="review.decided", args={"lane": lane, "decision_key": f"review:{lane}"},
        depends_on=depends_on, idempotency_key=f"review:{lane}:{reason}",
    )


def phone_only_review(run_id: str, row: int, record: dict[str, Any], cfg: RunConfig, parse_id: str,
                      detail: str | None = None) -> Step:
    name = _name(record)
    return review_step(
        run_id, f"{LANE_PREFIX}{row}", cfg, reason="phone_only", lead=record, name=name, depends_on=[parse_id],
        question=f"Row {row} ({name}) has no email, only a phone number. The playbook skips phone-only rows "
                 "unless the company is a strategic account. Skip it?",
        options=[ReviewOption(label="Skip (no email)", value="skip", detail="playbook: phone-only rows are skipped"),
                 ReviewOption(label="Create a contact without email", value="create_new")],
        context={"detail": detail, "phone": record.get("phone"), "company": record.get("company")},
    )


def fan_out(run_id: str, parse_step: Step, facts: dict[str, Any], cfg: RunConfig) -> list[Step]:
    """Lanes for every row of the committed parse: a search per usable lead,
    a phone_only review per row without email. Duplicate rows (in-file) get no
    lane: the parser already accounted for them."""
    summary = facts.get("parse.summary") or {}
    steps: list[Step] = []
    for row in summary.get("usable_rows") or []:
        lead = facts.get(f"{LANE_PREFIX}{row}")
        if lead is not None:
            steps.append(search_step(run_id, int(row), lead, cfg, parse_step.id))
    for fl in summary.get("flagged") or []:
        if fl.get("reason") in ("phone_only", "no_contact"):
            steps.append(phone_only_review(run_id, int(fl["row"]), fl.get("record") or {}, cfg, parse_step.id,
                                           fl.get("detail")))
    return steps


def contact_stage(ctx: LaneContext, *, update_contact_id: str | None, account_id: str | None = None,
                  depends_on: list[str], account_name: str | None = None) -> list[Step]:
    """Contact step (create or update) + follow-up task, then any tail stages.
    account_name (Track M fix): the linked account's exact CRM name, so the browser
    operator (which links by name in the UI, it has no REST key) links the same
    account the verifier expects by id, not the lead's free-text company."""
    lead, cfg, lane = ctx.lead, ctx.cfg, ctx.lane
    email = (lead.get("email") or "").strip().lower()
    lead_in: Any = ctx.lead_ref or lead
    name = _name(lead)
    if update_contact_id:
        expect: dict[str, Any] = {"contact_id": update_contact_id}
        if email:
            expect["emails"] = [email]
        if ctx.owner:
            expect["owner"] = ctx.owner
        contact = new_step(
            ctx.run_id, K.CRM_UPDATE_CONTACT, cfg, lane=lane, title=f"Update contact {name}",
            inputs={"contact_id": update_contact_id, "lead": lead_in,
                    "idempotency_key": f"update_contact:{update_contact_id}:{email}"},
            check="crm.contact_exists", args={"email": email, "contact_id": update_contact_id}, expect=expect,
            depends_on=depends_on, side_effect=True, idempotency_key=f"update_contact:{update_contact_id}:{email}",
        )
    else:
        inputs: dict[str, Any] = {"lead": lead_in, "idempotency_key": f"create_contact:{email}"}
        expect = {}
        if ctx.owner:
            inputs["owner"] = expect["owner"] = ctx.owner
        if account_id:
            inputs["account_id"] = expect["account_id"] = account_id
            if account_name:
                inputs["account_name"] = account_name
        contact = new_step(
            ctx.run_id, K.CRM_CREATE_CONTACT, cfg, lane=lane, title=f"Create contact {name}", inputs=inputs,
            check="crm.contact_exists", args={"email": email}, expect=expect, depends_on=depends_on,
            side_effect=True, idempotency_key=f"create_contact:{email}",
        )
    subject = ctx.event.task_subject
    t_inputs: dict[str, Any] = {"contact_id": f"{BIND_PREFIX}contact.contact_id", "subject": subject,
                                "due": ctx.event.due, "event_name": ctx.event.name,
                                "idempotency_key": f"create_task:{email}:{subject}"}
    t_expect: dict[str, Any] = {"subject": subject, "due": ctx.event.due}
    if ctx.owner:
        t_inputs["owner"] = t_expect["owner"] = ctx.owner
    task = new_step(
        ctx.run_id, K.CRM_CREATE_TASK, cfg, lane=lane, title=f"Follow-up task for {name}", inputs=t_inputs,
        check="crm.task_exists", args={"contact_id": f"{BIND_PREFIX}contact.contact_id"}, expect=t_expect,
        depends_on=[contact.id], side_effect=True, idempotency_key=f"create_task:{email}:{subject}",
    )
    ctx.contact_step, ctx.task_step = contact, task
    steps = [contact, task]
    for builder in TAIL_BUILDERS:
        steps += builder(ctx) or []
    return steps


def after_lookup(ctx: LaneContext, search: Step) -> list[Step]:
    """Stage 2 of a lane, from the committed lookup (ctx.lookup)."""
    lk, lead = ctx.lookup, ctx.lead
    result = lk.get("result")
    ctx.owner = lk.get("owner")
    ctx.open_deal = bool(lk.get("open_deal"))
    name = _name(lead)
    if result == "matched" and lk.get("contact_id"):
        return contact_stage(ctx, update_contact_id=lk["contact_id"], depends_on=[search.id])
    if result == "ambiguous":
        cands = lk.get("candidates") or []
        opts = [ReviewOption(label=f"Same person as {c.get('name') or c.get('id')}", value=f"match_existing:{c.get('id')}",
                             detail=_cand_detail(c)) for c in cands]
        opts.append(ReviewOption(label="Different person: create a new contact", value="create_new"))
        return [review_step(
            ctx.run_id, ctx.lane, ctx.cfg, reason="probable_match", lead=ctx.lead_ref or lead, name=name,
            depends_on=[search.id], options=opts,
            question=f"Is {name} ({lead.get('email')}, {lead.get('company')}) the same person as an existing "
                     "CRM contact? Confirmed matches get the new email added as a secondary address.",
            context={"candidates": cands, "owner": lk.get("owner"), "owner_reason": lk.get("owner_reason")},
        )]
    if result == "none" and ctx.owner:
        return contact_stage(ctx, update_contact_id=None, account_id=lk.get("account_id"), depends_on=[search.id],
                             account_name=lk.get("account_name"))
    reason = lk.get("owner_reason") or "unknown_region"
    accts = lk.get("account_candidates") or []
    opts = [ReviewOption(label=f"Link to {a.get('name')}", value=f"link_account:{a.get('id')}",
                         detail=", ".join(f"{k}: {a[k]}" for k in ("owner", "country") if a.get(k))) for a in accts]
    opts.append(ReviewOption(label="Skip this lead", value="skip"))
    question = (f"{lead.get('company')!r} matches {len(accts)} CRM accounts; which one should {name} be linked to?"
                if reason == "ambiguous_account" else
                f"No owner can be routed for {name} ({lead.get('country') or 'no country'}). Who should own it?")
    return [review_step(ctx.run_id, ctx.lane, ctx.cfg, reason=reason, lead=ctx.lead_ref or lead, name=name,
                        depends_on=[search.id], options=opts, question=question,
                        context={"account_candidates": accts, "region": lk.get("region")})]


def _cand_detail(c: dict[str, Any]) -> str:
    bits = []
    for k in ("score", "name_score", "company_score"):
        if c.get(k) is not None:
            bits.append(f"{k} {c[k]}")
    if c.get("phone_match"):
        bits.append("same phone")
    return ", ".join(bits)


def parse_decision(raw: Any) -> dict[str, Any] | None:
    """Normalise a review decision: {"decision", "value", ...}. Accepts the
    option value form "match_existing:<id>" as well."""
    if raw is None:
        return None
    if isinstance(raw, str):
        raw = {"decision": raw}
    if not isinstance(raw, dict):
        return None
    d = dict(raw)
    dec = str(d.get("decision") or d.get("answer") or "")
    if ":" in dec:
        dec, _, val = dec.partition(":")
        d.setdefault("value", val)
    if isinstance(d.get("value"), str) and ":" in d["value"] and d["value"].split(":", 1)[0] == dec:
        d["value"] = d["value"].split(":", 1)[1]
    d["decision"] = dec
    return d if dec else None


def after_review(ctx: LaneContext, review: Step) -> list[Step]:
    """Continue a lane after its review was decided (ctx.decision)."""
    d = ctx.decision or {}
    dec, val = d.get("decision"), d.get("value")
    if d.get("owner"):
        ctx.owner = d["owner"]
    if dec in ("skip", "reject"):
        return []
    if not (ctx.lead.get("email") or "").strip():
        return []  # nothing can be written for a lead without an email (playbook: skip)
    if dec == "match_existing" and val:
        return contact_stage(ctx, update_contact_id=str(val), depends_on=[review.id])
    if dec == "link_account" and val:
        acct = next((a for a in ctx.lookup.get("account_candidates") or [] if a.get("id") == val), {})
        ctx.owner = ctx.owner or acct.get("owner")
        return contact_stage(ctx, update_contact_id=None, account_id=str(val), depends_on=[review.id],
                             account_name=acct.get("name"))
    if dec == "create_new":
        return contact_stage(ctx, update_contact_id=None, depends_on=[review.id])
    return []


# ---------------------------------------------------------------------------
# lane state helpers
# ---------------------------------------------------------------------------


def group_lanes(steps: list[Step]) -> dict[str, list[Step]]:
    lanes: dict[str, list[Step]] = {}
    for s in sorted(steps, key=lambda x: x.created_at):
        if s.lane and s.lane.startswith(LANE_PREFIX):
            lanes.setdefault(s.lane, []).append(s)
    return lanes


def lane_row(lane: str) -> int:
    return int(lane[len(LANE_PREFIX):])


def latest(steps: list[Step], kinds: tuple[StepKind, ...] | StepKind, *, status: StepStatus | None = None) -> Step | None:
    kinds = kinds if isinstance(kinds, tuple) else (kinds,)
    found = [s for s in steps if s.kind in kinds and (status is None or s.status == status)]
    return found[-1] if found else None


def live(steps: list[Step]) -> list[Step]:
    """Steps not superseded by a replan."""
    return [s for s in steps if s.status != S.REPLANNED]


def step_output(step: Step, facts: dict[str, Any]) -> dict[str, Any]:
    """What a committed step produced: its committed fact (default extractor
    key step:<id>), else the verified claim data overlaid with the verifier's
    observations."""
    fact = facts.get(f"step:{step.id}")
    if isinstance(fact, dict):
        return fact
    out = dict(step.claim.data) if step.claim else {}
    return out


def lookup_for(lane_steps: list[Step], facts: dict[str, Any]) -> tuple[Step | None, dict[str, Any]]:
    lane = lane_steps[0].lane if lane_steps else ""
    search = latest(lane_steps, K.CRM_SEARCH_CONTACT, status=S.COMMITTED)
    if search is None:
        return None, {}
    # The search step's full output (claim data: owner, owner_reason, region,
    # account_id, account_candidates, open_deal ...) overlaid with the
    # verifier-committed lane facts (checks/crm_facts.py: lookup is only
    # {result, match_type, contact_id, candidates}, REST values win).
    out = dict(step_output(search, facts))
    routing = facts.get(f"{lane}.routing")
    if isinstance(routing, dict):
        out.update({k: v for k, v in routing.items() if v is not None})
    fact = facts.get(f"{lane}.lookup")
    if isinstance(fact, dict):
        out.update({k: v for k, v in fact.items() if v is not None})
    for k in ("owner", "open_deal"):
        if facts.get(f"{lane}.{k}") is not None:
            out[k] = facts[f"{lane}.{k}"]
    return search, out


def decision_for(lane: str, review: Step | None, facts: dict[str, Any]) -> dict[str, Any] | None:
    d = parse_decision(facts.get(f"review:{lane}"))
    if d is None and review is not None and review.status == S.COMMITTED:
        out = step_output(review, facts)
        d = parse_decision(out.get("decision") and out)
    return d


def bind_value(ref: str, lane_steps: list[Step]) -> Any:
    selector, _, fld = ref[len(BIND_PREFIX):].partition(".")
    kinds = SELECTORS.get(selector) or (StepKind(selector),)
    src = latest(lane_steps, kinds, status=S.COMMITTED)
    if src is None:
        raise BindError(f"{ref}: no committed {selector} step in the lane")
    for source in ((src.verdict.observed if src.verdict else {}), (src.claim.data if src.claim else {})):
        if source.get(fld) not in (None, ""):
            return source[fld]
    raise BindError(f"{ref}: committed step {src.id} has no {fld!r}")


def resolve_binds(value: Any, lane_steps: list[Step]) -> Any:
    if isinstance(value, str) and value.startswith(BIND_PREFIX):
        return bind_value(value, lane_steps)
    if isinstance(value, dict):
        return {k: resolve_binds(v, lane_steps) for k, v in value.items()}
    if isinstance(value, list):
        return [resolve_binds(v, lane_steps) for v in value]
    return value


def has_binds(value: Any) -> bool:
    if isinstance(value, str):
        return value.startswith(BIND_PREFIX)
    if isinstance(value, dict):
        return any(has_binds(v) for v in value.values())
    if isinstance(value, list):
        return any(has_binds(v) for v in value)
    return False


# ---------------------------------------------------------------------------
# lane outcomes (for the criteria sweep and the CLI / report)
# ---------------------------------------------------------------------------

WAITING = frozenset({S.REVIEW_REQUIRED, S.INPUT_REQUIRED})


def lane_outcome(lane: str, lane_steps: list[Step], facts: dict[str, Any], *,
                 waiting_ids: set[str] | frozenset[str] = frozenset()) -> dict[str, Any]:
    """Summarise one lane: status done | skipped | waiting | in_progress | failed."""
    steps = live(lane_steps)
    row = lane_row(lane)
    lead = facts.get(lane) if isinstance(facts.get(lane), dict) else None
    if lead is None:
        review0 = latest(lane_steps, K.REVIEW_AMBIGUITY)
        lead = (review0.inputs.get("lead") if review0 and isinstance(review0.inputs.get("lead"), dict) else {}) or {}
    _, lookup = lookup_for(lane_steps, facts)
    review = latest(steps, REVIEW_KINDS)
    decision = decision_for(lane, review, facts) if review else None
    contact = latest(steps, CONTACT_KINDS)
    task = latest(steps, K.CRM_CREATE_TASK)
    kinds = {s.kind.value: s.status.value for s in steps}
    out: dict[str, Any] = {
        "lane": lane, "row": row, "name": lead.get("name"), "email": (lead.get("email") or "").lower() or None,
        "company": lead.get("company"), "kinds": kinds, "action": None, "contact_id": None,
        "owner": None, "emails": [], "task": None, "reason": None, "open_deal": bool(lookup.get("open_deal")),
        "review": ({"kind": review.kind.value, "status": review.status.value, "reason": review.inputs.get("reason"),
                    "decision": decision} if review else None),
    }
    if out["email"]:
        out["emails"] = [out["email"]]
    if contact is not None:
        c_owner = contact.postcondition.expect.get("owner")
        out["owner"] = c_owner
        if contact.status == S.COMMITTED:
            data = step_output(contact, facts)
            observed = contact.verdict.observed if contact.verdict else {}
            out["contact_id"] = observed.get("contact_id") or data.get("contact_id")
            out["action"] = "created" if contact.kind == K.CRM_CREATE_CONTACT else "updated"
            out["write"] = data.get("action")  # created | exists | updated | unchanged (check-then-act)
            out["owner"] = c_owner or observed.get("owner")
    if task is not None:
        exp = task.postcondition.expect
        out["task"] = {"subject": exp.get("subject"), "due": exp.get("due"), "owner": exp.get("owner") or out["owner"],
                       "status": task.status.value}
    dead = [s for s in steps if s.status == S.DEAD]
    if dead:
        out["status"] = "failed"
        last = dead[-1]
        out["reason"] = (last.verdict.reason if last.verdict else None) or f"{last.kind.value} dead"
    elif decision and contact is None and (decision.get("decision") in ("skip", "reject") or not out["email"]):
        out["status"] = "skipped"
        out["reason"] = decision.get("reason") or (f"review decided {decision['decision']}" if out["email"]
                                                   else "no email: nothing to write (playbook)")
    elif steps and all(s.status == S.COMMITTED for s in steps) and contact is not None:
        out["status"] = "done"
    elif any(s.status in WAITING or s.id in waiting_ids or (s.kind in REVIEW_KINDS and s.status != S.COMMITTED)
             for s in steps):
        out["status"] = "waiting"
        out["reason"] = (review.inputs.get("reason") if review else None) or "waiting for a decision"
    else:
        out["status"] = "in_progress"
    return out


def lane_outcomes(steps: list[Step], facts: dict[str, Any], *,
                  waiting_ids: set[str] | frozenset[str] = frozenset()) -> list[dict[str, Any]]:
    return [lane_outcome(lane, ls, facts, waiting_ids=waiting_ids)
            for lane, ls in sorted(group_lanes(steps).items(), key=lambda kv: lane_row(kv[0]))]
