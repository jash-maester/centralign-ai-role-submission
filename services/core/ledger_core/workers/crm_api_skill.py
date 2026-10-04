"""api.espocrm skill handlers (C11 fallback; also the Phase 2 temporary CRM path).

Plain async functions; Track F wires them into a worker service:

    out = await handle(kind, inputs, writer=writer, reader=None, check_then_act=True)
    Claim(summary=out["summary"], data=out["data"], acted=out["acted"], ...)

`writer` is a CrmWriter (writer_api_key). `reader` is optional and defaults to
the writer: workers never hold the verifier's read-only key.

Every write is check-then-act: if the postcondition already holds (contact with
that email exists, email already on the contact, task with that subject already
linked) the handler returns acted=False and writes nothing, so a retry or a
takeover after a crash never duplicates.

Inputs (lead = {email, name | first_name + last_name, company, phone, title, country}):
  crm.search_contact  lead, threshold (0.85)
  crm.create_contact  lead, owner (userName; routed when absent), account_id
  crm.update_contact  contact_id, lead (its email/phone become secondary; empty
                      title filled) or add_emails / add_phones / fill
  crm.create_task     contact_id, subject | event_name, due | event_date, owner
                      (default: the contact's owner), description
All accept idempotency_key, echoed in the claim data.
"""

from __future__ import annotations

from typing import Any

from ..crm_api import CrmReader, CrmWriter, fillable, normalize_email, normalize_phone, split_name
from ..dates import follow_up_due
from ..routing import owner_for


class SkillInputError(ValueError):
    """The step's inputs are not enough to act. Not retryable without a replan."""


def _lead(inputs: dict[str, Any]) -> dict[str, Any]:
    return inputs.get("lead") or inputs


async def _route(reader: CrmReader, lead: dict[str, Any], existing: dict | None = None) -> dict[str, Any]:
    accounts = await reader.accounts_by_name(lead.get("company") or "")
    decision = owner_for(lead, existing, accounts)
    return {**decision.to_dict(), "accounts": [a["id"] for a in accounts]}


async def search_contact(inputs: dict[str, Any], writer: CrmWriter, reader: CrmReader, check_then_act: bool) -> dict:
    lead = _lead(inputs)
    found = await reader.lookup(lead, float(inputs.get("threshold", 0.85)))
    existing = await reader.get_contact(found["contact_id"]) if found["contact_id"] else None
    routing = await _route(reader, lead, existing)
    data = {
        **found,
        "owner": routing["owner"],
        "owner_reason": routing["reason"],
        "region": routing["region"],
        "account_id": routing["account_id"],
        "account_name": routing["account_name"],
        "accounts": routing["accounts"],
        "account_candidates": routing["candidates"],
    }
    if existing:
        data["contact_url"] = existing["url"]
        data["open_deal"] = bool(await reader.open_opportunities_for_contact(existing["id"]))
    summary = {
        "matched": f"found contact {found['contact_id']} by email",
        "ambiguous": f"{len(found['candidates'])} probable match(es) need review",
        "none": "no existing contact",
    }[found["result"]]
    if routing["reason"] == "ambiguous_account":
        summary += f"; company matches {len(routing['accounts'])} accounts"
    return {"acted": True, "summary": summary, "data": data}


async def create_contact(inputs: dict[str, Any], writer: CrmWriter, reader: CrmReader, check_then_act: bool) -> dict:
    lead = _lead(inputs)
    email = normalize_email(lead.get("email"))
    if not email:
        raise SkillInputError("crm.create_contact needs lead.email (phone-only rows are skipped per playbook)")
    if check_then_act:
        hits = await reader.find_contacts_by_email(email)
        if hits:
            c = hits[0]
            return {
                "acted": False,
                "summary": f"contact {c['id']} with {email} already exists; nothing written",
                "data": {"contact_id": c["id"], "action": "exists", "email": email, "owner": c.get("owner"),
                         "account_id": c.get("accountId"), "contact_url": c["url"],
                         "idempotency_key": inputs.get("idempotency_key")},
            }
    owner, account_id = inputs.get("owner"), inputs.get("account_id")
    routing = None
    if not owner:
        routing = await _route(reader, lead)
        if routing["owner"] is None:
            raise SkillInputError(f"no owner for {email}: {routing['reason']} (needs review)")
        owner, account_id = routing["owner"], account_id or routing["account_id"]
    first, last = split_name(lead)
    c = await writer.create_contact(
        first_name=first, last_name=last, email=email, phone=lead.get("phone"), title=lead.get("title"),
        account_id=account_id, owner=owner,
    )
    return {
        "acted": True,
        "summary": f"created contact {c['id']} {c.get('name')} <{email}> owner {owner}",
        "data": {"contact_id": c["id"], "action": "created", "email": email, "owner": owner,
                 "account_id": c.get("accountId"), "contact_url": c.get("url") or writer.contact_url(c["id"]),
                 "owner_reason": routing["reason"] if routing else "given",
                 "idempotency_key": inputs.get("idempotency_key")},
    }


async def update_contact(inputs: dict[str, Any], writer: CrmWriter, reader: CrmReader, check_then_act: bool) -> dict:
    contact_id = inputs.get("contact_id")
    if not contact_id:
        raise SkillInputError("crm.update_contact needs contact_id")
    lead = inputs.get("lead") or {}
    add_emails = list(inputs.get("add_emails") or []) + ([lead["email"]] if lead.get("email") else [])
    add_phones = list(inputs.get("add_phones") or []) + ([lead["phone"]] if lead.get("phone") else [])
    fill = dict(inputs.get("fill") or {})
    if lead.get("title"):
        fill.setdefault("title", lead["title"])
    current = await reader.get_contact(contact_id)
    if current is None:
        raise SkillInputError(f"contact {contact_id} does not exist")
    if check_then_act:
        missing = [e for e in add_emails if normalize_email(e) not in current["emails"]]
        missing += [p for p in add_phones if normalize_phone(p) and normalize_phone(p) not in current["phones"]]
        missing += [k for k, v in fill.items() if v and fillable(current, k)]
        if not missing:
            return {
                "acted": False,
                "summary": f"contact {contact_id} already has everything; nothing written",
                "data": {"contact_id": contact_id, "action": "unchanged", "changed": [], "owner": current.get("owner"),
                         "emails": current["emails"], "contact_url": current["url"],
                         "idempotency_key": inputs.get("idempotency_key")},
            }
    res = await writer.update_contact(contact_id, add_emails=add_emails, add_phones=add_phones, fill=fill)
    c = res["contact"]
    return {
        "acted": bool(res["changed"]),
        "summary": f"updated contact {contact_id}: " + (", ".join(res["changed"]) or "no change"),
        "data": {"contact_id": contact_id, "action": "updated" if res["changed"] else "unchanged",
                 "changed": res["changed"], "owner": c.get("owner"), "emails": c.get("emails"),
                 "contact_url": c.get("url"), "idempotency_key": inputs.get("idempotency_key")},
    }


async def create_task(inputs: dict[str, Any], writer: CrmWriter, reader: CrmReader, check_then_act: bool) -> dict:
    contact_id = inputs.get("contact_id")
    if not contact_id:
        raise SkillInputError("crm.create_task needs contact_id")
    subject = inputs.get("subject") or (f"Follow up: {inputs['event_name']}" if inputs.get("event_name") else None)
    if not subject:
        raise SkillInputError("crm.create_task needs subject or event_name")
    due = inputs.get("due") or (follow_up_due(inputs["event_date"]).isoformat() if inputs.get("event_date") else None)
    if not due:
        raise SkillInputError("crm.create_task needs due or event_date")
    due = str(due)[:10]
    contact = await reader.get_contact(contact_id)
    if contact is None:
        raise SkillInputError(f"contact {contact_id} does not exist")
    owner = inputs.get("owner") or contact.get("owner")
    if not owner:
        raise SkillInputError(f"no owner for task on contact {contact_id}")
    if check_then_act:
        same = [t for t in await reader.tasks_for_contact(contact_id)
                if (t.get("name") or "").strip().lower() == subject.strip().lower()]
        if same:
            t = same[0]
            return {
                "acted": False,
                "summary": f"task {t['id']} {subject!r} already on contact {contact_id}; nothing written",
                "data": {"task_id": t["id"], "contact_id": contact_id, "subject": subject,
                         "due": t.get("dateEndDate"), "owner": t.get("owner"), "action": "exists",
                         "idempotency_key": inputs.get("idempotency_key")},
            }
    t = await writer.create_task(contact_id=contact_id, subject=subject, due=due, owner=owner,
                                 description=inputs.get("description"))
    return {
        "acted": True,
        "summary": f"created task {t['id']} {subject!r} due {due} owner {owner} on contact {contact_id}",
        "data": {"task_id": t["id"], "contact_id": contact_id, "subject": subject, "due": due, "owner": owner,
                 "action": "created", "task_url": writer.task_url(t["id"]),
                 "idempotency_key": inputs.get("idempotency_key")},
    }


HANDLERS = {
    "crm.search_contact": search_contact,
    "crm.create_contact": create_contact,
    "crm.update_contact": update_contact,
    "crm.create_task": create_task,
}


async def handle(kind: str, inputs: dict[str, Any], *, writer: CrmWriter, reader: CrmReader | None = None,
                 check_then_act: bool = True) -> dict[str, Any]:
    """Run one CRM step through REST. Returns {"acted", "summary", "data"}."""
    fn = HANDLERS.get(str(kind))
    if fn is None:
        raise SkillInputError(f"api.espocrm cannot execute kind {kind!r}")
    return await fn(inputs, writer, reader or writer, check_then_act)
