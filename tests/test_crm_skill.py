"""api.espocrm skill handlers: check-then-act against the seeded CRM, verified by the checks."""

from __future__ import annotations

import pytest

from crm_helpers import *  # noqa: F403
from ledger_core.postconditions import CheckContext, load_all, run_check
from ledger_core.workers.crm_api_skill import SkillInputError, handle

pytestmark = pytest.mark.crm


def lead(uniq: str, **kw) -> dict:
    base = {"name": "Lena Fischer", "email": f"lena@{uniq}.test", "company": f"Kestrel {uniq}",
            "phone": "+49 30 901820", "title": "CTO", "country": "Germany"}
    return base | kw


async def contacts_with(reader, email):
    return await reader.find_contacts_by_email(email)


async def test_create_contact_twice_yields_one_contact(writer, reader, cleanup, uniq):
    ld = lead(uniq)
    cleanup.email(ld["email"])
    first = await handle("crm.create_contact", {"lead": ld, "idempotency_key": f"k-{uniq}"}, writer=writer,
                         check_then_act=True)
    assert first["acted"] is True and first["data"]["action"] == "created"
    assert first["data"]["owner"] == "r.silva" and first["data"]["owner_reason"] == "region"
    # simulated takeover: a second worker runs the same step
    second = await handle("crm.create_contact", {"lead": ld, "idempotency_key": f"k-{uniq}"}, writer=writer,
                          check_then_act=True)
    assert second["acted"] is False and second["data"]["action"] == "exists"
    assert second["data"]["contact_id"] == first["data"]["contact_id"]
    assert len(await contacts_with(reader, ld["email"])) == 1

    load_all()
    c = CheckContext(run_id="r", claim=first["data"], crm=reader)
    ok = await run_check("crm.contact_exists", {"email": ld["email"]}, {"owner": "r.silva", "first_name": "Lena"}, c)
    assert ok.ok, ok.reason
    assert (await run_check("crm.no_duplicate", {"email": ld["email"]}, {}, c)).ok


async def test_without_check_then_act_a_retry_duplicates_and_the_check_catches_it(writer, reader, cleanup, uniq):
    ld = lead(uniq, email=f"twice@{uniq}.test")
    cleanup.email(ld["email"])
    await handle("crm.create_contact", {"lead": ld}, writer=writer, check_then_act=False)
    await handle("crm.create_contact", {"lead": ld}, writer=writer, check_then_act=False)
    load_all()
    res = await run_check("crm.no_duplicate", {"email": ld["email"]}, {}, CheckContext(run_id="r", crm=reader))
    assert not res.ok and "2 contacts" in res.reason


async def test_create_contact_routes_by_account_owner(writer, reader, cleanup, uniq):
    ld = lead(uniq, email=f"q@{uniq}.test", company="Quarry Data", country="Germany")
    cleanup.email(ld["email"])
    out = await handle("crm.create_contact", {"lead": ld}, writer=writer, check_then_act=True)
    assert out["data"]["owner"] == "a.chen" and out["data"]["owner_reason"] == "account"
    c = await reader.get_contact(out["data"]["contact_id"])
    assert c["accountName"] == "Quarry Data"


async def test_create_contact_refuses_ambiguous_or_phone_only(writer, uniq):
    with pytest.raises(SkillInputError, match="ambiguous_account"):
        await handle("crm.create_contact", {"lead": lead(uniq, email=f"s@{uniq}.test", company="Lumen")},
                     writer=writer, check_then_act=True)
    with pytest.raises(SkillInputError, match="needs lead.email"):
        await handle("crm.create_contact", {"lead": lead(uniq, email="")}, writer=writer, check_then_act=True)


async def test_update_contact_adds_secondary_email_once(writer, reader, cleanup, uniq):
    ld = lead(uniq, email=f"primary@{uniq}.test")
    cleanup.email(ld["email"])
    created = await handle("crm.create_contact", {"lead": ld}, writer=writer, check_then_act=True)
    cid = created["data"]["contact_id"]
    upd = {"contact_id": cid, "lead": {"email": f"Personal@{uniq}.test", "phone": "(415) 555-0119"}}
    first = await handle("crm.update_contact", upd, writer=writer, check_then_act=True)
    assert first["acted"] and first["data"]["action"] == "updated"
    assert f"email:personal@{uniq}.test" in first["data"]["changed"]
    second = await handle("crm.update_contact", upd, writer=writer, check_then_act=True)
    assert second["acted"] is False and second["data"]["action"] == "unchanged"

    load_all()
    res = await run_check("crm.contact_exists", {"email": f"personal@{uniq}.test"},
                          {"contact_id": cid, "emails": [ld["email"], f"personal@{uniq}.test"]},
                          CheckContext(run_id="r", claim=first["data"], crm=reader))
    assert res.ok, res.reason
    c = await reader.get_contact(cid)
    assert c["emailAddress"] == ld["email"]  # primary untouched


async def test_create_task_check_then_act(writer, reader, cleanup, uniq):
    ld = lead(uniq, email=f"t@{uniq}.test", country="United States")
    cleanup.email(ld["email"])
    cid = (await handle("crm.create_contact", {"lead": ld}, writer=writer, check_then_act=True))["data"]["contact_id"]
    inputs = {"contact_id": cid, "event_name": "Signal Summit", "event_date": "2026-10-02"}  # Friday
    first = await handle("crm.create_task", inputs, writer=writer, check_then_act=True)
    assert first["acted"] and first["data"]["due"] == "2026-10-06" and first["data"]["owner"] == "a.chen"
    assert first["data"]["subject"] == "Follow up: Signal Summit"
    second = await handle("crm.create_task", inputs, writer=writer, check_then_act=True)
    assert second["acted"] is False and second["data"]["task_id"] == first["data"]["task_id"]
    assert len(await reader.tasks_for_contact(cid)) == 1

    load_all()
    res = await run_check("crm.task_exists", {"contact_id": cid},
                          {"subject": "Follow up: Signal Summit", "due": "2026-10-06", "owner": "a.chen"},
                          CheckContext(run_id="r", claim=first["data"], crm=reader))
    assert res.ok, res.reason


async def test_search_contact_outputs(writer, reader):
    load_all()
    marcus = await handle("crm.search_contact", {"lead": {"name": "marcus lee", "email": "MARCUS.LEE@ACME.COM",
                                                          "company": "Acme Corp", "country": "UK"}},
                          writer=writer, check_then_act=True)
    d = marcus["data"]
    assert d["result"] == "matched" and d["owner"] == "r.silva" and d["owner_reason"] == "existing_contact"
    assert d["open_deal"] is False and d["contact_url"].endswith(d["contact_id"])

    ben = await handle("crm.search_contact", {"lead": {"name": "Ben Ortiz", "email": "ben.ortiz@gmail.com",
                                                       "company": "Quarry Data", "phone": "(415) 555-0119",
                                                       "country": "US"}},
                       writer=writer, check_then_act=True)
    assert ben["data"]["result"] == "ambiguous" and ben["data"]["candidates"][0]["name"] == "Benjamin Ortiz"

    sam_lead = {"name": "Sam Ito", "email": "sam@lumen.io", "company": "Lumen", "country": ""}
    sam = await handle("crm.search_contact", {"lead": sam_lead}, writer=writer, check_then_act=True)
    assert sam["data"]["result"] == "none" and sam["data"]["owner"] is None
    assert sam["data"]["owner_reason"] == "ambiguous_account" and len(sam["data"]["accounts"]) == 2

    # the verifier agrees with each search claim through its own read-only key
    ben_lead = {"name": "Ben Ortiz", "email": "ben.ortiz@gmail.com", "company": "Quarry Data", "phone": "(415) 555-0119"}
    for lead_in, out in ((ben_lead, ben), (sam_lead, sam), ({"name": "marcus lee", "email": "MARCUS.LEE@ACME.COM", "company": "Acme Corp"}, marcus)):
        res = await run_check("crm.lookup_matches", {"lead": lead_in}, {},
                              CheckContext(run_id="r", claim=out["data"], crm=reader))
        assert res.ok, res.reason


async def test_unknown_kind_rejected(writer):
    with pytest.raises(SkillInputError):
        await handle("email.send", {}, writer=writer, check_then_act=True)
