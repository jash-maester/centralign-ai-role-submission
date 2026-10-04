"""crm_api: scoped REST clients against the seeded EspoCRM."""

from __future__ import annotations

import pytest

from ledger_core.crm_api import CrmError, CrmReader, name_score, normalize_phone, split_name

from crm_helpers import *  # noqa: F403

pytestmark = pytest.mark.crm


async def test_find_contacts_by_email_is_case_insensitive(reader):
    hits = await reader.find_contacts_by_email("  MARCUS.LEE@ACME.COM ")
    assert len(hits) == 1
    c = hits[0]
    assert c["name"] == "Marcus Lee" and c["owner"] == "r.silva" and c["accountName"] == "Acme Corp"
    assert c["emails"] == ["marcus.lee@acme.com"]
    assert c["url"].endswith(f"/#Contact/view/{c['id']}")


async def test_contact_url_uses_public_url(reader):
    from ledger_core.settings import get_settings

    assert reader.contact_url("abc") == f"{get_settings().crm_public_url}/#Contact/view/abc"


async def test_search_contacts_fuzzy_finds_benjamin(reader):
    cands = await reader.search_contacts(name="Ben Ortiz", company="Quarry Data", phone="(415) 555-0119")
    top = cands[0]
    assert top["name"] == "Benjamin Ortiz" and top["owner"] == "a.chen"
    assert top["phone_match"] and top["company_score"] == 1.0 and top["name_score"] >= 0.85


async def test_lookup_results(reader):
    assert (await reader.lookup({"email": "Hannah@Fieldstone.dev"}))["result"] == "matched"
    ben = await reader.lookup({"name": "Ben Ortiz", "email": "ben.ortiz@gmail.com", "company": "Quarry Data",
                               "phone": "(415) 555-0119"})
    assert ben["result"] == "ambiguous" and [c["name"] for c in ben["candidates"]] == ["Benjamin Ortiz"]
    sam = await reader.lookup({"name": "Sam Ito", "email": "sam@lumen.io", "company": "Lumen"})
    assert sam["result"] == "none"
    priya = await reader.lookup({"name": "Priya Raman", "email": "priya@northwind.com", "company": "Northwind"})
    assert priya["result"] == "none" and priya["candidates"] == []


async def test_accounts_by_name(reader):
    assert sorted(a["name"] for a in await reader.accounts_by_name("Lumen")) == ["Lumen Health", "Lumen Inc"]
    acme = await reader.accounts_by_name("Acme Corp")
    assert [a["name"] for a in acme] == ["Acme Corp"] and acme[0]["owner"] == "r.silva"
    assert await reader.accounts_by_name("Northwind") == []


async def test_open_opportunities_for_contact(reader):
    ben = (await reader.find_contacts_by_email("benjamin@quarrydata.com"))[0]
    deals = await reader.open_opportunities_for_contact(ben["id"])
    assert [d["name"] for d in deals] == ["Quarry Data - Platform expansion"]
    marcus = (await reader.find_contacts_by_email("marcus.lee@acme.com"))[0]
    assert await reader.open_opportunities_for_contact(marcus["id"]) == []


async def test_users_map(reader):
    users = await reader.users()
    assert {"a.chen", "r.silva"} <= set(users)
    assert users["a.chen"]["id"] in await reader.users_by_id()


async def test_reader_key_cannot_write(reader, uniq):
    assert not hasattr(reader, "create_contact")
    with pytest.raises(CrmError) as e:
        await reader._request("POST", "/Contact", json={"lastName": f"ro-{uniq}", "emailAddress": f"ro@{uniq}.test"})
    assert e.value.status == 403
    marcus = (await reader.find_contacts_by_email("marcus.lee@acme.com"))[0]
    with pytest.raises(CrmError) as e:
        await reader._request("PUT", f"/Contact/{marcus['id']}", json={"title": "hacked"})
    assert e.value.status == 403


async def test_writer_creates_and_enriches_without_overwriting(writer, reader, cleanup, uniq):
    email = cleanup.email(f"probe@{uniq}.test")
    c = await writer.create_contact(first_name="Probe", last_name=uniq, email=email.upper(), phone="(415) 555-0100",
                                    owner="r.silva", title=None)
    assert c["owner"] == "r.silva" and c["emails"] == [email]
    res = await writer.update_contact(c["id"], add_emails=[f"Second@{uniq}.test", email], add_phones=["+1 415 555 0100",
                                      "+44 20 7946 0000"], fill={"description": "Met at Signal Summit", "title": "CTO"})
    # title lives on the account link in EspoCRM: not fillable on an account-less contact
    assert sorted(res["changed"]) == sorted([f"email:second@{uniq}.test", "phone:+442079460000", "description"])
    after = await reader.get_contact(c["id"])
    assert after["emailAddress"] == email  # primary kept
    assert after["emails"] == [email, f"second@{uniq}.test"]
    assert after["phones"] == ["+14155550100", "+442079460000"] and after["description"] == "Met at Signal Summit"
    again = await writer.update_contact(c["id"], add_emails=[f"second@{uniq}.test"], fill={"description": "other"})
    assert again["changed"] == [] and again["contact"]["description"] == "Met at Signal Summit"  # never overwrites


async def test_writer_fills_title_on_contact_with_account(writer, reader, cleanup, uniq):
    email = cleanup.email(f"titled@{uniq}.test")
    acme = (await reader.accounts_by_name("Acme Corp"))[0]
    c = await writer.create_contact(first_name="Titled", last_name=uniq, email=email, owner="r.silva", account_id=acme["id"])
    res = await writer.update_contact(c["id"], fill={"title": "CTO"})
    assert res["changed"] == ["title"] and (await reader.get_contact(c["id"]))["title"] == "CTO"


async def test_writer_task_has_due_date_owner_and_link(writer, reader, cleanup, uniq):
    email = cleanup.email(f"task@{uniq}.test")
    c = await writer.create_contact(first_name="Task", last_name=uniq, email=email, owner="a.chen")
    t = await writer.create_task(contact_id=c["id"], subject="Follow up: Signal Summit", due="2026-10-06", owner="r.silva")
    tasks = await reader.tasks_for_contact(c["id"])
    assert [(x["id"], x["dateEndDate"], x["owner"], x["contactId"]) for x in tasks] == [
        (t["id"], "2026-10-06", "r.silva", c["id"])]


def test_normalisation_helpers():
    assert normalize_phone("(415) 555-0119") == "+14155550119"
    assert normalize_phone("+44 20 7946 0958") == "+442079460958"
    assert normalize_phone("nope") is None
    assert split_name({"name": "  marcus lee"}) == ("Marcus", "Lee")
    assert split_name({"first_name": "Ana", "last_name": "de la Cruz"}) == ("Ana", "de la Cruz")
    assert name_score("Ben", "Ortiz", "Benjamin", "Ortiz") >= 0.9
    assert name_score("Sam", "Ito", "Katrin", "Vogel") < 0.5


def test_reader_class_has_no_write_methods():
    for name in ("create_contact", "update_contact", "create_task"):
        assert not hasattr(CrmReader, name)
