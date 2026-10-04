"""EspoCRM skills (C2-C7): search, create, update, create task.

Every skill:
- runs inside `Operator.run_skill` (session checks, recovery, evidence);
- is check-then-act: it searches first and only writes when the work is not
  already done, so a retry or a takeover never creates a duplicate;
- returns a `SkillResult`: {acted, record_id, observations[], screenshots[], data}.

Navigation and form filling are coded; there are no LLM calls here.
Record ids come from what the UI itself shows or receives (row `data-id`,
the save response the UI gets back), never from a separate API channel.
"""

from __future__ import annotations

import re
from datetime import date
from urllib.parse import parse_qs, urlsplit
from typing import Any, Literal

from playwright.async_api import Error as PlaywrightError
from playwright.async_api import Locator

from . import selectors as sel
from .evidence import SkillResult
from .formats import candidate_score, company_score, format_date, name_score, norm_email, parse_iso_date, to_e164
from .operator import Operator, SessionLost, UIError
from .recovery import GiveUp

OnDuplicate = Literal["create", "abort"]

_LIST_ROWS_JS = """rows => rows.map(tr => {
  const cell = n => tr.querySelector(`td[data-name="${n}"]`);
  const txt = n => ((cell(n) && cell(n).innerText) || '').trim();
  const em = cell('emailAddress') && cell('emailAddress').querySelector('[data-email-address]');
  const ph = cell('phoneNumber') && cell('phoneNumber').querySelector('[data-phone-number]');
  return {id: tr.dataset.id, name: txt('name'), account: txt('account') || null,
          email: em ? em.dataset.emailAddress : null, phone: ph ? ph.dataset.phoneNumber : null};
})"""

_ROWS_MATCH_JS = """ids => {
  const got = [...document.querySelectorAll('.list-container tr.list-row[data-id]')].map(e => e.dataset.id);
  return got.length === ids.length && ids.every((x, i) => got[i] === x);
}"""


# =============================================================================
# building blocks
# =============================================================================


async def list_search(op: Operator, result: SkillResult, entity: str, query: str) -> list[dict[str, Any]]:
    """Type `query` into the list view's text filter and read the rows the UI renders.

    The rows are read only once the DOM shows exactly the records of the UI's
    own search request for this query (not the list's initial load).
    """
    page = op.page
    await op.goto(f"#{entity}", result)
    box = sel.list_text_filter(page)
    await op.wait_for(box)
    await op.wait_for(sel.list_loaded(page))  # initial load done, so it cannot race the search
    await box.fill(query)
    api = re.compile(rf"/api/v1/{entity}\?")

    def is_search(req: Any) -> bool:
        if req.method != "GET" or not api.search(req.url):
            return False
        values = [v for vs in parse_qs(urlsplit(req.url).query).values() for v in vs]
        return query in values

    ids: list[str] | None = None
    try:
        # A request sent after Enter: an in-flight initial load cannot be mistaken for it.
        async with page.expect_request(is_search, timeout=op.timeout_ms) as info:
            await box.press("Enter")
        resp = await (await info.value).response()
        if resp is None:
            raise UIError(f"{entity} search got no response")
        if resp.status == 401:
            raise SessionLost(f"{entity} search answered 401")
        if resp.ok:
            ids = [row["id"] for row in (await resp.json()).get("list", [])]
    except SessionLost:
        raise
    except PlaywrightError:
        ids = None  # no fetch observed; fall through and read what is rendered
    if ids is not None:
        await page.wait_for_function(_ROWS_MATCH_JS, arg=ids, timeout=op.timeout_ms)
    else:
        await page.wait_for_timeout(1000)
    rows: list[dict[str, Any]] = await sel.list_rows(page).evaluate_all(_LIST_ROWS_JS)
    result.observe(f"{entity.lower()}.list", f"{len(rows)} row(s) for {query!r}", query=query, rows=len(rows))
    return rows


async def open_contact(op: Operator, result: SkillResult, contact_id: str) -> None:
    page = op.page
    await op.goto(sel.route_contact_view(contact_id), result, wait_api=f"/api/v1/Contact/{contact_id}")
    await op.wait_for(sel.edit_button(page))
    if not page.url.endswith(sel.route_contact_view(contact_id)):
        raise UIError(f"expected contact {contact_id}, at {page.url}")
    await op.wait_for(sel.field_cell(page, "emailAddress"))


async def contact_details(op: Operator, result: SkillResult, contact_id: str) -> dict[str, Any]:
    """Emails and E.164 phones as the detail view shows them."""
    page = op.page
    await open_contact(op, result, contact_id)
    emails = [e for e in [await x.get_attribute("data-email-address") for x in await sel.detail_email_values(page).all()] if e]
    phones = [p for p in [await x.get_attribute("data-phone-number") for x in await sel.detail_phone_values(page).all()] if p]
    name = (await sel.field_cell(page, "name").locator(".field").first.inner_text()).strip()
    return {"id": contact_id, "name": name, "emails": emails, "phones": phones}


async def contacts_with_email(op: Operator, result: SkillResult, email: str) -> list[dict[str, Any]]:
    """Contacts whose primary OR secondary address equals `email` (case-insensitive)."""
    target = norm_email(email)
    rows = await list_search(op, result, "Contact", email.strip())
    hits = []
    for row in rows:
        if norm_email(row["email"]) == target:
            hits.append({**row, "matched_on": "email"})
    # The list shows only the primary address; a row matched on another address
    # is confirmed on its detail page (bounded: the text filter is already exact-ish).
    for row in [r for r in rows if norm_email(r["email"]) != target][:3]:
        details = await contact_details(op, result, row["id"])
        if target in {norm_email(e) for e in details["emails"]}:
            hits.append({**row, "matched_on": "secondary_email"})
    return hits


async def pick_suggestion(op: Operator, field: Locator, text: str, query: str | None = None) -> str:
    """Type into an autocomplete and click the suggestion equal to `text`.

    If nothing equals `text` but exactly one suggestion came back (e.g. a
    userName typed for a user whose display name differs), that one is taken.
    """
    page = op.page
    await field.fill(query or text)
    sugg = sel.autocomplete_suggestions(page)
    exact = sugg.filter(has_text=re.compile(rf"^\s*{re.escape(text)}\s*$", re.I))
    try:
        await exact.first.wait_for(state="visible", timeout=4000)
        choice = exact.first
    except PlaywrightError:
        texts = [t.strip() for t in await sugg.all_inner_texts()]
        if len(texts) != 1:
            await field.fill("")
            raise GiveUp(f"no unique match for {text!r} in suggestions {texts}")
        choice = sugg.first
    label = (await choice.inner_text()).strip()
    await choice.click()
    return label


async def save_record(
    op: Operator,
    result: SkillResult,
    entity: str,
    scope: Any,
    method: str = "POST",
    on_possible_duplicate: OnDuplicate = "create",
) -> dict[str, Any]:
    """Click Save and return the record the UI received back from its own save call."""
    page = op.page
    api = re.compile(rf"/api/v1/{entity}(/[A-Za-z0-9]+)?(\?|$)")

    def is_save(r: Any) -> bool:
        return r.request.method == method and bool(api.search(r.url))

    await op.checkpoint("before_save")
    async with page.expect_response(is_save) as info:
        await sel.save_button(scope).click()
    resp = await info.value
    if resp.status == 409:
        dialog = sel.duplicate_dialog(page)
        await dialog.wait_for(state="visible")
        rows = [t.strip() for t in await dialog.locator("tbody tr").all_inner_texts()]
        result.observe("duplicate_dialog", "CRM warns the record might already exist", rows=rows[:5])
        if on_possible_duplicate != "create":
            await sel.duplicate_dialog_cancel(page).click()
            raise GiveUp(f"possible duplicate {entity}: {rows[:3]}")
        async with page.expect_response(is_save) as info:
            await sel.duplicate_dialog_create(page).click()
        resp = await info.value
    if resp.status == 401:
        raise SessionLost(f"save {entity} answered 401")
    if resp.status in (400, 403, 404):
        raise GiveUp(f"save {entity} refused: HTTP {resp.status} {(await resp.text())[:200]}")
    if not resp.ok:
        raise UIError(f"save {entity} failed: HTTP {resp.status}")
    data = await resp.json()
    await op.checkpoint("after_save")
    return data


async def ensure_account(
    op: Operator, result: SkillResult, name: str, owner: str | None, create_missing: bool
) -> str | None:
    """Find the account by exact name (case-insensitive); create it if missing and allowed."""
    rows = await list_search(op, result, "Account", name.strip())
    for row in rows:
        if row["name"].strip().lower() == name.strip().lower():
            result.observe("account.list", f"account {row['name']!r} exists", account_id=row["id"])
            return row["id"]
    if not create_missing:
        result.observe("account.list", f"no account named {name!r}; contact will have no account")
        return None
    page = op.page
    await op.goto(sel.ROUTE_ACCOUNT_CREATE, result)
    await op.wait_for(sel.name_input(page))
    await sel.name_input(page).fill(name.strip())
    if owner:
        await pick_suggestion(op, sel.assigned_user_input(page), owner)
    data = await save_record(op, result, "Account", page)
    result.data["account_created"] = True
    result.observe("account.create", f"created account {name!r}", account_id=data["id"])
    return data["id"]


# =============================================================================
# skills
# =============================================================================


async def search_contact(op: Operator, email: str, *, label: str | None = None) -> SkillResult:
    """C3: find contacts by email. record_id is set when exactly one contact matches."""

    async def body(result: SkillResult) -> None:
        hits = await contacts_with_email(op, result, email)
        result.data.update(query={"email": email}, candidates=hits, count=len(hits))
        result.record_id = hits[0]["id"] if len(hits) == 1 else None
        seen = f"{len(hits)} contact(s) with email {email}"
        result.observe("contact.search", seen, count=len(hits), ids=[h["id"] for h in hits])

    return await op.run_skill("crm.search_contact", label, body)


async def search_by_name_company(
    op: Operator, name: str, company: str | None, *, label: str | None = None, limit: int = 10
) -> SkillResult:
    """C3: fuzzy candidates by person name (+ company). Scores only; the decision is upstream."""

    async def body(result: SkillResult) -> None:
        parts = name.split()
        queries = [name.strip()] + ([parts[-1], parts[0]] if len(parts) > 1 else [])
        found: dict[str, dict[str, Any]] = {}
        for q in dict.fromkeys(q for q in queries if len(q) >= 2):
            for row in await list_search(op, result, "Contact", q):
                found.setdefault(row["id"], row)
        cands = []
        for row in found.values():
            cands.append(
                {
                    **row,
                    "score": candidate_score(name, company, row["name"], row["account"]),
                    "name_score": name_score(name, row["name"]),
                    "company_score": company_score(company, row["account"]),
                }
            )
        cands.sort(key=lambda c: (-c["score"], c["name"]))
        cands = cands[:limit]
        result.data.update(query={"name": name, "company": company}, candidates=cands, count=len(cands))
        result.data["best"] = cands[0] if cands else None
        top = f"best {cands[0]['name']} / {cands[0]['account']} ({cands[0]['score']})" if cands else "none"
        result.observe("contact.search", f"{len(cands)} candidate(s) for {name!r} at {company!r}; {top}")

    return await op.run_skill("crm.search_contact", label, body)


async def create_contact(
    op: Operator,
    first: str,
    last: str,
    email: str | None,
    phone: str | None = None,
    title: str | None = None,
    account_name: str | None = None,
    owner_user_name: str | None = None,
    *,
    label: str | None = None,
    on_possible_duplicate: OnDuplicate = "create",
    create_missing_account: bool = True,
) -> SkillResult:
    """C4: create a contact unless one with this email (or, without email, name+account) exists."""
    full = f"{first} {last}".strip()

    async def body(result: SkillResult) -> None:
        page = op.page
        # ---- check
        if email:
            hits = await contacts_with_email(op, result, email)
        else:
            rows = await list_search(op, result, "Contact", full)
            hits = [
                r
                for r in rows
                if r["name"].lower() == full.lower()
                and (not account_name or (r["account"] or "").lower() == account_name.lower())
            ]
        if hits:
            result.acted, result.record_id = False, hits[0]["id"]
            result.data["existing"] = hits
            result.observe("contact.search", f"contact already exists ({hits[0]['id']}); not creating")
            await op.shot(result, "before")
            return
        # ---- act
        if account_name:
            await ensure_account(op, result, account_name, owner_user_name, create_missing_account)
        await op.goto(sel.ROUTE_CONTACT_CREATE, result)
        await op.wait_for(sel.first_name(page))
        await sel.first_name(page).fill(first)
        await sel.last_name(page).fill(last)
        if email:
            await sel.email_inputs(page).first.fill(email.strip())
        if phone:
            await sel.phone_inputs(page).first.fill(to_e164(phone) or phone)
        if account_name:
            try:
                picked = await pick_suggestion(op, sel.accounts_select(page), account_name.strip())
                result.data["account"] = picked
                if title:
                    await sel.account_title_inputs(page).first.fill(title)
            except GiveUp:
                if create_missing_account:
                    raise
                result.observe("contact.create", f"account {account_name!r} not linked (not found)")
        if title and not account_name:
            result.observe("contact.create", "title not set: EspoCRM stores it per linked account")
        if owner_user_name:
            result.data["owner"] = await pick_suggestion(op, sel.assigned_user_input(page), owner_user_name)
        await op.shot(result, "before")
        saved = await save_record(op, result, "Contact", page, on_possible_duplicate=on_possible_duplicate)
        result.acted, result.record_id = True, saved["id"]
        try:
            await page.wait_for_url(re.compile(rf"#Contact/view/{saved['id']}"), timeout=op.timeout_ms)
        except PlaywrightError:
            pass
        result.observe("contact.view", f"created contact {full}", contact_id=saved["id"])

    return await op.run_skill("crm.create_contact", label, body)


async def update_contact(
    op: Operator,
    contact_id: str,
    *,
    secondary_email: str | None = None,
    phone: str | None = None,
    title: str | None = None,
    label: str | None = None,
) -> SkillResult:
    """C5: enrich an existing contact without overwriting anything.

    - email: added as an extra address unless the contact already has it;
    - phone: added as an extra number unless already present (E.164 compare);
    - title: set only when the linked account's title is empty.
    acted=False when there was nothing to add.
    """

    async def body(result: SkillResult) -> None:
        page = op.page
        # ---- check
        current = await contact_details(op, result, contact_id)
        have_emails = {norm_email(e) for e in current["emails"]}
        have_phones = {to_e164(p) or p for p in current["phones"]}
        todo: dict[str, str] = {}
        skipped: dict[str, str] = {}
        if secondary_email:
            if norm_email(secondary_email) in have_emails:
                skipped["email"] = "already present"
            else:
                todo["email"] = secondary_email.strip()
        if phone:
            e164 = to_e164(phone) or phone
            if e164 in have_phones:
                skipped["phone"] = "already present"
            else:
                todo["phone"] = e164
        await op.goto(sel.route_contact_edit(contact_id), result, wait_api=f"/api/v1/Contact/{contact_id}")
        await op.wait_for(sel.first_name(page))
        if title:
            titles = sel.account_title_inputs(page)
            if not await titles.count():
                skipped["title"] = "no linked account to hold a title"
            elif (await titles.first.input_value()).strip():
                skipped["title"] = "already set"
            else:
                todo["title"] = title
        result.data.update(current=current, planned=todo, skipped=skipped)
        if not todo:
            result.acted, result.record_id = False, contact_id
            result.observe("contact.edit", "nothing to add; leaving the contact unchanged", skipped=skipped)
            await op.shot(result, "before")
            await page.goto(op.url(sel.route_contact_view(contact_id)))  # leave edit mode untouched
            return
        # ---- act
        if "email" in todo:
            await sel.email_add_button(page).click()
            await sel.email_inputs(page).last.fill(todo["email"])
        if "phone" in todo:
            phones = sel.phone_inputs(page)
            if current["phones"]:
                await sel.phone_add_button(page).click()
            await phones.last.fill(todo["phone"])
        if "title" in todo:
            await sel.account_title_inputs(page).first.fill(todo["title"])
        await op.shot(result, "before")
        await save_record(op, result, "Contact", page, method="PUT")
        result.acted, result.record_id = True, contact_id
        try:
            await page.wait_for_url(re.compile(rf"#Contact/view/{contact_id}"), timeout=op.timeout_ms)
        except PlaywrightError:
            pass
        result.observe("contact.view", f"updated contact {current['name']}", added=sorted(todo))

    return await op.run_skill("crm.update_contact", label, body)


async def create_task(
    op: Operator,
    contact_id: str,
    subject: str,
    due_date: str | date,
    owner_user_name: str | None = None,
    *,
    label: str | None = None,
) -> SkillResult:
    """C6: follow-up task linked to the contact (parent = Contact), due date and owner set.

    Check: the contact's Tasks panel already lists a task with this subject.
    """
    due = parse_iso_date(due_date)

    async def body(result: SkillResult) -> None:
        page = op.page
        # ---- check
        await open_contact(op, result, contact_id)
        await op.wait_for(sel.tasks_panel_loaded(page))
        existing = []
        for link in await sel.tasks_panel_links(page).all():
            text = (await link.inner_text()).strip()
            href = await link.get_attribute("href") or ""
            existing.append({"id": href.rsplit("/", 1)[-1], "name": text})
        same = [t for t in existing if t["name"].strip().lower() == subject.strip().lower()]
        result.data["existing_tasks"] = existing
        if same:
            result.acted, result.record_id = False, same[0]["id"]
            result.observe("contact.tasks", f"task {subject!r} already linked; not creating", task_id=same[0]["id"])
            await op.shot(result, "before")
            return
        result.observe("contact.tasks", f"{len(existing)} task(s) linked; none named {subject!r}")
        # ---- act
        await sel.tasks_panel_create(page).click()
        modal = sel.open_modal(page)
        await op.wait_for(sel.name_input(modal))
        parent = await sel.parent_id_input(modal).input_value()
        if parent != contact_id:
            raise UIError(f"task form parent is {parent!r}, expected {contact_id}")
        await sel.name_input(modal).fill(subject)
        due_text = format_date(due, op.date_format)
        due_input = sel.date_end_input(modal)
        # Real keystrokes: the datepicker keeps its own state from key events and
        # would blank a value set with fill() when it closes.
        await due_input.click()
        await due_input.press_sequentially(due_text, delay=15)
        await due_input.press("Tab")
        if (await due_input.input_value()).strip() != due_text:
            raise UIError(f"due date field shows {await due_input.input_value()!r}, expected {due_text!r}")
        if owner_user_name:
            result.data["owner"] = await pick_suggestion(op, sel.assigned_user_input(modal), owner_user_name)
        result.data["due_text"] = due_text
        await op.shot(result, "before")
        saved = await save_record(op, result, "Task", modal)
        result.acted, result.record_id = True, saved["id"]
        try:
            await modal.wait_for(state="hidden", timeout=op.timeout_ms)
        except PlaywrightError:
            pass
        result.observe(
            "contact.tasks",
            f"created task {subject!r} due {due.isoformat()}",
            task_id=saved["id"],
            due=saved.get("dateEndDate") or saved.get("dateEnd"),
            owner=saved.get("assignedUserName"),
        )

    return await op.run_skill("crm.create_task", label, body)
