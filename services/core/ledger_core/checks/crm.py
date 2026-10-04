"""CRM postconditions (plans/01-architecture.md §8). Read through EspoCRM REST
with the verifier's read-only key (ctx.crm: CrmReader), never through the
channel the worker acted on, and never trusting the claim beyond comparing it.

Arguments (Postcondition.args / .expect), all optional unless noted:

crm.contact_exists  args   email (required), contact_id
                    expect contact_id, account (name) | account_id, owner (userName),
                           first_name, last_name, title, emails [..], phones [..]
crm.no_duplicate    args   email | emails [..]
                    expect count (default 1)
crm.task_exists     args   contact_id | email, subject
                    expect subject, due (YYYY-MM-DD), owner (userName), allow_multiple
crm.lookup_matches  args   lead {email, name|first_name+last_name, company, phone} (or those keys
                           flat in args), threshold (default 0.85)
                    claim  result matched|none|ambiguous, contact_id, candidates [ids|{id}],
                           accounts [ids] (optional: the account matches the claim relied on)

The claim (ctx.claim, the worker's Claim.data) is only ever compared with
what REST shows: a claimed id that REST does not confirm fails the check.
"""

from __future__ import annotations

from typing import Any

from ..crm_api import CrmError, CrmReader, normalize_email, normalize_phone
from ..postconditions import CheckContext, CheckResult, register


def _reader(ctx: CheckContext) -> CrmReader | None:
    return ctx.crm if isinstance(ctx.crm, CrmReader) else None


def _claim(ctx: CheckContext) -> dict[str, Any]:
    return ctx.claim or {}


def _no_reader() -> CheckResult:
    return CheckResult(False, "no CRM reader in check context (verifier must pass ctx.crm)")


def _contact_summary(c: dict[str, Any]) -> dict[str, Any]:
    return {
        "contact_id": c["id"],
        "name": c.get("name"),
        "emails": c.get("emails"),
        "phones": c.get("phones"),
        "account": c.get("accountName"),
        "account_id": c.get("accountId"),
        "owner": c.get("owner"),
        "title": c.get("title"),
        "url": c.get("url"),
    }


def _mismatches(c: dict[str, Any], expect: dict[str, Any]) -> list[str]:
    out: list[str] = []

    def cmp(label: str, want: Any, got: Any, norm=lambda v: (v or "").strip().lower()) -> None:
        if want is not None and norm(want) != norm(got):
            out.append(f"{label}: expected {want!r}, CRM has {got!r}")

    cmp("contact_id", expect.get("contact_id"), c["id"], norm=lambda v: v)
    cmp("account", expect.get("account"), c.get("accountName"))
    cmp("account_id", expect.get("account_id"), c.get("accountId"), norm=lambda v: v)
    cmp("owner", expect.get("owner"), c.get("owner"))
    cmp("first_name", expect.get("first_name"), c.get("firstName"))
    cmp("last_name", expect.get("last_name"), c.get("lastName"))
    cmp("title", expect.get("title"), c.get("title"))
    for e in expect.get("emails") or []:
        if normalize_email(e) not in (c.get("emails") or []):
            out.append(f"email {normalize_email(e)!r} missing (CRM has {c.get('emails')})")
    for p in expect.get("phones") or []:
        if (normalize_phone(p) or p) not in (c.get("phones") or []):
            out.append(f"phone {p!r} missing (CRM has {c.get('phones')})")
    return out


@register("crm.contact_exists")
async def contact_exists(args: dict[str, Any], expect: dict[str, Any], ctx: CheckContext) -> CheckResult:
    crm = _reader(ctx)
    if crm is None:
        return _no_reader()
    email = normalize_email(args.get("email"))
    if not email:
        return CheckResult(False, "crm.contact_exists needs args.email")
    claim = _claim(ctx)
    try:
        hits = await crm.find_contacts_by_email(email)
    except CrmError as e:
        return CheckResult(False, f"CRM query failed: {e}", {"email": email})
    observed: dict[str, Any] = {"email": email, "count": len(hits)}
    if not hits:
        claimed = claim.get("contact_id") or args.get("contact_id")
        tail = f"; claim said contact {claimed}" if claimed else ""
        return CheckResult(False, f"no CRM contact has email {email}{tail}", observed)
    if len(hits) > 1:
        observed["contact_ids"] = [c["id"] for c in hits]
        return CheckResult(False, f"{len(hits)} CRM contacts share email {email} (duplicate)", observed)
    c = hits[0]
    observed.update(_contact_summary(c))
    problems = _mismatches(c, expect)
    for label, src in (("args", args), ("claim", claim)):  # any stated id must be the record REST found
        if src.get("contact_id") and src["contact_id"] != c["id"]:
            problems.append(f"{label} contact_id {src['contact_id']!r} but the contact with {email} is {c['id']!r}")
    if problems:
        return CheckResult(False, "contact mismatch: " + "; ".join(problems), observed)
    return CheckResult(True, f"contact {c['id']} has {email}" + "".join(
        f", {k}={observed[k]}" for k in ("account", "owner") if observed.get(k)), observed)


@register("crm.no_duplicate")
async def no_duplicate(args: dict[str, Any], expect: dict[str, Any], ctx: CheckContext) -> CheckResult:
    crm = _reader(ctx)
    if crm is None:
        return _no_reader()
    emails = [normalize_email(e) for e in (args.get("emails") or [args.get("email")]) if normalize_email(e)]
    if not emails:
        return CheckResult(False, "crm.no_duplicate needs args.email or args.emails")
    want = int(expect.get("count", 1))
    counts: dict[str, list[str]] = {}
    try:
        for e in emails:
            counts[e] = [c["id"] for c in await crm.find_contacts_by_email(e)]
    except CrmError as err:
        return CheckResult(False, f"CRM query failed: {err}", {"emails": emails})
    observed = {"contacts_by_email": counts}
    bad = {e: ids for e, ids in counts.items() if len(ids) != want}
    if bad:
        parts = [f"{e}: {len(ids)} contacts {ids}" for e, ids in bad.items()]
        return CheckResult(False, f"expected exactly {want} contact per email (primary+secondary); " + "; ".join(parts), observed)
    return CheckResult(True, f"exactly {want} contact for each of {len(emails)} email(s)", observed)


@register("crm.task_exists")
async def task_exists(args: dict[str, Any], expect: dict[str, Any], ctx: CheckContext) -> CheckResult:
    crm = _reader(ctx)
    if crm is None:
        return _no_reader()
    claim = _claim(ctx)
    contact_id = args.get("contact_id")
    try:
        if not contact_id and args.get("email"):
            hits = await crm.find_contacts_by_email(args["email"])
            if len(hits) != 1:
                return CheckResult(False, f"cannot resolve contact for {args['email']}: {len(hits)} contacts",
                                   {"email": args["email"]})
            contact_id = hits[0]["id"]
        contact_id = contact_id or claim.get("contact_id")
        if not contact_id:
            return CheckResult(False, "crm.task_exists needs args.contact_id or args.email")
        if await crm.get_contact(contact_id) is None:
            return CheckResult(False, f"contact {contact_id} does not exist", {"contact_id": contact_id})
        tasks = await crm.tasks_for_contact(contact_id)
    except CrmError as e:
        return CheckResult(False, f"CRM query failed: {e}", {"contact_id": contact_id})

    subject = expect.get("subject") or args.get("subject")
    matching = [t for t in tasks if not subject or (t.get("name") or "").strip().lower() == subject.strip().lower()]
    observed: dict[str, Any] = {
        "contact_id": contact_id,
        "tasks": [{"id": t["id"], "subject": t.get("name"), "due": _due(t), "owner": t.get("owner")} for t in tasks],
    }
    if not matching:
        what = f"subject {subject!r}" if subject else "any subject"
        tail = f"; claim said task {claim['task_id']}" if claim.get("task_id") else ""
        return CheckResult(False, f"no task with {what} linked to contact {contact_id}{tail}", observed)
    if claim.get("task_id") and claim["task_id"] not in {t["id"] for t in matching}:
        return CheckResult(False, f"claimed task {claim['task_id']} is not a matching task on contact {contact_id}",
                           observed)
    if len(matching) > 1 and not expect.get("allow_multiple"):
        return CheckResult(False, f"{len(matching)} tasks {subject!r} on contact {contact_id} (duplicate follow-up)",
                           observed)
    t = next((t for t in matching if t["id"] == claim.get("task_id")), matching[0])
    observed.update({"task_id": t["id"], "subject": t.get("name"), "due": _due(t), "owner": t.get("owner"),
                     "url": crm.task_url(t["id"])})
    problems = []
    if expect.get("due") and str(expect["due"])[:10] != observed["due"]:
        problems.append(f"due: expected {str(expect['due'])[:10]}, CRM has {observed['due']}")
    if expect.get("owner") and expect["owner"] != observed["owner"]:
        problems.append(f"owner: expected {expect['owner']!r}, CRM has {observed['owner']!r}")
    if problems:
        return CheckResult(False, "task mismatch: " + "; ".join(problems), observed)
    return CheckResult(True, f"task {t['id']} {t.get('name')!r} due {observed['due']} owner {observed['owner']}",
                       observed)


def _due(task: dict[str, Any]) -> str | None:
    return task.get("dateEndDate") or (task.get("dateEnd") or "")[:10] or None


def _ids(items: Any) -> list[str]:
    return [i["id"] if isinstance(i, dict) else str(i) for i in (items or [])]


async def _observe_routing(crm: CrmReader, lead: dict[str, Any], truth: dict[str, Any],
                           observed: dict[str, Any]) -> None:
    """Playbook routing as REST shows it, so the lane gets an owner whichever
    skill searched (browser search claims carry no routing). Best effort: a
    routing read failure never fails the lookup check itself."""
    from ..routing import owner_for

    try:
        existing = await crm.get_contact(truth["contact_id"]) if truth.get("contact_id") else None
        accounts = await crm.accounts_by_name(lead.get("company") or "")
        d = owner_for(lead, existing, accounts)
        observed["routing"] = {"owner": d.owner, "owner_reason": d.reason, "region": d.region,
                               "account_id": d.account_id, "account_name": d.account_name,
                               "account_candidates": d.candidates or []}
        if existing:
            observed["open_deal"] = bool(await crm.open_opportunities_for_contact(existing["id"]))
    except (CrmError, KeyError, TypeError) as e:
        observed["routing_error"] = str(e)


@register("crm.lookup_matches")
async def lookup_matches(args: dict[str, Any], expect: dict[str, Any], ctx: CheckContext) -> CheckResult:
    crm = _reader(ctx)
    if crm is None:
        return _no_reader()
    claim = {**_claim(ctx), **(expect.get("claim") or {})}
    claimed = claim.get("result")
    if claimed not in ("matched", "none", "ambiguous"):
        return CheckResult(False, f"claim has no valid result (matched|none|ambiguous): {claimed!r}")
    lead = args.get("lead") or args
    threshold = float(args.get("threshold", 0.85))
    try:
        truth = await crm.lookup(lead, threshold)
        accounts = await crm.accounts_by_name(lead.get("company") or "") if "accounts" in claim else None
    except CrmError as e:
        return CheckResult(False, f"CRM query failed: {e}")
    observed = {
        "result": truth["result"],
        "match_type": truth["match_type"],
        "contact_id": truth["contact_id"],
        "candidates": [{"id": c["id"], "name": c.get("name"), "score": c.get("score")} for c in truth["candidates"]],
    }
    if accounts is not None:
        observed["accounts"] = [{"id": a["id"], "name": a.get("name")} for a in accounts]
    await _observe_routing(crm, lead, truth, observed)

    if claimed != truth["result"]:
        return CheckResult(False, f"claimed {claimed!r} but REST lookup says {truth['result']!r}"
                           + (f" ({truth['contact_id'] or _ids(truth['candidates'])})" if truth["candidates"] else ""),
                           observed)
    if claimed == "matched" and claim.get("contact_id") != truth["contact_id"]:
        return CheckResult(False, f"claimed contact {claim.get('contact_id')} but email matches {truth['contact_id']}",
                           observed)
    if claimed == "ambiguous":
        got, want = set(_ids(claim.get("candidates"))), set(_ids(truth["candidates"]))
        if not got:
            return CheckResult(False, "claimed 'ambiguous' without candidates", observed)
        if not got <= want:
            return CheckResult(False, f"claimed candidates {sorted(got - want)} are not probable matches per REST",
                               observed)
        observed["missed_candidates"] = sorted(want - got)
    if accounts is not None:
        got, want = set(_ids(claim.get("accounts"))), {a["id"] for a in accounts}
        if got != want:
            return CheckResult(False, f"claimed accounts {sorted(got)} but REST matches {sorted(want)}", observed)
    return CheckResult(True, f"lookup agrees with REST: {truth['result']}"
                       + (f" {truth['contact_id']}" if truth["contact_id"] else ""), observed)
