"""Evidence report for a run (plans/02 G1-G3, plans/03 Phase 6).

Built only from ledger state: steps (with attempt history, claims and
verdicts), committed facts and the event log, plus the run config, the LLM
spend hash and agent cards. Nothing is taken from a worker's word alone: a
lead counts as created/updated only when the step that did it is committed
(the verifier read it back).

    report = await build_report(r, keys, run_id)       # dict, JSON-ready
    md = to_markdown(report)                            # also in report["markdown"]

The JSON shape matches the GUI's `Report` interface (web/src/api/types.ts):
summary, criteria, leads, phases, faults, decisions, coverage, emails,
agents, input, reproduce, totals; plus stats, recoveries, cost_by_role.

Conventions read (all optional, so a partial run still reports):
- per-lead steps carry lane "lead:N"; the lead record is the fact "lead:N";
  rows the parser dropped are in fact "parse.summary".flagged.
- claim.data / verdict.observed / postcondition.expect keys: owner,
  contact_id, contact_url, due, subject, to, decision, confidence, threshold,
  evidence, decided_by, result, dry_run.
"""

from __future__ import annotations

import json
import statistics
from dataclasses import dataclass, field
from typing import Any

import redis.asyncio as aioredis

from . import ledger
from .agent_config import get_config as get_agent_config
from .keys import Keys
from .protocol import AgentCard, Event, EventType, Fact, Run, Step, StepKind, StepStatus
from .settings import get_settings

S = StepStatus
K = StepKind
ROLES = ("orchestrator", "worker", "meta_reviewer", "verifier")
REVIEW_KINDS = {K.REVIEW_AMBIGUITY, K.REVIEW_APPROVAL, K.HUMAN_DECIDE}
WAITING = {S.INPUT_REQUIRED, S.REVIEW_REQUIRED}

CHANNEL_BY_PREFIX = {
    "crm.": "EspoCRM REST (read-only key)",
    "email.sent": "Mailpit API",
    "email.draft_valid": "deterministic + LLM judge",
    "file.": "source file re-read",
    "review.": "ledger decision fact",
    "run.": "ledger sweep",
}


@dataclass
class ReportInput:
    run: Run
    steps: list[Step]
    facts: dict[str, Fact]
    events: list[Event]
    config: dict[str, Any] = field(default_factory=dict)
    config_hash: str | None = None
    spend: dict[str, str] = field(default_factory=dict)
    cards: dict[str, AgentCard] = field(default_factory=dict)
    prompt_versions: dict[str, int] = field(default_factory=dict)
    models: dict[str, list[str]] = field(default_factory=dict)
    crm_public_url: str = ""
    playbook_version: Any = None


# ---- loading ------------------------------------------------------------------------


async def load_input(r: aioredis.Redis, keys: Keys, run_id: str) -> ReportInput:
    run = await ledger.get_run(r, keys, run_id)
    if run is None:
        raise ledger.NotFound(f"run {run_id} not found")
    cfg = await ledger.get_run_config(r, keys, run_id)  # reads either config store
    cards = {k: AgentCard.model_validate_json(v) for k, v in (await r.hgetall(keys.agents)).items()}
    prompts = {aid: (await get_agent_config(r, keys, aid)).prompt_version for aid in cards}
    s = get_settings()
    version = None
    try:
        from . import playbook

        version = playbook.load(run.playbook).version
    except Exception:  # noqa: BLE001 - playbook dir may not be mounted here
        pass
    return ReportInput(
        run=run, steps=await ledger.list_steps(r, keys, run_id),
        facts=await ledger.get_fact_records(r, keys, run_id), events=await ledger.run_events(r, keys, run_id),
        config=cfg.model_dump(mode="json"), config_hash=cfg.config_hash(),
        spend=await r.hgetall(keys.llm_spend(run_id)), cards=cards, prompt_versions=prompts,
        models={role: s.models_for(role) for role in ROLES}, crm_public_url=s.crm_public_url,
        playbook_version=version,
    )


async def build_report(r: aioredis.Redis, keys: Keys, run_id: str) -> dict[str, Any]:
    report = build(await load_input(r, keys, run_id))
    report["markdown"] = to_markdown(report)
    return report


# ---- small helpers -----------------------------------------------------------------------


def _secs(ms: int | None, t0: int) -> float | None:
    return None if ms is None else round((ms - t0) / 1000, 1)


def _p50(values: list[float]) -> float | None:
    return round(statistics.median(values), 1) if values else None


def _lead_no(key: str | None) -> int | None:
    if not key or not key.startswith("lead:"):
        return None
    tail = key[5:].split(".", 1)[0]
    return int(tail) if tail.isdigit() else None


def _sources(step: Step) -> list[dict[str, Any]]:
    """Where facts about a step live, most trusted first."""
    out = []
    if step.verdict and step.verdict.observed:
        out.append(step.verdict.observed)
    if step.claim and step.claim.data:
        out.append(step.claim.data)
    out.append(step.postcondition.expect)
    out.append(step.inputs)
    return out


def _pick(steps: list[Step], names: tuple[str, ...]) -> Any:
    for step in steps:
        for src in _sources(step):
            for n in names:
                v = src.get(n)
                if v not in (None, "", [], {}):
                    return v
    return None


def _by_kind(steps: list[Step], *kinds: StepKind, committed: bool = False) -> list[Step]:
    out = [s for s in steps if s.kind in kinds and (not committed or s.status == S.COMMITTED)]
    return sorted(out, key=lambda s: s.created_at)


def _name(v: Any) -> str | None:
    if isinstance(v, dict):
        return v.get("name") or v.get("userName") or v.get("id")
    return None if v is None else str(v)


def _evidence(steps: list[Step]) -> list[str]:
    seen: list[str] = []
    for s in steps:
        for a in s.history:
            for p in (a.claim.evidence if a.claim else []):
                if p not in seen:
                    seen.append(p)
    return seen


def _decision(step: Step) -> dict[str, Any]:
    return dict(step.claim.data) if step.claim else {}


# ---- sections --------------------------------------------------------------------------------


def _flagged(inp: ReportInput) -> dict[int, dict[str, Any]]:
    summary = inp.facts.get("parse.summary")
    flagged = (summary.value or {}).get("flagged", []) if summary and isinstance(summary.value, dict) else []
    return {f["row"]: f for f in flagged if isinstance(f, dict) and isinstance(f.get("row"), int)}


def _lead_rows(inp: ReportInput) -> list[dict[str, Any]]:
    lanes: dict[int, list[Step]] = {}
    for s in inp.steps:
        n = _lead_no(s.lane)
        if n is not None:
            lanes.setdefault(n, []).append(s)
    records: dict[int, dict[str, Any]] = {}
    for key, fact in inp.facts.items():
        n = _lead_no(key)
        if n is not None and key == f"lead:{n}" and isinstance(fact.value, dict):
            records[n] = fact.value
    flagged = _flagged(inp)
    for n, f in flagged.items():
        records.setdefault(n, f.get("record") or {})
    rows = []
    for n in sorted(set(records) | set(lanes)):
        rows.append(_lead_row(inp, n, records.get(n, {}), lanes.get(n, []), flagged.get(n)))
    return rows


def _lead_row(inp: ReportInput, n: int, rec: dict[str, Any], steps: list[Step],
              flag: dict[str, Any] | None) -> dict[str, Any]:
    live = [s for s in steps if s.status != S.REPLANNED]
    create = _by_kind(live, K.CRM_CREATE_CONTACT, committed=True)
    update = _by_kind(live, K.CRM_UPDATE_CONTACT, committed=True)
    search = _by_kind(live, K.CRM_SEARCH_CONTACT, committed=True)
    task = _by_kind(live, K.CRM_CREATE_TASK)
    reviews = _by_kind(live, *REVIEW_KINDS)
    crm_steps = create + update + search
    skip = next((s for s in reviews if s.status == S.COMMITTED and _decision(s).get("decision") == "skip"), None)
    waiting = [s for s in live if s.status in WAITING]
    dead = [s for s in live if s.status == S.DEAD]

    if flag and not live:
        outcome, detail = "skipped", flag.get("detail") or flag.get("reason")
    elif skip is not None:
        d = _decision(skip)
        outcome = "skipped"
        detail = d.get("reason") or "; ".join(d.get("evidence") or []) or f"skipped by {d.get('decided_by', 'reviewer')}"
    elif waiting:
        outcome, detail = "waiting", waiting[0].title or f"{waiting[0].kind.value} needs input"
    elif dead:
        v = dead[0].verdict
        outcome, detail = "failed", (v.reason if v else f"{dead[0].kind.value} gave up after {dead[0].attempt} attempts")
    elif create:
        outcome, detail = "created", "new contact, verified via REST"
    elif update:
        outcome, detail = "updated", "existing contact updated, verified via REST"
    elif search and _pick(search, ("result",)) == "matched":
        outcome, detail = "updated", "existing contact matched" + ("; follow-up task added" if any(
            s.status == S.COMMITTED for s in task) else "")
    elif live and all(s.status in (S.COMMITTED, S.DEAD) for s in live):
        outcome, detail = "skipped", "no CRM change needed"
    else:
        done = sum(1 for s in live if s.status == S.COMMITTED)
        outcome, detail = "waiting", f"in progress ({done}/{len(live)} steps committed)" if live else "not planned yet"

    contact_fact = inp.facts.get(f"lead:{n}.contact_id")
    contact_id = (contact_fact.value if contact_fact else None) or _pick(crm_steps, ("contact_id", "record_id"))
    url = _pick(crm_steps, ("contact_url", "crm_url"))
    if not url and contact_id and inp.crm_public_url:
        url = f"{inp.crm_public_url.rstrip('/')}/#Contact/view/{contact_id}"
    evidence = _evidence(create + update + task + search) or _evidence(live)
    main = (create or update or search or [None])[0]
    decider = next((s for s in reversed(reviews) if s.status == S.COMMITTED), None)
    decided_by = (_decision(decider).get("decided_by") if decider else None) or (
        main.claim.worker if main is not None and main.claim else None)
    return {
        "n": n, "name": rec.get("name") or "-", "company": rec.get("company"), "email": rec.get("email"),
        "outcome": outcome, "outcome_detail": detail,
        "owner": _name(_pick(crm_steps + task, ("owner", "owner_name", "assigned_user", "assigned_user_name",
                                               "owner_user_name"))),
        "task_due": _pick([s for s in task if s.status == S.COMMITTED] or task, ("due", "due_date", "dateEnd")),
        "email_status": _email_status(live),
        "decided_by": decided_by,
        "verified_by": main.verdict.verifier if main is not None and main.verdict else None,
        "check": main.verdict.check if main is not None and main.verdict else None,
        "contact_id": contact_id, "crm_url": url,
        "screenshot": evidence[-1] if evidence else None, "screenshots": evidence,
        "tries": sum(len(s.history) for s in steps),
        "flag": flag.get("reason") if flag else None,
    }


def _email_status(steps: list[Step]) -> str | None:
    send = _by_kind(steps, K.EMAIL_SEND)
    approval = _by_kind(steps, K.REVIEW_APPROVAL)
    draft = _by_kind(steps, K.EMAIL_DRAFT)
    if any(s.status == S.COMMITTED for s in send):
        s = next(s for s in send if s.status == S.COMMITTED)
        return "dry run (not sent)" if _pick([s], ("dry_run",)) else "sent"
    if any(s.status in WAITING for s in approval + send):
        return "awaiting approval"
    if any(s.status == S.DEAD for s in send + draft):
        return "failed"
    if any(s.status == S.COMMITTED for s in draft):
        return "drafted"
    if send or draft:
        return "pending"
    return None


def _decisions(inp: ReportInput) -> list[dict[str, Any]]:
    escalated = {e.step_id for e in inp.events if e.type == EventType.REVIEW_ESCALATED}
    out = []
    for s in sorted((s for s in inp.steps if s.kind in REVIEW_KINDS), key=lambda s: s.created_at):
        d = _decision(s)
        options = {o.get("value"): o.get("label") for o in d.get("options", []) if isinstance(o, dict)}
        if s.status == S.COMMITTED:
            value = d.get("value")
            result = " ".join(x for x in (d.get("decision"), options.get(value) or value) if x) or "decided"
        elif s.status in WAITING:
            result = "waiting on you"
        else:
            result = s.status.value
        out.append({
            "step_id": s.id, "lane": s.lane, "title": s.title or s.kind.value,
            "evidence": list(d.get("evidence") or []), "confidence": d.get("confidence"),
            "threshold": d.get("threshold"), "result": result,
            "decided_by": d.get("decided_by") or (s.claim.worker if s.claim else "-"),
            "escalated": s.id in escalated or s.status == S.INPUT_REQUIRED,
            "forced_reason": d.get("forced_reason"),
        })
    return out


def _coverage(inp: ReportInput) -> list[dict[str, Any]]:
    rows: dict[str, dict[str, Any]] = {}
    for s in inp.steps:
        for a in s.history:
            if not a.verdict:
                continue
            row = rows.setdefault(a.verdict.check, {"check": a.verdict.check, "runs": 0, "pass": 0, "reject": 0,
                                                    "_lat": []})
            row["runs"] += 1
            row["pass" if a.verdict.ok else "reject"] += 1
            if a.claim:
                row["_lat"].append(max(0, a.verdict.ts - a.claim.ts))
    out = []
    for check, row in sorted(rows.items()):
        channel = next((c for p, c in CHANNEL_BY_PREFIX.items() if check.startswith(p)), "ledger")
        lat = row.pop("_lat")
        out.append({**row, "channel": channel, "p50_ms": _p50(lat)})
    return out


def _emails(inp: ReportInput, leads: dict[int, dict[str, Any]]) -> list[dict[str, Any]]:
    lanes: dict[str, list[Step]] = {}
    for s in inp.steps:
        if s.lane and s.kind in (K.EMAIL_DRAFT, K.EMAIL_SEND, K.REVIEW_APPROVAL) and s.status != S.REPLANNED:
            lanes.setdefault(s.lane, []).append(s)
    out = []
    for lane, steps in sorted(lanes.items(), key=lambda kv: (_lead_no(kv[0]) or 0, kv[0])):
        draft = _by_kind(steps, K.EMAIL_DRAFT)
        approval = _by_kind(steps, K.REVIEW_APPROVAL)
        lead = leads.get(_lead_no(lane) or -1, {})
        judge = _pick(draft, ("judge_score", "score", "judge"))
        if approval:
            a = approval[-1]
            d = _decision(a)
            if a.status == S.COMMITTED:
                conf = d.get("confidence")
                approval_txt = f"{d.get('decision', 'approved')} by {d.get('decided_by', 'meta-reviewer')}" + (
                    f" ({conf:.2f})" if isinstance(conf, (int, float)) else "")
                judge = judge if judge is not None else conf
            else:
                approval_txt = "waiting on you" if a.status in WAITING else a.status.value
        else:
            approval_txt = "-"
        out.append({
            "lane": lane, "to": _pick(draft, ("to", "recipient")) or lead.get("email") or "-",
            "subject": _pick(draft, ("subject",)) or "-",
            "judge": judge if isinstance(judge, (int, float)) else None,
            "approval": approval_txt, "delivery": _email_status(steps) or "-",
        })
    return out


def _phases(inp: ReportInput, t0: int) -> list[dict[str, Any]]:
    groups: dict[str, dict[str, Any]] = {}
    for s in inp.steps:
        for a in s.history:
            g = groups.setdefault(s.kind.value, {"start": a.started_at, "end": 0, "open": False, "agents": set()})
            g["start"] = min(g["start"], a.started_at)
            if a.ended_at is None and s.status not in (S.COMMITTED, S.DEAD, S.REPLANNED):
                g["open"] = True
            g["end"] = max(g["end"], a.ended_at or (s.verdict.ts if s.verdict else s.updated_at))
            if a.worker:
                g["agents"].add(a.worker)
    out = []
    for kind, g in sorted(groups.items(), key=lambda kv: kv[1]["start"]):
        cat = ("review" if kind.startswith("review.") else "human" if kind.startswith("human.")
               else "verify" if kind.startswith("run.") else "work")
        out.append({"label": kind, "agent": ", ".join(sorted(g["agents"])) or "-", "kind": cat,
                    "start_s": _secs(g["start"], t0), "end_s": None if g["open"] else _secs(g["end"], t0)})
    return out


# ---- faults and recoveries (G2) ----------------------------------------------------------------


def _first(events: list[Event], start: int, pred) -> tuple[int, Event] | tuple[None, None]:
    for i in range(start, len(events)):
        if pred(events[i]):
            return i, events[i]
    return None, None


def _faults(inp: ReportInput, t0: int) -> list[dict[str, Any]]:
    evs = inp.events
    anchors: list[tuple[int, Event, Event | None]] = []  # (index, injected-or-consumed, consumed)
    used: set[int] = set()
    for i, e in enumerate(evs):
        if e.type != EventType.FAULT_INJECTED or e.payload.get("phase") == "consumed":
            continue
        j, consumed = _first(evs, i + 1, lambda x, f=e.payload.get("fault"): (
            x.type == EventType.FAULT_INJECTED and x.payload.get("phase") == "consumed"
            and x.payload.get("fault") == f))
        if j is not None and j not in used:
            used.add(j)
            anchors.append((j, e, consumed))
        else:
            anchors.append((i, e, None))
    for i, e in enumerate(evs):  # shots consumed without an API injection (redis-cli)
        if e.type == EventType.FAULT_INJECTED and e.payload.get("phase") == "consumed" and i not in used:
            anchors.append((i, e, e))
    anchors.sort(key=lambda a: a[0])
    return [_pair(evs, i, inj, consumed, t0) for i, inj, consumed in anchors]


def _committed_after(evs: list[Event], i: int, step_id: str | None) -> Event | None:
    if not step_id:
        return None
    return _first(evs, i, lambda x: x.type == EventType.STEP_COMMITTED and x.step_id == step_id)[1]


def _pair(evs: list[Event], i: int, inj: Event, consumed: Event | None, t0: int) -> dict[str, Any]:
    fault = inj.payload.get("fault", "?")
    anchor = consumed or inj
    step_id = anchor.step_id
    what, recovery, end = "", "not recovered yet", None
    recovery_events: list[str] = []
    if fault == "false_claim":
        what = f"{step_id or 'next step'}: claimed done without acting"
        _, rej = _first(evs, i, lambda x: x.type == EventType.STEP_REJECTED and (not step_id or x.step_id == step_id))
        if rej is not None:
            step_id = step_id or rej.step_id
            recovery = f"verifier rejected: {rej.payload.get('reason', '')}".strip()
            recovery_events.append(rej.id or "")
            done = _committed_after(evs, i, step_id)
            if done is not None:
                recovery += f"; retry committed (attempt {done.payload.get('attempt')})"
                end = done
    elif fault == "kill_worker":
        agent = inj.payload.get("agent_id")
        held = inj.payload.get("held")
        try:
            held_step = json.loads(held).get("current_step") if isinstance(held, str) else None
        except ValueError:
            held_step = None
        held_steps = [x for x in inj.payload.get("held_steps") or [] if x] or ([held_step] if held_step else [])
        what = f"killed {agent}" + (f" holding {', '.join(held_steps)}" if held_steps else "")

        def _last_lessee(j: int, sid: str | None) -> str | None:
            for k in range(j - 1, -1, -1):
                if evs[k].type == EventType.STEP_LEASED and evs[k].step_id == sid:
                    return evs[k].actor
            return None

        # the step the dead agent held: the recorded lease, else any step whose
        # last lease before expiring was the killed agent's
        _, exp = _first(evs, i, lambda x: x.type == EventType.STEP_LEASE_EXPIRED and x.step_id in held_steps)
        if exp is None:
            exp = next((evs[j] for j in range(i, len(evs)) if evs[j].type == EventType.STEP_LEASE_EXPIRED
                        and _last_lessee(j, evs[j].step_id) == agent), None)
        if exp is not None:
            step_id = exp.step_id
            recovery_events.append(exp.id or "")
            _, again = _first(evs, i, lambda x: x.type == EventType.STEP_LEASED and x.step_id == step_id
                              and x.actor != agent and x.ts >= exp.ts)
            recovery = f"lease expired after {round((exp.ts - inj.ts) / 1000, 1)}s"
            if again is not None:
                recovery += f"; {again.actor} took over (fence {again.payload.get('fence')})"
            done = _committed_after(evs, i, step_id)
            if done is not None:
                recovery += "; committed"
                end = done
    elif fault == "expire_session":
        what = "CRM session cookie invalidated"
        _, obs = _first(evs, i, lambda x: x.type == EventType.STEP_OBSERVATION and any(
            w in json.dumps(x.payload).lower() for w in ("login", "session", "relogin")))
        if obs is not None:
            step_id = obs.step_id
            recovery = "operator saw the login page and re-authenticated"
            recovery_events.append(obs.id or "")
            done = _committed_after(evs, i, step_id)
            if done is not None:
                recovery += "; step committed"
                end = done
            else:
                end = obs
    elif fault == "model_outage":
        what = "primary worker model unavailable"
        _, fb = _first(evs, i, lambda x: x.type == EventType.MODEL_FALLBACK)
        if fb is not None:
            step_id = fb.step_id
            recovery = f"model.fallback {fb.payload.get('from')} -> {fb.payload.get('to')}"
            recovery_events.append(fb.id or "")
            done = _committed_after(evs, i, step_id)
            end = done or fb
            if done is not None:
                recovery += "; step committed"
    elif fault == "ui_changed":
        what = "CRM UI selector broken"
        _, rp = _first(evs, i, lambda x: x.type in (EventType.STEP_REPLANNED, EventType.PLAN_REVISED))
        if rp is not None:
            recovery = "replanned to api.espocrm" if rp.type == EventType.PLAN_REVISED else "step replanned"
            recovery_events.append(rp.id or "")
            end = rp
    else:
        what = fault
    return {
        "at_s": _secs(anchor.ts, t0) or 0.0, "fault": fault, "what": what, "recovery": recovery,
        "lost_s": round((end.ts - anchor.ts) / 1000, 1) if end is not None else None,
        "step_id": step_id, "fault_event": inj.id, "recovery_events": [x for x in recovery_events if x],
        "recovered": end is not None,
    }


RECOVERY_TYPES = {
    EventType.STEP_REJECTED: "retry",
    EventType.STEP_LEASE_EXPIRED: "takeover",
    EventType.AGENT_LOST: "agent lost",
    EventType.MODEL_FALLBACK: "model fallback",
    EventType.STEP_REPLANNED: "replan",
    EventType.STEP_STALE_FENCE: "stale write refused",
}


def _recoveries(inp: ReportInput, t0: int) -> list[dict[str, Any]]:
    out = []
    for e in inp.events:
        if e.type not in RECOVERY_TYPES:
            continue
        p = e.payload
        detail = {EventType.STEP_REJECTED: p.get("reason"),
                  EventType.STEP_LEASE_EXPIRED: f"lease of {p.get('worker') or 'worker'} expired",
                  EventType.MODEL_FALLBACK: f"{p.get('from')} -> {p.get('to')}",
                  EventType.STEP_STALE_FENCE: f"fence {p.get('fence')} < {p.get('current_fence')}"}.get(e.type)
        out.append({"at_s": _secs(e.ts, t0), "type": RECOVERY_TYPES[e.type], "event": e.type.value,
                    "step_id": e.step_id, "actor": e.actor, "detail": detail or ""})
    return out


# ---- agents and cost (G3) ----------------------------------------------------------------------


def _agents(inp: ReportInput) -> list[dict[str, Any]]:
    per: dict[str, dict[str, Any]] = {}

    def row(agent: str) -> dict[str, Any]:
        return per.setdefault(agent, {"agent": agent, "steps": 0, "rejections": 0, "retries": 0, "_dur": [],
                                      "_models": [], "tokens": 0, "cost_usd": 0.0, "requests": 0,
                                      "cache_hits": 0, "_steps": set()})

    for s in inp.steps:
        for a in s.history:
            if not a.worker:
                continue
            rw = row(a.worker)
            if s.id in rw["_steps"]:
                rw["retries"] += 1
            rw["_steps"].add(s.id)
            if a.outcome == "committed":
                rw["steps"] += 1
            elif a.outcome == "rejected":
                rw["rejections"] += 1
            if a.ended_at:
                rw["_dur"].append(a.ended_at - a.started_at)
            if a.model:
                rw["_models"].append(a.model)
    for e in inp.events:
        if e.type == EventType.LLM_CALL:
            rw = row(e.actor)
            rw["requests"] += 1
            rw["tokens"] += int(e.payload.get("total_tokens") or 0)
            rw["cost_usd"] += float(e.payload.get("cost_usd") or 0)
            if e.payload.get("ok") and e.payload.get("model"):
                rw["_models"].append(e.payload["model"])
        elif e.type == EventType.LLM_CACHE_HIT:
            rw = row(e.actor)
            rw["cache_hits"] += 1
            if e.payload.get("model"):
                rw["_models"].append(e.payload["model"])
    out = []
    for agent, rw in sorted(per.items()):
        models = rw.pop("_models")
        card = inp.cards.get(agent)
        fallback = (inp.models.get(card.model_role) or [None])[0] if card and card.model_role else None
        model = " -> ".join(dict.fromkeys(models)) if models else (fallback or "-")
        rw.pop("_steps")
        dur = rw.pop("_dur")
        out.append({**rw, "model": model, "p50_ms": _p50(dur), "cost_usd": round(rw["cost_usd"], 6),
                    "role": card.role if card else None})
    return out


def _cost_by_role(inp: ReportInput) -> list[dict[str, Any]]:
    models: dict[str, list[str]] = {}
    cached: dict[str, int] = {}
    for e in inp.events:
        if e.type in (EventType.LLM_CALL, EventType.LLM_CACHE_HIT):
            role = e.payload.get("role") or "?"
            if e.payload.get("model") and e.payload.get("ok", True):
                models.setdefault(role, [])
                if e.payload["model"] not in models[role]:
                    models[role].append(e.payload["model"])
            if e.type == EventType.LLM_CACHE_HIT:
                cached[role] = cached.get(role, 0) + 1
    roles = {k.split(":", 1)[1] for k in inp.spend if ":" in k} | set(models) | set(cached)
    out = []
    for role in sorted(roles):
        out.append({"role": role, "models": models.get(role) or inp.models.get(role, []),
                    "requests": int(inp.spend.get(f"requests:{role}", 0) or 0),
                    "tokens": int(inp.spend.get(f"tokens:{role}", 0) or 0),
                    "cost_usd": round(float(inp.spend.get(f"usd:{role}", 0) or 0), 6),
                    "cache_hits": cached.get(role, 0)})
    return out


# ---- input and reproduce ----------------------------------------------------------------------


def _input(inp: ReportInput) -> dict[str, Any]:
    summary = inp.facts.get("parse.summary")
    val = summary.value if summary and isinstance(summary.value, dict) else {}
    stats = val.get("stats") or {}
    quality: list[dict[str, Any]] = []
    for reason, count in (stats.get("flagged_by_reason") or {}).items():
        example = next((f"row {f['row']}: {f.get('detail', '')}" for f in val.get("flagged", [])
                        if f.get("reason") == reason), None)
        quality.append({"n": count, "label": reason.replace("_", " "), "example": example})
    for key in ("emails_lowercased", "phones_normalised", "phones_unparseable", "fields_trimmed",
                "names_title_cased"):
        if stats.get(key):
            quality.append({"n": stats[key], "label": key.replace("_", " ")})
    return {"file": inp.run.input_file or val.get("source"), "rows": val.get("total_rows"),
            "usable": len(val.get("usable_rows") or []) if val else None,
            "sha256": inp.run.input_sha256 or val.get("sha256"), "quality": quality}


def _reproduce(inp: ReportInput, faults: list[dict[str, Any]]) -> dict[str, Any]:
    cfg = inp.config
    return {
        "config": cfg, "config_hash": inp.config_hash, "start_config_hash": inp.run.config_hash,
        "determinism": cfg.get("determinism"), "seed": cfg.get("seed"),
        "thresholds": {k: cfg.get(k) for k in ("review_auto_threshold", "approval_auto_threshold",
                                                "fuzzy_match_threshold")},
        "playbook": inp.run.playbook, "playbook_version": inp.playbook_version,
        "playbook_hash": inp.run.playbook_hash, "input_sha256": inp.run.input_sha256,
        "prompts": dict(sorted(inp.prompt_versions.items())), "models": inp.models,
        "faults": [f["fault"] for f in faults], "replay_of": inp.run.replay_of,
        "command": f"make replay RUN={inp.run.id}",
    }


# ---- assemble -----------------------------------------------------------------------------------


def _summary(inp: ReportInput, leads: list[dict[str, Any]], decisions: list[dict[str, Any]],
             emails: list[dict[str, Any]], faults: list[dict[str, Any]]) -> str:
    cnt = {k: sum(1 for x in leads if x["outcome"] == k) for k in ("created", "updated", "skipped", "waiting",
                                                                   "failed")}
    if not leads and not inp.steps:
        return f"Run {inp.run.status.value}; no steps planned yet."
    parts = [f"{len(leads)} rows processed: {cnt['created']} created, {cnt['updated']} updated, "
             f"{cnt['skipped']} skipped with a reason"]
    if cnt["waiting"]:
        parts[0] += f", {cnt['waiting']} waiting"
    if cnt["failed"]:
        parts[0] += f", {cnt['failed']} failed"
    parts[0] += "."
    if decisions:
        auto = sum(1 for d in decisions if d["result"] != "waiting on you" and not d["escalated"])
        human = sum(1 for d in decisions if d["escalated"] and d["result"] != "waiting on you")
        parts.append(f"{auto} of {len(decisions)} decisions made automatically" + (f", {human} by you" if human
                                                                                    else "") + ".")
    sent = sum(1 for e in emails if e["delivery"] == "sent")
    if emails:
        parts.append(f"{sent} of {len(emails)} follow-ups sent.")
    if faults:
        ok = sum(1 for f in faults if f["recovered"])
        parts.append(f"{len(faults)} injected fault{'s' if len(faults) != 1 else ''}, {ok} recovered.")
    return " ".join(parts)


def build(inp: ReportInput) -> dict[str, Any]:
    run = inp.run
    t0 = run.created_at
    last_ts = max([e.ts for e in inp.events] + [t0])
    leads = _lead_rows(inp)
    decisions = _decisions(inp)
    emails = _emails(inp, {x["n"]: x for x in leads})
    faults = _faults(inp, t0)
    agents = _agents(inp)
    cost = _cost_by_role(inp)
    criteria = [{**c.model_dump(mode="json"), "how": c.check} for c in run.criteria]
    lost = round(sum(f["lost_s"] or 0 for f in faults), 1)
    coverage = _coverage(inp)
    duplicates = sum(c["reject"] for c in coverage if c["check"] == "crm.no_duplicate")
    totals = {
        "tokens": int(inp.spend.get("prompt_tokens", 0) or 0) + int(inp.spend.get("completion_tokens", 0) or 0),
        "cost_usd": round(float(inp.spend.get("usd", 0) or 0), 6),
        "requests": int(inp.spend.get("requests", 0) or 0),
        "cache_hits": sum(c["cache_hits"] for c in cost),
        "duplicates": duplicates, "lost_s": lost,
        "steps": len(inp.steps), "committed": sum(1 for s in inp.steps if s.status == S.COMMITTED),
        "events": len(inp.events),
    }
    cnt = {k: sum(1 for x in leads if x["outcome"] == k) for k in ("created", "updated", "skipped", "waiting")}
    stats = [
        {"n": cnt["created"] + cnt["updated"], "label": "contacts verified", "sub": f"REST · {duplicates} duplicates"},
        {"n": cnt["skipped"], "label": "skipped with reason", "sub": "logged per row"},
        {"n": f"{sum(1 for d in decisions if not d['escalated'] and d['result'] != 'waiting on you')}/{len(decisions)}",
         "label": "decided automatically", "sub": "meta-reviewer"},
        {"n": cnt["waiting"], "label": "waiting on you", "sub": "see Decisions" if cnt["waiting"] else "none"},
        {"n": sum(1 for e in emails if e["delivery"] == "sent"), "label": "emails sent", "sub": "confirmed in Mailpit"},
        {"n": sum(1 for f in faults if f["recovered"]), "label": "faults recovered", "sub": f"{lost}s lost"},
    ]
    return {
        "run_id": run.id, "status": run.status.value, "goal": run.goal, "playbook": run.playbook,
        "summary": _summary(inp, leads, decisions, emails, faults),
        "created_at": run.created_at, "finished_at": run.finished_at,
        "duration_s": _secs(run.finished_at or last_ts, t0),
        "criteria": criteria, "leads": leads, "phases": _phases(inp, t0), "faults": faults,
        "recoveries": _recoveries(inp, t0), "decisions": decisions, "coverage": coverage,
        "emails": emails, "agents": agents, "cost_by_role": cost, "input": _input(inp),
        "reproduce": _reproduce(inp, faults), "totals": totals, "stats": stats,
    }


# ---- markdown ---------------------------------------------------------------------------------------


def _cell(v: Any) -> str:
    if v is None or v == "":
        return "-"
    return str(v).replace("|", "\\|").replace("\n", " ")


def _table(headers: list[str], rows: list[list[Any]]) -> list[str]:
    if not rows:
        return ["_none_", ""]
    out = ["| " + " | ".join(headers) + " |", "|" + "---|" * len(headers)]
    out += ["| " + " | ".join(_cell(c) for c in row) + " |" for row in rows]
    return out + [""]


def to_markdown(rep: dict[str, Any]) -> str:
    L = [f"# Evidence report · {rep['run_id']}", "",
         f"**Status:** {rep['status']} · **Duration:** {rep.get('duration_s')}s · **Playbook:** {rep.get('playbook')}",
         "", rep["summary"], "", f"**Goal:** {rep['goal']}", "", "## Success criteria", ""]
    L += _table(["Criterion", "Verdict", "How verified", "Evidence"],
                [[c["text"], c["status"], c["how"], c.get("evidence")] for c in rep["criteria"]])
    L += ["## Leads", ""]
    L += _table(["#", "Name", "Company", "Outcome", "Detail", "Owner", "Task due", "Email", "Decided by", "Check",
                 "CRM", "Screenshot"],
                [[x["n"], x["name"], x.get("company"), x["outcome"], x.get("outcome_detail"), x.get("owner"),
                  x.get("task_due"), x.get("email_status"), x.get("decided_by"), x.get("check"),
                  f"[open]({x['crm_url']})" if x.get("crm_url") else None,
                  f"![]({x['screenshot']})" if x.get("screenshot") else None] for x in rep["leads"]])
    L += ["## Decisions", ""]
    L += _table(["Decision", "Confidence", "Threshold", "Result", "Decided by", "Evidence"],
                [[d["title"], d.get("confidence"), d.get("threshold"), d["result"],
                  d["decided_by"] + (" (escalated)" if d.get("escalated") else ""), "; ".join(d["evidence"])]
                 for d in rep["decisions"]])
    L += ["## Faults and recoveries", ""]
    L += _table(["At (s)", "Fault", "What happened", "Recovery", "Time lost (s)"],
                [[f["at_s"], f["fault"], f["what"], f["recovery"], f.get("lost_s")] for f in rep["faults"]])
    if rep.get("recoveries"):
        L += ["### Every retry, takeover, fallback and replan", ""]
        L += _table(["At (s)", "Type", "Step", "By", "Detail"],
                    [[x["at_s"], x["type"], x.get("step_id"), x["actor"], x["detail"]] for x in rep["recoveries"]])
    L += ["## Verification coverage", ""]
    L += _table(["Check", "Channel", "Runs", "Pass", "Reject", "p50 (ms)"],
                [[c["check"], c["channel"], c["runs"], c["pass"], c["reject"], c.get("p50_ms")]
                 for c in rep["coverage"]])
    L += ["## Emails", ""]
    L += _table(["To", "Subject", "Judge", "Approval", "Delivery"],
                [[e["to"], e["subject"], e.get("judge"), e["approval"], e["delivery"]] for e in rep["emails"]])
    L += ["## Agents and cost", ""]
    L += _table(["Agent", "Model", "Steps", "Rejections", "Retries", "p50 (ms)", "Requests", "Tokens", "Cost ($)"],
                [[a["agent"], a["model"], a["steps"], a["rejections"], a["retries"], a.get("p50_ms"),
                  a.get("requests"), a.get("tokens"), a.get("cost_usd")] for a in rep["agents"]])
    L += _table(["Role", "Models", "Requests", "Cache hits", "Tokens", "Cost ($)"],
                [[c["role"], ", ".join(c["models"]), c["requests"], c["cache_hits"], c["tokens"], c["cost_usd"]]
                 for c in rep.get("cost_by_role", [])])
    inp = rep.get("input") or {}
    L += ["## Input file", "", f"`{inp.get('file')}` · {inp.get('rows')} rows · sha256 `{inp.get('sha256')}`", ""]
    L += _table(["Count", "Issue", "Example"], [[q["n"], q["label"], q.get("example")] for q in inp.get("quality", [])])
    rp = rep.get("reproduce") or {}
    L += ["## Reproduce this run", "", "```",
          f"determinism={rp.get('determinism')} seed={rp.get('seed')}",
          f"playbook={rp.get('playbook')} v{rp.get('playbook_version')} hash={rp.get('playbook_hash')}",
          f"input_sha256={rp.get('input_sha256')}",
          f"config_hash={rp.get('config_hash')}",
          f"thresholds={json.dumps(rp.get('thresholds'), sort_keys=True)}",
          f"prompts={json.dumps(rp.get('prompts'), sort_keys=True)}",
          f"models={json.dumps(rp.get('models'), sort_keys=True)}",
          rp.get("command", ""), "```", ""]
    t = rep.get("totals") or {}
    L += [f"**Totals:** {t.get('requests')} LLM requests ({t.get('cache_hits')} cache hits), {t.get('tokens')} tokens, "
          f"${t.get('cost_usd')}, {t.get('duplicates')} duplicates, {t.get('lost_s')}s lost to faults.", ""]
    return "\n".join(L)
