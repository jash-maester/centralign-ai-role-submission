"""Track C: Playwright skills against the seeded EspoCRM (make test-browser).

The operator acts through the browser; every assertion reads the CRM through
REST with the admin user (a different channel), never through the operator.
"""

from __future__ import annotations

import os
from datetime import date, timedelta
from pathlib import Path

import httpx
import pytest

pytest.importorskip("playwright")
operator_mod = pytest.importorskip("browser_worker.operator")

from ledger_core.settings import get_settings  # noqa: E402

Operator = operator_mod.Operator
pytestmark = pytest.mark.browser


# ---------------------------------------------------------------- fixtures
@pytest.fixture
async def admin():
    s = get_settings()
    async with httpx.AsyncClient(
        base_url=f"{s.crm_internal_url}/api/v1", auth=(s.espo_admin_user, s.espo_admin_password), timeout=30
    ) as client:
        yield client


@pytest.fixture
async def created(admin):
    """Records to delete after the test: list of (entity, id)."""
    records: list[tuple[str, str]] = []
    yield records
    for entity, rid in reversed(records):
        await admin.delete(f"/{entity}/{rid}")


@pytest.fixture
async def op(tmp_path, uniq):
    o = Operator(f"test-{uniq}", state_dir=str(tmp_path))
    await o.start()
    yield o
    await o.close()


async def contacts_by_email(admin: httpx.AsyncClient, email: str) -> list[dict]:
    params = {"where[0][type]": "equals", "where[0][attribute]": "emailAddress", "where[0][value]": email}
    resp = await admin.get("/Contact", params=params)
    resp.raise_for_status()
    return resp.json()["list"]


async def contact(admin: httpx.AsyncClient, cid: str) -> dict:
    resp = await admin.get(f"/Contact/{cid}")
    resp.raise_for_status()
    return resp.json()


async def tasks_of(admin: httpx.AsyncClient, cid: str) -> list[dict]:
    params = {
        "where[0][type]": "equals",
        "where[0][attribute]": "parentId",
        "where[0][value]": cid,
        "select": "id,name,parentType,parentId,dateEnd,dateEndDate,assignedUserName,assignedUserId",
    }
    resp = await admin.get("/Task", params=params)
    resp.raise_for_status()
    return resp.json()["list"]


async def make_contact(admin, created, uniq: str, **extra) -> dict:
    data = {"firstName": "Seed", "lastName": f"Probe{uniq}", "emailAddress": f"seed.probe@{uniq}.test", **extra}
    resp = await admin.post("/Contact", json=data, headers={"X-Skip-Duplicate-Check": "true"})
    resp.raise_for_status()
    row = resp.json()
    created.append(("Contact", row["id"]))
    return row


def assert_evidence(result) -> None:
    root = Path(get_settings().evidence_dir)
    names = result.screenshots
    assert any(n.endswith("_before.png") for n in names), names
    assert any(n.endswith("_after.png") for n in names), names
    for n in names:
        assert (root / n).stat().st_size > 1000, n


# ---------------------------------------------------------------- tests
async def test_login_and_session_reuse(tmp_path, uniq):
    agent = f"test-login-{uniq}"
    async with Operator(agent, state_dir=str(tmp_path)) as first:
        await first.ensure_logged_in()
        assert first.login_count == 1
        assert first.user_id, "the UI's App/user payload was observed"
    assert (tmp_path / f"{agent}.storage.json").exists()
    async with Operator(agent, state_dir=str(tmp_path)) as second:
        await second.ensure_logged_in()
        assert second.login_count == 0, "stored session should be reused without a new login"


async def test_create_contact_then_search_finds_it(op, admin, created, uniq):
    email = f"priya.{uniq}@northwind-{uniq}.test"
    res = await op.create_contact(
        "Priya",
        f"Raman{uniq}",
        email,
        phone="+1 (415) 555-0142",
        title="VP Data",
        account_name="Acme Corp",
        owner_user_name="a.chen",
        label=f"test-{uniq}-create",
    )
    assert res.ok, res.as_dict()
    assert res.acted and res.record_id
    assert res.data["recoveries"] == 0, res.as_dict()
    created.append(("Contact", res.record_id))

    rows = await contacts_by_email(admin, email)
    assert [r["id"] for r in rows] == [res.record_id]
    c = await contact(admin, res.record_id)
    assert c["firstName"] == "Priya" and c["lastName"] == f"Raman{uniq}"
    assert c["accountName"] == "Acme Corp"
    assert c["assignedUserName"] == "Alex Chen"
    assert c["phoneNumber"] == "+14155550142"
    assert c["title"] == "VP Data"
    assert_evidence(res)
    assert all({"page", "seen", "ts"} <= set(o) for o in res.observations)

    found = await op.search_contact(email.upper(), label=f"test-{uniq}-search")
    assert found.ok and found.acted is False
    assert found.record_id == res.record_id
    assert found.data["count"] == 1


async def test_check_then_act_create_twice_makes_one_contact(op, admin, created, uniq):
    email = f"lena.{uniq}@kestrel-{uniq}.test"
    company = f"Kestrel Labs {uniq}"
    kw = dict(account_name=company, owner_user_name="r.silva")
    one = await op.create_contact("Lena", f"Fischer{uniq}", email, label=f"test-{uniq}-twice-1", **kw)
    assert one.ok and one.acted, one.as_dict()
    assert one.data["recoveries"] == 0, one.as_dict()
    created.append(("Contact", one.record_id))
    accounts = (await admin.get("/Account", params={"where[0][type]": "equals", "where[0][attribute]": "name", "where[0][value]": company})).json()["list"]
    assert len(accounts) == 1, "missing account was created once"
    created.insert(0, ("Account", accounts[0]["id"]))

    two = await op.create_contact("Lena", f"Fischer{uniq}", email.upper(), label=f"test-{uniq}-twice-2", **kw)
    assert two.ok, two.as_dict()
    assert two.acted is False
    assert two.record_id == one.record_id
    assert len(await contacts_by_email(admin, email)) == 1
    accounts = (await admin.get("/Account", params={"where[0][type]": "equals", "where[0][attribute]": "name", "where[0][value]": company})).json()["list"]
    assert len(accounts) == 1


async def test_update_adds_secondary_email_without_overwriting(op, admin, created, uniq):
    seeded = await make_contact(admin, created, uniq, phoneNumber="+12125550100")
    extra = f"personal.{uniq}@gmail.test"
    res = await op.update_contact(
        seeded["id"], secondary_email=extra, phone="+1 415 555 0177", label=f"test-{uniq}-update"
    )
    assert res.ok and res.acted, res.as_dict()
    c = await contact(admin, seeded["id"])
    emails = {e["emailAddress"].lower(): e["primary"] for e in c["emailAddressData"]}
    assert emails == {f"seed.probe@{uniq}.test": True, extra: False}
    phones = {p["phoneNumber"] for p in c["phoneNumberData"]}
    assert phones == {"+12125550100", "+14155550177"}
    assert c["phoneNumber"] == "+12125550100", "primary phone kept"
    assert_evidence(res)

    again = await op.update_contact(seeded["id"], secondary_email=extra.upper(), label=f"test-{uniq}-update-2")
    assert again.ok and again.acted is False, again.as_dict()
    assert len((await contact(admin, seeded["id"]))["emailAddressData"]) == 2


async def test_create_task_linked_with_due_date_and_owner(op, admin, created, uniq):
    seeded = await make_contact(admin, created, uniq)
    due = date.today() + timedelta(days=3)
    subject = f"Follow up: Signal Summit {uniq}"
    res = await op.create_task(seeded["id"], subject, due.isoformat(), "r.silva", label=f"test-{uniq}-task")
    assert res.ok and res.acted, res.as_dict()
    assert res.data["recoveries"] == 0, res.as_dict()
    tasks = await tasks_of(admin, seeded["id"])
    created.extend(("Task", t["id"]) for t in tasks)
    assert len(tasks) == 1
    t = tasks[0]
    assert t["id"] == res.record_id
    assert t["name"] == subject
    assert t["parentType"] == "Contact" and t["parentId"] == seeded["id"]
    assert (t.get("dateEndDate") or t["dateEnd"][:10]) == due.isoformat()
    assert t["assignedUserName"] == "Rita Silva"
    assert_evidence(res)

    again = await op.create_task(seeded["id"], subject, due, "r.silva", label=f"test-{uniq}-task-2")
    assert again.ok and again.acted is False and again.record_id == res.record_id, again.as_dict()
    assert len(await tasks_of(admin, seeded["id"])) == 1


async def test_expired_session_between_skills_relogs_and_continues(op, admin, created, uniq):
    seeded = await make_contact(admin, created, uniq)
    await op.ensure_logged_in()
    assert op.login_count == 1
    await op.expire_session()
    res = await op.create_task(seeded["id"], f"After expiry {uniq}", date.today(), label=f"test-{uniq}-expired")
    assert res.ok and res.acted, res.as_dict()
    assert op.login_count == 2
    assert any(o["page"] == "login" and "logged in" in o["seen"] for o in res.observations)
    tasks = await tasks_of(admin, seeded["id"])
    created.extend(("Task", t["id"]) for t in tasks)
    assert [t["id"] for t in tasks] == [res.record_id]


async def test_expired_session_mid_skill_recovers_without_duplicate(op, admin, created, uniq):
    """F3: cookies vanish after the form is filled; save fails, operator re-logs in and redoes it."""
    shots = {"n": 0}

    async def expire_once(o, _name):
        if shots["n"] == 0:
            shots["n"] += 1
            await o.expire_session()

    op.on_checkpoint("before_save", expire_once)
    email = f"dana.{uniq}@helixbio-{uniq}.test"
    res = await op.create_contact("Dana", f"Okafor{uniq}", email, label=f"test-{uniq}-midexpiry")
    assert res.ok and res.acted, res.as_dict()
    created.append(("Contact", res.record_id))
    assert shots["n"] == 1
    assert res.data["recoveries"] >= 1
    assert any(o["page"] == "recovery" for o in res.observations)
    assert op.login_count >= 2
    assert [r["id"] for r in await contacts_by_email(admin, email)] == [res.record_id]
    assert any("recovery-1" in s for s in res.screenshots)


async def test_search_by_name_company_finds_fuzzy_candidate(op):
    res = await op.search_by_name_company("Ben Ortiz", "Quarry Data")
    assert res.ok, res.as_dict()
    best = res.data["best"]
    assert best["name"] == "Benjamin Ortiz" and best["account"] == "Quarry Data"
    assert best["score"] >= 0.85
    assert res.record_id is None, "fuzzy matches are decided upstream, not by the operator"


async def test_unknown_owner_gives_up_with_reason(op, admin, uniq):
    email = f"nobody.{uniq}@example-{uniq}.test"
    res = await op.create_contact("No", f"Owner{uniq}", email, owner_user_name=f"ghost.{uniq}")
    assert res.ok is False and res.acted is False
    assert "ghost" in res.reason
    assert await contacts_by_email(admin, email) == []


def test_evidence_dir_is_the_volume():
    assert os.path.isdir(get_settings().evidence_dir)
