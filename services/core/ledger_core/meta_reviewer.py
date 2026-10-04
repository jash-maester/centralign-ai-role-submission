"""The meta-reviewer: decide review steps, escalate only below threshold (plans/01 §4, §9; E1-E6).

    mr = MetaReviewer(r, keys, agent_id="meta-reviewer")    # python -m ledger_core.services.meta_reviewer
    await mr.run()                                            # consume queue:review until stop()

It is a worker_base.Worker on skill `review` (lease, fence, heartbeats,
liveness), with its own outcome per step:

review.ambiguity
  1. Gather deterministic evidence, read-only: the lead, the CRM candidates
     (CrmReader with the verifier's read-only key: emails, phones, shared
     domains, account websites/countries, open deals, owner and task history),
     the playbook's escalation rules, and saved playbook rules.
  2. A saved playbook rule for exactly this case (reason + company/domain)
     decides with confidence 1.0 and no LLM call (E5 "next time").
  3. Otherwise llm.complete(role "meta_reviewer", schema ReviewDecision).
  4. Policy guards (playbook "Escalation rules"): the answer must be one of
     the step's options; a company matching several accounts with nothing to
     tell them apart always escalates.
  5. confidence >= RunConfig.review_auto_threshold (and no guard): commit the
     decision fact `review:<lane>`, emit review.resolved, claim the step with
     the decision (the verifier's review.decided commits it).
     Below: escalations.escalate() -> input_required + review.escalated +
     input.requested + an Escalation on queue:human. Only that lane waits.

review.approval (Track K's email batch)
  inputs.drafts = [{id, lane?, to, subject, body, checks_ok?, check_reason?}]
  Approve when every draft passed its deterministic checks (and has no
  placeholders / policy flags here), the batch judge (judge.judge_drafts, one
  LLM call) gives min score >= approval_auto_threshold with no flags,
  llm_judge_enabled is on and always_ask_human_email is off -> fact
  `approval:<lane|key>` + approval.auto. Otherwise escalate (approve / reject).

A step that comes back after a human answer (inputs.human_decision) is
claimed with that decision, no LLM call. Every decision records confidence,
threshold, evidence, decided_by and model (E6) in the claim and the fact.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import redis.asyncio as aioredis

from . import agent_config, escalations, ledger, llm, playbook, prompts
from .config import RunConfig
from .crm_api import CrmError, contact_emails, normalize_phone, reader_from_redis
from .events import append_event
from .keys import Keys
from .protocol import (
    AgentCard,
    Claim,
    Event,
    EventType,
    ReviewDecision,
    ReviewOption,
    Skill,
    Step,
    StepKind,
)
from .worker_base import Worker

log = logging.getLogger("ledger.meta_reviewer")

PERSONAL_DOMAINS = frozenset({"gmail.com", "googlemail.com", "yahoo.com", "hotmail.com", "outlook.com", "icloud.com",
                              "proton.me", "protonmail.com", "aol.com", "gmx.de", "live.com", "me.com"})
FORCED_ACCOUNTS = "playbook: the company matches more than one account and nothing distinguishes them"


def meta_reviewer_card(agent_id: str = "meta-reviewer") -> AgentCard:
    return AgentCard.model_validate({
        "id": agent_id, "name": "Meta-reviewer", "role": "meta_reviewer", "model_role": "meta_reviewer",
        "skills": [{"id": Skill.REVIEW.value,
                    "kinds": [StepKind.REVIEW_AMBIGUITY.value, StepKind.REVIEW_APPROVAL.value]}],
        "side_effects": False, "container": "meta-reviewer",
        "tools": [
            {"id": "crm-rest", "type": "rest", "name": "EspoCRM REST (read-only key)",
             "locked_reason": "Verifier-only channel"},
            {"id": "playbook", "type": "function", "name": "Playbook (escalation rules, saved rules)"},
            {"id": "facts", "type": "function", "name": "Committed facts (read-only)"},
            {"id": "llm", "type": "llm", "name": "Decision model (role meta_reviewer)"},
            {"id": "judge", "type": "llm", "name": "Batch draft judge (approvals)"},
        ],
    })


# ---------------------------------------------------------------------------
# outcome of one review
# ---------------------------------------------------------------------------


@dataclass
class Outcome:
    decision: ReviewDecision
    auto: bool  # True: resolve; False: escalate
    tried: list[str] = field(default_factory=list)
    forced_reason: str | None = None
    label: str | None = None
    option: str | None = None
    source: str = "llm"  # llm | rule | human | policy
    extra: dict[str, Any] = field(default_factory=dict)

    def record(self) -> dict[str, Any]:
        rec = escalations.decision_record(self.decision, option=self.option, label=self.label, extra=self.extra)
        rec["source"] = self.source
        if self.forced_reason:
            rec["forced_reason"] = self.forced_reason
        return rec


def _domain(email: str | None) -> str:
    return str(email or "").rpartition("@")[2].strip().lower()


def _opts(step: Step) -> list[ReviewOption]:
    out = []
    for o in step.inputs.get("options") or []:
        try:
            out.append(o if isinstance(o, ReviewOption) else ReviewOption.model_validate(o))
        except Exception:  # noqa: BLE001 - a malformed option is dropped, never invented
            continue
    return out


def _lead(inputs: dict[str, Any]) -> dict[str, Any]:
    lead = inputs.get("lead")
    return lead if isinstance(lead, dict) else {}


def match_option(d: ReviewDecision, options: list[ReviewOption]) -> ReviewOption | None:
    """The option a model decision names: "<decision>:<value>" or "<decision>"."""
    values = {o.value: o for o in options}
    val = str(d.value or "")
    if val in values:  # model returned the full option value as `value`
        return values[val]
    if d.value and f"{d.decision}:{val}" in values:
        return values[f"{d.decision}:{val}"]
    if d.decision in values and not d.value:
        return values[d.decision]
    same = [o for o in options if escalations.split_option(o.value)[0] == d.decision]
    if d.value:  # the model named the record (e.g. "Lumen Inc") rather than its id
        want = val.strip().lower()
        named = [o for o in same if want and (want in o.label.lower() or want in (o.detail or "").lower())]
        return named[0] if len(named) == 1 else None
    return same[0] if len(same) == 1 else None


# ---------------------------------------------------------------------------
# evidence (deterministic, read-only)
# ---------------------------------------------------------------------------


async def ambiguity_evidence(inputs: dict[str, Any], crm: Any) -> tuple[list[str], dict[str, Any]]:
    """Evidence lines for the prompt and the record, plus structured signals."""
    reason = str(inputs.get("reason") or "")
    lead = _lead(inputs)
    ctx = inputs.get("context") if isinstance(inputs.get("context"), dict) else {}
    ev: list[str] = []
    sig: dict[str, Any] = {"reason": reason}
    dom = _domain(lead.get("email"))
    phone = normalize_phone(lead.get("phone"))
    if dom:
        ev.append(f"lead email domain {dom}" + (" is a personal mailbox domain" if dom in PERSONAL_DOMAINS else ""))
    if crm is None:
        ev.append("CRM REST unavailable: evidence from the search step only")

    if reason == "probable_match":
        for c in ctx.get("candidates") or []:
            cid = c.get("id")
            full = None
            if crm is not None and cid:
                try:
                    full = await crm.get_contact(cid)
                except Exception as exc:  # noqa: BLE001
                    ev.append(f"REST: contact {cid} unreadable ({type(exc).__name__})")
            name = (full or {}).get("name") or c.get("name") or cid
            phones = (full or {}).get("phones") or [p for p in [normalize_phone(c.get("phoneNumber"))] if p]
            emails = (full.get("emails") if full else contact_emails(c)) or []
            same_phone = bool(phone) and phone in phones
            same_domain = bool(dom) and any(_domain(e) == dom for e in emails or [])
            ev.append(f"candidate {name} ({cid}): name score {c.get('name_score')}, company score "
                      f"{c.get('company_score')}, account {(full or {}).get('accountName') or c.get('accountName')}")
            ev.append(f"phone {'matches' if same_phone else 'differs from'} the lead's ({phone or 'no phone'})")
            if emails:
                ev.append(f"{name} emails: {', '.join(emails)}" + ("; same domain as the lead" if same_domain else ""))
            owner = (full or {}).get("assignedUserName") or (full or {}).get("owner") or c.get("owner")
            if owner:
                ev.append(f"{name} is owned by {owner}")
            if crm is not None and cid:
                try:
                    deals = await crm.open_opportunities_for_contact(cid)
                    tasks = await crm.tasks_for_contact(cid)
                    ev.append(f"{name}: {len(deals)} open deal(s), {len(tasks)} task(s)"
                              + (f" owned by {', '.join(sorted({str(t.get('assignedUserName')) for t in tasks if t.get('assignedUserName')}))}"
                                 if tasks else ""))
                except Exception as exc:  # noqa: BLE001
                    ev.append(f"REST: history of {cid} unreadable ({type(exc).__name__})")
            sig.setdefault("candidates", []).append({"id": cid, "same_phone": same_phone, "same_domain": same_domain})
    elif reason == "ambiguous_account":
        accts = list(ctx.get("account_candidates") or [])
        if crm is not None and lead.get("company"):
            try:
                rest = await crm.accounts_by_name(lead["company"])
                by_id = {a.get("id"): a for a in rest}
                accts = [{**a, **by_id.get(a.get("id"), {})} for a in accts] or rest
            except Exception as exc:  # noqa: BLE001
                ev.append(f"REST: accounts unreadable ({type(exc).__name__})")
        ev.append(f"REST: {len(accts)} accounts match company {lead.get('company')!r}: "
                  + ", ".join(str(a.get("name")) for a in accts))
        sites = {a.get("id"): str(a.get("website") or "").lower().removeprefix("www.") for a in accts}
        shared = [a.get("name") for a in accts if dom and sites.get(a.get("id")) == dom]
        if len(shared) > 1:
            ev.append(f"domain {dom} shared by {', '.join(str(s) for s in shared)}")
        country = str(lead.get("country") or "").strip().lower()
        if not country:
            ev.append("lead has no country to tell the accounts apart")
        for a in accts:
            ev.append(f"{a.get('name')}: website {a.get('website') or '-'}, country "
                      f"{a.get('billingAddressCountry') or a.get('country') or '-'}, owner {a.get('owner') or '-'}")
        # Something distinguishes exactly one account: its website is the lead's domain
        # while others' are not, or its country is the lead's country while others' are not.
        dom_hits = [a for a in accts if dom and sites.get(a.get("id")) == dom]
        ctry_hits = [a for a in accts if country and str(a.get("billingAddressCountry") or a.get("country") or "")
                     .strip().lower() == country]
        distinct = len(accts) <= 1 or len(dom_hits) == 1 or len(ctry_hits) == 1
        sig.update(accounts=len(accts), distinguishing=distinct)
        if not distinct:
            ev.append("nothing in the CRM distinguishes the accounts")
    elif reason in ("phone_only", "no_contact"):
        ev.append(f"row has no email; phone {lead.get('phone') or '-'}; company {lead.get('company') or '-'}")
        strategic = False
        if crm is not None and lead.get("company"):
            try:
                strategic = bool(await crm.accounts_by_name(lead["company"]))
            except Exception:  # noqa: BLE001
                pass
        ev.append("company is a known CRM account" if strategic else "company is not a known (strategic) account")
        ev.append("playbook: phone-only rows are skipped unless the company is a strategic account")
        sig["strategic"] = strategic
    else:
        ev.append(f"review reason {reason or '-'}; lead country {lead.get('country') or '-'}")
    return ev, sig


def _pb(name: str | None, playbook_dir: str | Path | None) -> playbook.Playbook | None:
    try:
        return playbook.load(name or playbook.DEFAULT_PLAYBOOK, playbook_dir)
    except Exception as exc:  # noqa: BLE001 - the reviewer still works without the playbook
        log.warning("playbook unavailable: %s", exc)
        return None


# ---------------------------------------------------------------------------
# decisions
# ---------------------------------------------------------------------------


async def decide_ambiguity(
    step: Step, inputs: dict[str, Any], cfg: RunConfig, *, crm: Any = None, pb: playbook.Playbook | None = None,
    instructions: str | None = None, layers: dict[str, bool] | None = None,
) -> Outcome:
    options = _opts(step)
    threshold = float(cfg.review_auto_threshold)
    evidence, sig = await ambiguity_evidence(inputs, crm)
    lead = _lead(inputs)
    reason = str(inputs.get("reason") or "")

    saved = escalations.saved_rule_for(pb.text, reason=reason, lead=lead) if pb is not None else None
    if saved is not None:
        ans, line = saved
        opt = next((o for o in options if o.value == ans), None)
        if opt is not None:
            dec, val = escalations.split_option(opt.value)
            d = ReviewDecision(decision=dec, value=val, confidence=1.0, threshold=threshold,
                               evidence=[f"playbook rule: {line}"] + evidence, options=options,
                               decided_by="meta-reviewer", model=None)
            return Outcome(d, True, tried=[f"applied saved playbook rule: {line}"], label=opt.label,
                           option=opt.value, source="rule")
        evidence.append(f"saved rule names {ans}, which is not an option here: ignored")

    tried = ["read the lead and the search result"] + (["read the CRM (REST, read-only)"] if crm else [])
    sections = pb.sections_for(step.kind) if pb is not None else []
    view = {k: inputs.get(k) for k in ("reason", "question", "lead", "options", "context")}
    view["evidence"] = evidence
    prompt = prompts.assemble("meta_reviewer", step, view, step.history[:-1], sections, ReviewDecision,
                              layers, instructions=instructions, fixture_key=step.lane or step.kind.value)
    try:
        d = await llm.complete("meta_reviewer", prompt.messages, ReviewDecision, run_id=step.run_id,
                               step_id=step.id, config=cfg)
        info = llm.last_call()
        model = info.model if info else None
    except llm.LLMError as exc:
        tried.append(f"asked the decision model: unavailable ({type(exc).__name__}: {str(exc)[:120]})")
        d = ReviewDecision(decision="skip", value=None, confidence=0.0, threshold=threshold, evidence=evidence,
                           options=options, decided_by="meta-reviewer", model=None)
        return Outcome(d, False, tried=tried, forced_reason="decision model unavailable", source="policy")
    tried.append(f"asked {model or 'the decision model'}: {d.decision}{':' + d.value if d.value else ''} "
                 f"at confidence {d.confidence:.2f} (threshold {threshold:.2f})")
    conf = max(0.0, min(1.0, float(d.confidence)))
    opt = match_option(d, options)
    model_ev = [e for e in d.evidence if e not in evidence]
    dec, val = (escalations.split_option(opt.value) if opt else (d.decision, d.value))
    decision = ReviewDecision(decision=dec, value=val, confidence=round(conf, 4), threshold=threshold,
                              evidence=evidence + model_ev, options=options, decided_by="meta-reviewer", model=model)
    forced = None
    if opt is None:
        forced = f"model answer {d.decision}{':' + d.value if d.value else ''} is not one of the options"
    elif reason == "ambiguous_account" and not sig.get("distinguishing", True) and dec == "link_account":
        forced = FORCED_ACCOUNTS
    auto = forced is None and conf >= threshold
    if forced:
        tried.append(f"policy: {forced}")
    return Outcome(decision, auto, tried=tried, forced_reason=forced, label=opt.label if opt else None,
                   option=opt.value if opt else None, source="llm")


async def decide_approval(step: Step, inputs: dict[str, Any], cfg: RunConfig, *,
                          pb: playbook.Playbook | None = None) -> Outcome:
    from . import judge
    from .checks.email import find_placeholders, policy_flags

    threshold = float(cfg.approval_auto_threshold)
    options = _opts(step) or [ReviewOption(label="Approve and send", value="approve"),
                              ReviewOption(label="Reject (do not send)", value="reject")]
    raw = inputs.get("drafts")
    if raw is None and isinstance(inputs.get("draft"), dict):
        raw = [inputs["draft"]]
    if raw is None and isinstance(inputs.get("items"), list):
        raw = drafts_from_items(inputs["items"])
    drafts = [d for d in (raw or []) if isinstance(d, dict)]
    evidence: list[str] = [f"{len(drafts)} draft(s) in the batch"]
    tried: list[str] = []
    problems: list[str] = []
    for i, dr in enumerate(drafts):
        did = str(dr.get("id", i))
        text = f"{dr.get('subject') or ''}\n{dr.get('body') or ''}"
        if dr.get("checks_ok") is False:
            problems.append(f"{did}: deterministic check failed ({dr.get('check_reason') or 'no reason'})")
        if not dr.get("to"):
            problems.append(f"{did}: no recipient")
        if find_placeholders(text):
            problems.append(f"{did}: placeholder text")
        flags = policy_flags(text, inputs.get("allowed_domains") or ())
        if flags:
            problems.append(f"{did}: policy flags " + ", ".join(f["policy"] for f in flags))
    evidence += problems or ["deterministic checks passed for every draft"]
    forced = None
    if not drafts:
        forced = "no drafts to approve"
    elif cfg.always_ask_human_email:
        forced = "always_ask_human_email is on: every external email needs a human"
    elif not cfg.llm_judge_enabled:
        forced = "llm_judge_enabled is off: no judge score to approve on"
    elif problems:
        forced = "deterministic checks failed"
    scores: dict[str, float] = {}
    model = None
    min_score = 0.0
    if drafts and not cfg.always_ask_human_email and cfg.llm_judge_enabled:
        try:
            verdicts, model = await judge.judge_drafts(drafts, run_id=step.run_id, step_id=step.id, config=cfg)
            scores = {k: round(v.score, 4) for k, v in verdicts.items()}
            min_score = min(scores.values()) if scores else 0.0
            flagged = {k: v.flags for k, v in verdicts.items() if v.flags}
            tried.append(f"batch judge ({model}): min score {min_score:.2f} over {len(scores)} draft(s)")
            evidence.append(f"judge scores: " + ", ".join(f"{k} {s:.2f}" for k, s in sorted(scores.items())))
            if flagged:
                forced = forced or "judge raised policy flags"
                evidence.append("judge flags: " + "; ".join(f"{k}: {', '.join(v)}" for k, v in flagged.items()))
        except llm.LLMError as exc:
            forced = forced or "judge unavailable"
            tried.append(f"batch judge unavailable ({type(exc).__name__})")
    auto = forced is None and min_score >= threshold
    dec = "approve" if auto else ("reject" if problems else "approve")
    d = ReviewDecision(decision=dec, value=None, confidence=round(min_score, 4), threshold=threshold,
                       evidence=evidence, options=options, decided_by="meta-reviewer", model=model)
    if forced:
        tried.append(f"policy: {forced}")
    return Outcome(d, auto, tried=tried, forced_reason=forced, option=dec,
                   label=next((o.label for o in options if o.value == dec), None), source="judge",
                   extra={"scores": scores, "drafts": [str(x.get("id", i)) for i, x in enumerate(drafts)]})


def drafts_from_items(items: list[Any]) -> list[dict[str, Any]]:
    """W3 integration: Track K's approval step lists `items` [{lane, draft_step,
    draft (resolved lead:<n>.draft fact), to, subject, approval_key}]; the judge
    and the checks above want flat drafts keyed by lane."""
    out: list[dict[str, Any]] = []
    for it in items:
        if not isinstance(it, dict) or not it.get("lane"):
            continue
        d = it.get("draft") if isinstance(it.get("draft"), dict) else {}
        out.append({**d, "id": it["lane"], "lane": it["lane"], "draft_step": it.get("draft_step"),
                    "to": d.get("to") or it.get("to"), "subject": d.get("subject") or it.get("subject"),
                    "body": d.get("body") or "", "checks_ok": None if d else False,
                    "check_reason": None if d else f"draft fact for {it['lane']} is not committed"})
    return out


def human_outcome(step: Step) -> Outcome | None:
    rec = step.inputs.get("human_decision")
    if not isinstance(rec, dict):
        return None
    d = ReviewDecision.model_validate({k: rec.get(k) for k in ReviewDecision.model_fields if k in rec})
    extra = {k: rec[k] for k in ("escalation_id", "answered_by", "reason") if rec.get(k) is not None}
    return Outcome(d, True, tried=["human answered the escalation"], label=rec.get("label"),
                   option=rec.get("option"), source="human", extra=extra)


# ---------------------------------------------------------------------------
# the service
# ---------------------------------------------------------------------------


class MetaReviewer(Worker):
    """worker_base.Worker for skill `review` with resolve-or-escalate outcomes."""

    def __init__(self, r: aioredis.Redis, keys: Keys, *, agent_id: str = "meta-reviewer", crm: Any = None,
                 playbook_dir: str | Path | None = None, secrets_keys: Keys | None = None,
                 use_crm: bool = True, **kw: Any) -> None:
        super().__init__(r, keys, meta_reviewer_card(agent_id), self._unused, **kw)
        self._crm = crm
        self._use_crm = use_crm
        self._secrets_keys = secrets_keys
        self.playbook_dir = playbook_dir

    @staticmethod
    async def _unused(step: Step, ctx: Any) -> Any:  # pragma: no cover - _work is overridden
        raise NotImplementedError

    async def crm(self) -> Any:
        if self._crm is None and self._use_crm:
            try:
                self._crm = await reader_from_redis(self.r, self._secrets_keys or self.keys)
            except (CrmError, Exception) as exc:  # noqa: BLE001 - decide without the CRM; evidence says so
                log.warning("no CRM reader: %s", exc)
        return self._crm

    async def aclose(self) -> None:
        if self._crm is not None and hasattr(self._crm, "aclose"):
            await self._crm.aclose()
        self._crm = None

    async def decide(self, step: Step, cfg: RunConfig) -> Outcome:
        human = human_outcome(step)
        if human is not None:
            return human
        inputs = await ledger.resolve_inputs(self.r, self.keys, step, strict=False)
        run = await ledger.get_run(self.r, self.keys, step.run_id)
        pb = _pb(run.playbook if run else None, self.playbook_dir)
        if step.kind == StepKind.REVIEW_APPROVAL:
            return await decide_approval(step, inputs, cfg, pb=pb)
        instructions, layers = None, None
        try:
            acfg = await agent_config.get_config(self.r, self.keys, self.agent_id)
            layers = agent_config.effective_layers(acfg)
            stored = await agent_config.get_prompt(self.r, self.keys, self.agent_id)
            instructions = stored.text if stored else None
        except Exception:  # noqa: BLE001 - defaults
            pass
        return await decide_ambiguity(step, inputs, cfg, crm=await self.crm(), pb=pb, instructions=instructions,
                                      layers=layers)

    async def _work(self, step: Step, fence: int, cfg: RunConfig) -> str:
        try:
            out = await self.decide(step, cfg)
        except Exception as exc:  # noqa: BLE001 - never lose a review: escalate with the error
            log.exception("%s: deciding %s failed", self.agent_id, step.id)
            d = ReviewDecision(decision="skip", confidence=0.0, threshold=cfg.review_auto_threshold,
                               options=_opts(step), decided_by="meta-reviewer")
            out = Outcome(d, False, tried=[f"reviewer error: {type(exc).__name__}: {exc}"[:200]],
                          forced_reason="reviewer error", source="policy")
        return await (self._resolve(step, fence, out) if out.auto else self._escalate(step, fence, out))

    async def _resolve(self, step: Step, fence: int, out: Outcome) -> str:
        rec = out.record()
        key = escalations.decision_key(step)
        approval = step.kind == StepKind.REVIEW_APPROVAL
        if approval:
            rec = await self._approval_items(step, rec)
            if out.source == "human":  # re-commit the human record with the per-email items
                await ledger.commit_fact(self.r, self.keys, step.run_id, key, rec, source_step=step.id,
                                         actor=self.agent_id)
        if out.source != "human":  # a human answer already committed its fact
            await ledger.commit_fact(self.r, self.keys, step.run_id, key, rec, source_step=step.id, actor=self.agent_id)
            await append_event(self.r, self.keys, Event(
                run_id=step.run_id, step_id=step.id, actor=self.agent_id,
                type=EventType.APPROVAL_AUTO if approval else EventType.REVIEW_RESOLVED,
                payload={**rec, "lane": step.lane, "kind": "approval" if approval else "ambiguity",
                         "title": step.title, "question": step.inputs.get("question"), "fact": key}))
        d = out.decision
        summary = (f"{d.decision}{' ' + (out.label or str(d.value)) if (out.label or d.value) else ''} "
                   f"(confidence {d.confidence:.2f} >= {d.threshold:.2f}, {out.source})")
        if out.source == "human":
            summary = f"{d.decision}{' ' + (out.label or str(d.value)) if (out.label or d.value) else ''} (human answer)"
        claim_data = {**rec, "value": out.option or rec.get("value"), "fact": key}
        try:
            await ledger.claim(self.r, self.keys, step.id, Claim(worker=self.agent_id, fence=fence, summary=summary,
                                                                data=claim_data, acted=False),
                               model=d.model)
        except (ledger.IllegalTransition, ledger.StaleFenceError) as exc:
            log.warning("%s: claim for %s refused: %s", self.agent_id, step.id, exc)
            return "claim_refused"
        return "resolved"

    async def _approval_items(self, step: Step, rec: dict[str, Any]) -> dict[str, Any]:
        """W3 integration with Track K (approval.py): a batch decision becomes one
        `approval:<lane>` fact per email (what the mailer and the send stage read,
        pinned to recipient, subject and draft step) plus the per-email `items`,
        `approved` / `rejected` lists on the batch record."""
        from . import approval as ap

        inputs = await ledger.resolve_inputs(self.r, self.keys, step, strict=False)
        items = [it for it in (inputs.get("items") or []) if isinstance(it, dict) and it.get("lane")]
        if not items:
            return rec
        dec = rec.get("decision")
        dec = dec if dec in (ap.APPROVE, ap.REJECT) else None
        scores = rec.get("scores") or {}
        for it in items:
            lane = it["lane"]
            d = it.get("draft") if isinstance(it.get("draft"), dict) else {}
            per = {"decision": dec, "lane": lane, "step": step.id, "draft_step": it.get("draft_step"),
                   "to": d.get("to") or it.get("to"), "subject": d.get("subject") or it.get("subject"),
                   "score": scores.get(lane), "flags": [], "confidence": rec.get("confidence"),
                   "threshold": rec.get("threshold"), "decided_by": rec.get("decided_by"),
                   "model": rec.get("model"), "source": rec.get("source"),
                   "escalation_id": rec.get("escalation_id")}
            await ledger.commit_fact(self.r, self.keys, step.run_id, ap.approval_key(lane), per,
                                     source_step=step.id, actor=self.agent_id)
        facts = await ledger.get_facts(self.r, self.keys, step.run_id)
        merged = {k.split(":", 1)[1]: v for k, v in facts.items()
                  if k.startswith("approval:") and k != ap.BATCH_KEY and isinstance(v, dict)}
        batch = ap.batch_record(merged, decided_by=str(rec.get("decided_by") or "meta-reviewer"), step_id=step.id,
                                model=rec.get("model"), threshold=rec.get("threshold"))
        return {**rec, "items": batch["items"], "approved": batch["approved"], "rejected": batch["rejected"],
                "lanes": [it["lane"] for it in items], "batch_decision": batch["decision"]}

    async def _escalate(self, step: Step, fence: int, out: Outcome) -> str:
        d = out.decision
        options = d.options or _opts(step)
        question = str(step.inputs.get("question") or step.title or "Please decide")
        if step.kind == StepKind.REVIEW_APPROVAL and not step.inputs.get("question"):
            question = f"Approve {len(out.extra.get('drafts') or [])} follow-up email(s) for sending?"
        proposal = {"decision": d.decision, "value": out.option or d.value, "label": out.label,
                    "evidence": d.evidence, "model": d.model, "forced": out.forced_reason,
                    "forced_reason": out.forced_reason}
        try:
            esc = await escalations.escalate(
                self.r, self.keys, step, fence=fence, actor=self.agent_id, question=question, options=options,
                tried=out.tried + [e for e in d.evidence if e not in out.tried], confidence=d.confidence,
                threshold=d.threshold, context="; ".join(d.evidence[:6]), decision=proposal)
        except (ledger.IllegalTransition, ledger.StaleFenceError) as exc:
            log.warning("%s: escalation of %s refused: %s", self.agent_id, step.id, exc)
            return "escalation_refused"
        log.info("%s: escalated %s (%s) as %s", self.agent_id, step.id, step.lane, esc.id)
        return "escalated"


__all__ = ["MetaReviewer", "Outcome", "ambiguity_evidence", "decide_ambiguity", "decide_approval",
           "human_outcome", "match_option", "meta_reviewer_card"]
