"""Plug-in point for the worker loop (Track I): step kind + inputs -> skill call -> Claim.

    result = await execute(op, StepKind.CRM_CREATE_CONTACT, step.inputs, label=f"{step.id}_a{step.attempt}")
    claim = to_claim(result, worker=op.agent_id, fence=fence)
    observations = result.observations  # one step.observation event each

Accepted input keys per kind (aliases in brackets):
- crm.search_contact : email | name [+ company]
- crm.create_contact : first_name [firstName], last_name [lastName], email [emailAddress],
                       phone [phoneNumber], title, account_name [company, accountName],
                       owner [owner_user_name, assigned_user]
- crm.update_contact : contact_id [id], secondary_email [email], phone, title
- crm.create_task    : contact_id, subject [name], due_date [due], owner [owner_user_name]
Values must already be resolved (no "fact:" refs); the worker resolves them.
"""

from __future__ import annotations

from typing import Any

from ledger_core.protocol import Claim, StepKind

from .evidence import SkillResult
from .operator import Operator


def _pick(inputs: dict[str, Any], *names: str) -> Any:
    for n in names:
        v = inputs.get(n)
        if v not in (None, ""):
            return v
    return None


def _need(inputs: dict[str, Any], *names: str) -> Any:
    v = _pick(inputs, *names)
    if v is None:
        raise ValueError(f"missing input {names[0]!r} (any of {names})")
    return v


async def execute(
    op: Operator, kind: StepKind | str, inputs: dict[str, Any], *, label: str | None = None,
    check_then_act: bool = True, create_missing_account: bool = True,
) -> SkillResult:
    """check_then_act=False skips the write skills' existence check (demo of duplicates);
    create_missing_account=False links only an Account that already exists."""
    kind = StepKind(kind)
    if kind is StepKind.CRM_SEARCH_CONTACT:
        email = _pick(inputs, "email", "emailAddress")
        if email:
            return await op.search_contact(email, label=label)
        return await op.search_by_name_company(
            _need(inputs, "name", "full_name"), _pick(inputs, "company", "account_name"), label=label
        )
    if kind is StepKind.CRM_CREATE_CONTACT:
        return await op.create_contact(
            _need(inputs, "first_name", "firstName"),
            _need(inputs, "last_name", "lastName"),
            _pick(inputs, "email", "emailAddress"),
            phone=_pick(inputs, "phone", "phoneNumber"),
            title=_pick(inputs, "title"),
            account_name=_pick(inputs, "account_name", "company", "accountName"),
            owner_user_name=_pick(inputs, "owner", "owner_user_name", "assigned_user"),
            label=label,
            check=check_then_act,
            create_missing_account=create_missing_account,
        )
    if kind is StepKind.CRM_UPDATE_CONTACT:
        return await op.update_contact(
            _need(inputs, "contact_id", "id"),
            secondary_email=_pick(inputs, "secondary_email", "email"),
            phone=_pick(inputs, "phone", "phoneNumber"),
            title=_pick(inputs, "title"),
            label=label,
        )
    if kind is StepKind.CRM_CREATE_TASK:
        return await op.create_task(
            _need(inputs, "contact_id"),
            _need(inputs, "subject", "name"),
            _need(inputs, "due_date", "due"),
            _pick(inputs, "owner", "owner_user_name"),
            label=label,
            check=check_then_act,
        )
    raise ValueError(f"browser operator cannot execute {kind}")


def summarize(result: SkillResult) -> str:
    if not result.ok:
        return f"{result.skill} failed: {result.reason}"
    verb = "done" if result.acted else "already done (no action)"
    return f"{result.skill} {verb}; record {result.record_id or '-'}"


def to_claim(result: SkillResult, *, worker: str, fence: int) -> Claim:
    """The claim a worker files (only for ok results; failures are reported, not claimed)."""
    data = {k: v for k, v in result.data.items() if k not in ("label",)}
    data["record_id"] = result.record_id
    return Claim(
        worker=worker,
        fence=fence,
        summary=summarize(result),
        data=data,
        evidence=list(result.screenshots),
        acted=result.acted,
    )
