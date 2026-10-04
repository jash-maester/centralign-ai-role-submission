"""Facts committed for verified CRM steps (Track F; plans/01 §3 facts).

Per lead (prefix "lead:<n>", from step.lane, else the step's "fact:lead:<n>"
input ref, else the resolved lead's "row"):

  crm.search_contact  lead:<n>.lookup       {result, match_type, contact_id, candidates}
                      lead:<n>.contact_id   (when matched)  lead:<n>.owner, lead:<n>.crm_url
                      lead:<n>.routing      {owner, owner_reason, region, account_id, account_name}
  crm.create_contact  lead:<n>.contact_id, lead:<n>.action = "created", lead:<n>.owner, lead:<n>.crm_url
  crm.update_contact  lead:<n>.contact_id, lead:<n>.action = "updated" | "unchanged", lead:<n>.owner, .crm_url
  crm.create_task     lead:<n>.task_id, lead:<n>.task_due, lead:<n>.task_owner

Values the check read from REST (result.observed) win over the worker's claim;
the claim only fills what REST does not report (e.g. owner_reason). A step
with no lead prefix commits the same values under "step:<id>.<name>".
"""

from __future__ import annotations

import re
from typing import Any

from ..protocol import Claim, Step, StepKind
from ..postconditions import CheckResult
from ..verifier import register_facts

_LEAD_REF = re.compile(r"^fact:(lead:\d+)$")


def lead_prefix(step: Step) -> str:
    if step.lane and step.lane.startswith("lead:"):
        return step.lane
    lead = (step.inputs or {}).get("lead")
    if isinstance(lead, str) and (m := _LEAD_REF.match(lead)):
        return m.group(1)
    if isinstance(lead, dict) and lead.get("row") is not None:
        return f"lead:{lead['row']}"
    return f"step:{step.id}"


def _put(out: dict[str, Any], prefix: str, name: str, value: Any) -> None:
    if value is not None:
        out[f"{prefix}.{name}"] = value


@register_facts(StepKind.CRM_SEARCH_CONTACT)
def search_facts(step: Step, claim: Claim, result: CheckResult) -> dict[str, Any]:
    p, obs, data = lead_prefix(step), result.observed or {}, claim.data or {}
    out: dict[str, Any] = {}
    lookup = {
        "result": obs.get("result", data.get("result")),
        "match_type": obs.get("match_type", data.get("match_type")),
        "contact_id": obs.get("contact_id", data.get("contact_id")),
        "candidates": obs.get("candidates", data.get("candidates", [])),
    }
    _put(out, p, "lookup", lookup)
    if lookup["result"] == "matched":
        _put(out, p, "contact_id", lookup["contact_id"])
        _put(out, p, "crm_url", data.get("contact_url"))
        _put(out, p, "open_deal", data.get("open_deal"))
    _put(out, p, "owner", data.get("owner"))
    _put(out, p, "routing", {k: data.get(k) for k in ("owner", "owner_reason", "region", "account_id", "account_name")
                             if k in data} or None)
    return out


def _contact_facts(step: Step, claim: Claim, result: CheckResult, action: str) -> dict[str, Any]:
    p, obs, data = lead_prefix(step), result.observed or {}, claim.data or {}
    out: dict[str, Any] = {}
    _put(out, p, "contact_id", obs.get("contact_id") or data.get("contact_id"))
    _put(out, p, "action", action)
    _put(out, p, "owner", obs.get("owner") or data.get("owner"))
    _put(out, p, "crm_url", obs.get("url") or data.get("contact_url"))
    return out


@register_facts(StepKind.CRM_CREATE_CONTACT)
def create_contact_facts(step: Step, claim: Claim, result: CheckResult) -> dict[str, Any]:
    # "exists" (check-then-act found the record, e.g. after a takeover) still
    # means this step's contact is in the CRM: the plan only creates new leads.
    return _contact_facts(step, claim, result, "created")


@register_facts(StepKind.CRM_UPDATE_CONTACT)
def update_contact_facts(step: Step, claim: Claim, result: CheckResult) -> dict[str, Any]:
    action = "unchanged" if (claim.data or {}).get("action") == "unchanged" and not claim.acted else "updated"
    return _contact_facts(step, claim, result, action)


@register_facts(StepKind.CRM_CREATE_TASK)
def task_facts(step: Step, claim: Claim, result: CheckResult) -> dict[str, Any]:
    p, obs, data = lead_prefix(step), result.observed or {}, claim.data or {}
    out: dict[str, Any] = {}
    _put(out, p, "task_id", obs.get("task_id") or data.get("task_id"))
    _put(out, p, "task_due", obs.get("due") or data.get("due"))
    _put(out, p, "task_owner", obs.get("owner") or data.get("owner"))
    _put(out, p, "task_url", obs.get("url") or data.get("task_url"))
    return out
