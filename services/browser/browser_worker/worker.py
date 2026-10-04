"""browser.espocrm on the ledger (Track I, plans/03 Phase 4).

    op = Operator(agent_id); await op.start()
    handler = BrowserHandler(op)
    Worker(r, keys, browser_card(agent_id), handler).run()

worker_base owns the lease, the fence, heartbeats and the claim. This module
turns one leased CRM step into one Track C skill call:

  step.inputs (planner / api.espocrm shape: lead{...}, owner, contact_id,
  subject | event_name, due | event_date ...)
    -> normalize_inputs()            flat dispatch keys
    -> dispatch.execute()            check-then-act skill in Chromium
    -> ctx.observe() per observation (step.observation events)
    -> WorkResult: claim data shaped like the api.espocrm handlers' data
       (what checks/crm.py compares), before/after screenshots as evidence.

Faults (Keys.faults, field "<fault>:browser.espocrm" before "<fault>"):
- expire_session (F3): one shot clears the context's cookies before the skill;
  the skill then meets the login page, re-authenticates and carries on.
  "on" is treated as one shot too (the field is removed once consumed).
- ui_changed (F5): breaks the Save selector for this step only.
- false_claim (F2) is handled by worker_base (handler skipped).

LLM use is optional and limited to the recovery chooser: choosing one of the
fixed RecoveryAction values for an unexpected page (BROWSER_LLM_RECOVERY=on).
Any LLM error falls back to the deterministic default chooser.

A skill that gives up is not an exception: the worker files a claim with
data.blocked=True and acted=False. The verifier checks the CRM and rejects it
(nothing there) or commits it (the work is in fact done); the rejection reason
and the "give_up" observation are what a replan to api.espocrm works from.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
from typing import Any, Literal

from pydantic import BaseModel

from ledger_core import faults, llm
from ledger_core.crm_api import split_name
from ledger_core.dates import follow_up_due
from ledger_core.events import append_event
from ledger_core.protocol import AgentCard, Event, EventType, FaultName, Skill, Step, StepKind
from ledger_core.settings import get_settings
from ledger_core.worker_base import WorkContext, WorkResult, take_fault_shot, worker_card

from . import dispatch
from . import selectors as sel
from .evidence import SkillResult
from .formats import norm_email, to_e164
from .operator import Operator
from .recovery import PageState, RecoveryAction, default_chooser

log = logging.getLogger("ledger.browser_worker")

SKILL = Skill.BROWSER_ESPOCRM
KINDS = (StepKind.CRM_SEARCH_CONTACT, StepKind.CRM_CREATE_CONTACT, StepKind.CRM_UPDATE_CONTACT,
         StepKind.CRM_CREATE_TASK)


class BrowserInputError(ValueError):
    """The step's inputs are not enough for the browser to act (replan / review)."""


def browser_card(agent_id: str) -> AgentCard:
    return worker_card(
        agent_id, "EspoCRM browser operator", {SKILL.value: [k.value for k in KINDS]},
        side_effects=True, model_role="worker", container=agent_id,
        tools=[
            {"id": "playwright", "type": "browser", "name": "Chromium (Playwright) on the EspoCRM UI",
             "detail": "search, create/update contact, create task; check-then-act; screenshots"},
            {"id": "crm-rest", "type": "rest", "name": "EspoCRM REST", "enabled": False,
             "locked_reason": "Workers act through the UI only; REST is the verifier's channel"},
        ],
    )


# ---------------------------------------------------------------- inputs
def _first(v: Any) -> Any:
    return v[0] if isinstance(v, list) and v else (None if isinstance(v, list) else v)


def normalize_inputs(kind: StepKind | str, inputs: dict[str, Any]) -> dict[str, Any]:
    """Planner / api.espocrm inputs -> the flat keys dispatch.execute reads."""
    kind = StepKind(kind)
    lead = dict(inputs.get("lead") or {})
    flat = {**lead, **{k: v for k, v in inputs.items() if k != "lead" and v not in (None, "")}}
    out: dict[str, Any] = {}
    if kind is StepKind.CRM_SEARCH_CONTACT:
        first, last = split_name(flat)
        out = {"email": flat.get("email"), "name": " ".join(p for p in (first, last) if p) or None,
               "company": flat.get("company") or flat.get("account_name"), "phone": flat.get("phone"),
               "threshold": float(flat.get("threshold", 0.85))}
    elif kind is StepKind.CRM_CREATE_CONTACT:
        email = flat.get("email") or flat.get("emailAddress")
        if not email:
            raise BrowserInputError("crm.create_contact needs lead.email (phone-only rows are skipped per playbook)")
        first, last = split_name(flat)
        owner = flat.get("owner") or flat.get("owner_user_name") or flat.get("assigned_user")
        if not owner:
            raise BrowserInputError(f"crm.create_contact for {email} needs an owner (route it in the search step)")
        out = {"first_name": first, "last_name": last, "email": email, "phone": flat.get("phone"),
               "title": flat.get("title"), "owner": owner,
               "account_name": flat.get("account_name") or flat.get("account") or flat.get("company")}
    elif kind is StepKind.CRM_UPDATE_CONTACT:
        if not flat.get("contact_id"):
            raise BrowserInputError("crm.update_contact needs contact_id")
        fill = inputs.get("fill") or {}
        out = {"contact_id": flat["contact_id"],
               "secondary_email": _first(inputs.get("add_emails")) or flat.get("secondary_email") or lead.get("email"),
               "phone": _first(inputs.get("add_phones")) or lead.get("phone") or inputs.get("phone"),
               "title": fill.get("title") or lead.get("title") or inputs.get("title")}
    elif kind is StepKind.CRM_CREATE_TASK:
        if not flat.get("contact_id"):
            raise BrowserInputError("crm.create_task needs contact_id")
        subject = flat.get("subject") or (f"Follow up: {flat['event_name']}" if flat.get("event_name") else None)
        if not subject:
            raise BrowserInputError("crm.create_task needs subject or event_name")
        due = flat.get("due") or flat.get("due_date")
        if not due and flat.get("event_date"):
            due = follow_up_due(flat["event_date"]).isoformat()
        if not due:
            raise BrowserInputError("crm.create_task needs due or event_date")
        out = {"contact_id": flat["contact_id"], "subject": subject, "due_date": str(due)[:10],
               "owner": flat.get("owner") or flat.get("owner_user_name")}
    else:
        raise BrowserInputError(f"browser.espocrm cannot execute {kind}")
    return {k: v for k, v in out.items() if v not in (None, "")}


# ---------------------------------------------------------------- claim data
def contact_url(contact_id: str | None) -> str | None:
    return f"{get_settings().crm_public_url.rstrip('/')}/#Contact/view/{contact_id}" if contact_id else None


def is_probable(c: dict[str, Any], threshold: float, phone: str | None) -> bool:
    """Same rule as crm_api.is_probable: same company and (same phone or name >= threshold)."""
    phone_match = bool(phone and c.get("phone") and (to_e164(c["phone"]) or c["phone"]) == (to_e164(phone) or phone))
    c["phone_match"] = phone_match
    return float(c.get("company_score") or 0) >= 0.9 and (phone_match or float(c.get("name_score") or 0) >= threshold)


def search_claim(inputs: dict[str, Any], by_email: SkillResult | None, by_name: SkillResult | None) -> dict[str, Any]:
    """crm.search_contact claim data, same vocabulary as api.espocrm (crm.lookup_matches)."""
    if by_email is not None:
        hits = by_email.data.get("candidates") or []
        if hits:
            return {"result": "matched" if len(hits) == 1 else "ambiguous", "match_type": "email",
                    "contact_id": hits[0]["id"] if len(hits) == 1 else None,
                    "candidates": [{"id": h["id"], "name": h.get("name"), "accountName": h.get("account"),
                                    "matched_on": h.get("matched_on"), "score": 1.0} for h in hits],
                    "contact_url": contact_url(hits[0]["id"]) if len(hits) == 1 else None}
    probable = []
    if by_name is not None:
        thr = float(inputs.get("threshold", 0.85))
        probable = [c for c in (by_name.data.get("candidates") or []) if is_probable(c, thr, inputs.get("phone"))]
    return {"result": "ambiguous" if probable else "none", "match_type": "fuzzy" if probable else None,
            "contact_id": None,
            "candidates": [{"id": c["id"], "name": c.get("name"), "accountName": c.get("account"),
                            "score": c.get("score"), "name_score": c.get("name_score"),
                            "company_score": c.get("company_score"), "phone_match": c.get("phone_match")}
                           for c in probable]}


def claim_data(kind: StepKind | str, inputs: dict[str, Any], res: SkillResult) -> dict[str, Any]:
    """Claim.data for a successful write skill, shaped like crm_api_skill's data."""
    kind = StepKind(kind)
    rid = res.record_id
    if kind is StepKind.CRM_CREATE_CONTACT:
        return {"contact_id": rid, "action": "created" if res.acted else "exists",
                "email": norm_email(inputs.get("email")), "owner": res.data.get("owner") or inputs.get("owner"),
                "account": res.data.get("account"), "contact_url": contact_url(rid)}
    if kind is StepKind.CRM_UPDATE_CONTACT:
        planned = res.data.get("planned") or {}
        return {"contact_id": rid or inputs.get("contact_id"), "action": "updated" if res.acted else "unchanged",
                "changed": sorted(planned) if res.acted else [], "skipped": res.data.get("skipped") or {},
                "contact_url": contact_url(rid or inputs.get("contact_id"))}
    if kind is StepKind.CRM_CREATE_TASK:
        return {"task_id": rid, "contact_id": inputs.get("contact_id"), "subject": inputs.get("subject"),
                "due": inputs.get("due_date"), "owner": res.data.get("owner") or inputs.get("owner"),
                "action": "created" if res.acted else "exists"}
    raise ValueError(f"no claim shape for {kind}")


# ---------------------------------------------------------------- LLM recovery
class RecoveryChoice(BaseModel):
    action: Literal["reload", "relogin", "home", "give_up"]
    reason: str = ""


RECOVERY_SYSTEM = (
    "You are the recovery policy of a browser operator working in the EspoCRM web UI. "
    "A scripted skill met a page it did not expect. Choose exactly ONE action: "
    "reload (reload the page), relogin (log in again), home (go to the home page), "
    "give_up (stop; the step is reported as blocked). Prefer relogin when the page is the login form, "
    "give_up after repeated failures. Answer as JSON {\"action\": ..., \"reason\": ...}."
)


def llm_recovery_enabled() -> bool:
    return os.environ.get("BROWSER_LLM_RECOVERY", "off").lower() in ("1", "on", "true", "yes")


class LLMRecoveryChooser:
    """Operator recovery_chooser backed by llm.complete(role="worker"). The answer is a
    RecoveryAction by schema; any LLM failure falls back to the default chooser."""

    def __init__(self) -> None:
        self.step: Step | None = None
        self.calls: list[dict[str, Any]] = []

    async def __call__(self, state: PageState, attempt: int, problem: str) -> RecoveryAction:
        step = self.step
        messages = [
            {"role": "system", "content": RECOVERY_SYSTEM},
            {"role": "user", "content": json.dumps({
                "step_kind": str(step.kind) if step else None, "page_state": state.value, "attempt": attempt,
                "problem": problem[:300], "allowed": [a.value for a in RecoveryAction]})},
        ]
        try:
            out = await llm.complete("worker", messages, RecoveryChoice,
                                     run_id=step.run_id if step else None, step_id=step.id if step else None)
            action = RecoveryAction(out.action)
            self.calls.append({"state": state.value, "attempt": attempt, "action": action.value,
                               "model": getattr(llm.last_call(), "model", None)})
            return action
        except Exception as e:  # noqa: BLE001 - budget, outage, bad output: deterministic policy
            log.warning("LLM recovery chooser failed (%s); default policy", e)
            action = default_chooser(state, attempt, problem)
            self.calls.append({"state": state.value, "attempt": attempt, "action": action.value, "fallback": str(e)})
            return action


# ---------------------------------------------------------------- the handler
def _pause_spec() -> tuple[str, float, str | None] | None:
    """LEDGER_BROWSER_PAUSE="<checkpoint>:<seconds>[:<step kind>]" — a demo/test knob that
    holds the step at a checkpoint (e.g. after_save) so `make chaos-kill-browser` can land
    mid-step. Off unless set."""
    raw = os.environ.get("LEDGER_BROWSER_PAUSE", "").strip()
    if not raw:
        return None
    parts = raw.split(":", 2)
    return parts[0], float(parts[1]) if len(parts) > 1 else 10.0, parts[2] if len(parts) > 2 else None


class BrowserHandler:
    """worker_base Handler for skill browser.espocrm."""

    def __init__(self, op: Operator, *, llm_chooser: LLMRecoveryChooser | None = None) -> None:
        self.op = op
        self.llm_chooser = llm_chooser
        if llm_chooser is None and llm_recovery_enabled():
            self.llm_chooser = LLMRecoveryChooser()
        if self.llm_chooser is not None:
            op.recovery_chooser = self.llm_chooser
        self.current: Step | None = None
        pause = _pause_spec()
        if pause:
            name, secs, kind = pause

            async def hold(_op: Operator, _name: str) -> None:
                if self.current is not None and (kind is None or str(self.current.kind) == kind):
                    log.warning("%s: pausing %.0fs at %s (LEDGER_BROWSER_PAUSE)", op.agent_id, secs, name)
                    await asyncio.sleep(secs)

            op.on_checkpoint(name, hold)

    async def _fault(self, step: Step, ctx: WorkContext, fault: FaultName) -> bool:
        shot = await take_fault_shot(ctx.r, ctx.keys, fault, skill=SKILL.value)
        if not shot:
            return False
        if await ctx.r.hget(ctx.keys.faults, shot) == "on":
            await ctx.r.hdel(ctx.keys.faults, shot)  # browser faults are one-shot even when "on"
        await faults.attach_pending(ctx.r, ctx.keys, fault, run_id=step.run_id, step_id=step.id)
        await append_event(ctx.r, ctx.keys, Event(
            run_id=step.run_id, step_id=step.id, actor=ctx.agent_id, type=EventType.FAULT_INJECTED,
            payload={"fault": fault.value, "switch": shot, "phase": "consumed", "agent_id": ctx.agent_id}))
        return True

    async def __call__(self, step: Step, ctx: WorkContext) -> WorkResult:
        kind = StepKind(step.kind)
        inputs = normalize_inputs(kind, ctx.inputs)
        label = f"{step.id}_a{step.attempt}"
        self.current = step
        if self.llm_chooser is not None:
            self.llm_chooser.step = step
        ui_changed = False
        try:
            await self.op.start()
            if await self._fault(step, ctx, FaultName.EXPIRE_SESSION):
                await self.op.expire_session()
                await ctx.observe({"page": "fault", "seen": "CRM session cookies cleared (expire_session)"})
            if await self._fault(step, ctx, FaultName.UI_CHANGED):
                ui_changed = True
                sel.set_ui_changed(True)
                await ctx.observe({"page": "fault", "seen": "Save selector broken (ui_changed)"})
            return await self._run(kind, inputs, label, ctx)
        finally:
            if ui_changed:
                sel.set_ui_changed(False)
            self.current = None

    async def _skill(self, kind: StepKind, inputs: dict[str, Any], label: str, ctx: WorkContext) -> SkillResult:
        res = await dispatch.execute(self.op, kind, inputs, label=label,
                                     check_then_act=ctx.config.check_then_act, create_missing_account=False)
        for obs in res.observations:
            await ctx.observe(obs)
        return res

    async def _run(self, kind: StepKind, inputs: dict[str, Any], label: str, ctx: WorkContext) -> WorkResult:
        results: list[SkillResult] = []
        if kind is StepKind.CRM_SEARCH_CONTACT:
            by_email = by_name = None
            if inputs.get("email"):
                by_email = await self._skill(kind, {"email": inputs["email"]}, f"{label}_email", ctx)
                results.append(by_email)
            if by_email is None or (by_email.ok and not by_email.data.get("candidates")):
                if inputs.get("name"):
                    by_name = await self._skill(kind, {"name": inputs["name"], "company": inputs.get("company")},
                                                f"{label}_name", ctx)
                    results.append(by_name)
            failed = next((x for x in results if not x.ok), None)
            if failed is None:
                data = search_claim(inputs, by_email, by_name)
                data.update(channel="browser", recoveries=sum(x.data.get("recoveries", 0) for x in results))
                summary = {"matched": f"found contact {data['contact_id']} in the CRM UI",
                           "ambiguous": f"{len(data['candidates'])} probable match(es) need review",
                           "none": "no existing contact in the CRM UI"}[data["result"]]
                return self._result(summary, data, results, acted=False)
        else:
            res = await self._skill(kind, inputs, label, ctx)
            results.append(res)
            failed = None if res.ok else res
            if failed is None:
                data = claim_data(kind, inputs, res)
                data.update(channel="browser", record_id=res.record_id, recoveries=res.data.get("recoveries", 0))
                return self._result(dispatch.summarize(res), data, results, acted=res.acted)
        assert failed is not None
        return self._result(
            f"blocked: {failed.skill} gave up: {failed.reason}",
            {"blocked": True, "reason": failed.reason, "skill": failed.skill, "channel": "browser",
             "fallback_skill": Skill.API_ESPOCRM.value, "recoveries": failed.data.get("recoveries", 0)},
            results, acted=any(x.acted for x in results))

    def _result(self, summary: str, data: dict[str, Any], results: list[SkillResult], *, acted: bool) -> WorkResult:
        evidence = [p for x in results for p in x.screenshots]
        model = None
        if self.llm_chooser is not None and self.llm_chooser.calls:
            data["llm_recovery"] = list(self.llm_chooser.calls)
            model = next((c.get("model") for c in reversed(self.llm_chooser.calls) if c.get("model")), None)
            self.llm_chooser.calls.clear()
        return WorkResult(summary=summary, data=data, evidence=evidence, acted=acted, model=model)
