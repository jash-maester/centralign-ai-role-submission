"""CRM postconditions: pass on real records, reject fabricated claims with a clear reason."""

from __future__ import annotations

import pytest

from crm_helpers import *  # noqa: F403
from ledger_core.postconditions import REGISTRY, CheckContext, load_all, run_check

pytestmark = pytest.mark.crm


@pytest.fixture(autouse=True)
def _load_checks():
    load_all()


def ctx(reader, claim=None) -> CheckContext:
    return CheckContext(run_id="run_test", step_id="stp_test", claim=claim, crm=reader)


def test_crm_checks_registered():
    load_all()
    assert {"crm.contact_exists", "crm.no_duplicate", "crm.task_exists", "crm.lookup_matches"} <= set(REGISTRY)


async def test_checks_fail_without_reader():
    res = await run_check("crm.contact_exists", {"email": "a@b.test"}, {}, CheckContext(run_id="r"))
    assert not res.ok and "no CRM reader" in res.reason


# ---- crm.contact_exists ----------------------------------------------------


async def test_contact_exists_on_seeded_contact(reader):
    res = await run_check("crm.contact_exists", {"email": "MARCUS.LEE@ACME.COM"},
                          {"account": "Acme Corp", "owner": "r.silva"}, ctx(reader))
    assert res.ok, res.reason
    assert res.observed["owner"] == "r.silva" and res.observed["account"] == "Acme Corp"
    assert res.observed["url"].endswith(res.observed["contact_id"])


async def test_contact_exists_rejects_fabricated_claim(reader, uniq):
    claim = {"contact_id": "6ac0000000fake001", "action": "created"}
    res = await run_check("crm.contact_exists", {"email": f"dana@{uniq}.test"}, {"owner": "a.chen"}, ctx(reader, claim))
    assert not res.ok
    assert f"no CRM contact has email dana@{uniq}.test" in res.reason and "6ac0000000fake001" in res.reason
    assert res.observed["count"] == 0


async def test_contact_exists_lists_mismatches(reader):
    res = await run_check("crm.contact_exists", {"email": "hannah@fieldstone.dev"},
                          {"owner": "r.silva", "account": "Acme Corp", "emails": ["hannah.second@x.test"]}, ctx(reader))
    assert not res.ok
    assert "owner: expected 'r.silva', CRM has 'a.chen'" in res.reason
    assert "account: expected 'Acme Corp', CRM has 'Fieldstone'" in res.reason
    assert "hannah.second@x.test" in res.reason


async def test_contact_exists_rejects_wrong_claimed_id(reader):
    res = await run_check("crm.contact_exists", {"email": "hannah@fieldstone.dev"}, {},
                          ctx(reader, {"contact_id": "notTheRealId"}))
    assert not res.ok and "claim contact_id 'notTheRealId'" in res.reason


# ---- crm.no_duplicate ------------------------------------------------------


async def test_no_duplicate_passes_for_seed_and_catches_duplicate(reader, writer, cleanup, uniq):
    ok = await run_check("crm.no_duplicate", {"emails": ["marcus.lee@acme.com", "Hannah@Fieldstone.dev"]}, {}, ctx(reader))
    assert ok.ok, ok.reason

    email = cleanup.email(f"dup@{uniq}.test")
    a = await writer.create_contact(first_name="Dup", last_name="One", email=email, owner="a.chen")
    single = await run_check("crm.no_duplicate", {"email": email}, {}, ctx(reader))
    assert single.ok, single.reason
    # a second contact carrying the same address as a *secondary* email
    b = await writer.create_contact(first_name="Dup", last_name="Two", email=f"other@{uniq}.test", owner="a.chen")
    cleanup.email(f"other@{uniq}.test")
    await writer.update_contact(b["id"], add_emails=[email.upper()])
    res = await run_check("crm.no_duplicate", {"email": email}, {}, ctx(reader))
    assert not res.ok
    assert f"{email}: 2 contacts" in res.reason
    assert set(res.observed["contacts_by_email"][email]) == {a["id"], b["id"]}


async def test_no_duplicate_fails_when_missing(reader, uniq):
    res = await run_check("crm.no_duplicate", {"email": f"ghost@{uniq}.test"}, {}, ctx(reader))
    assert not res.ok and "0 contacts" in res.reason


# ---- crm.task_exists -------------------------------------------------------


async def test_task_exists_real_and_mismatch(reader, writer, cleanup, uniq):
    email = cleanup.email(f"task@{uniq}.test")
    c = await writer.create_contact(first_name="Task", last_name=uniq, email=email, owner="r.silva")
    t = await writer.create_task(contact_id=c["id"], subject="Follow up: Signal Summit", due="2026-10-06", owner="r.silva")
    expect = {"subject": "Follow up: Signal Summit", "due": "2026-10-06", "owner": "r.silva"}

    res = await run_check("crm.task_exists", {"email": email}, expect, ctx(reader, {"task_id": t["id"]}))
    assert res.ok, res.reason
    assert res.observed["task_id"] == t["id"] and res.observed["due"] == "2026-10-06"

    wrong = await run_check("crm.task_exists", {"contact_id": c["id"]}, {**expect, "due": "2026-10-07", "owner": "a.chen"},
                            ctx(reader))
    assert not wrong.ok
    assert "due: expected 2026-10-07, CRM has 2026-10-06" in wrong.reason and "owner: expected 'a.chen'" in wrong.reason

    await writer.create_task(contact_id=c["id"], subject="Follow up: Signal Summit", due="2026-10-06", owner="r.silva")
    dup = await run_check("crm.task_exists", {"contact_id": c["id"]}, expect, ctx(reader))
    assert not dup.ok and "duplicate follow-up" in dup.reason


async def test_task_exists_rejects_fabricated_claim(reader, writer, cleanup, uniq):
    email = cleanup.email(f"notask@{uniq}.test")
    c = await writer.create_contact(first_name="No", last_name="Task", email=email, owner="a.chen")
    claim = {"task_id": "6acfabricated0001", "contact_id": c["id"], "action": "created"}
    res = await run_check("crm.task_exists", {"contact_id": c["id"]}, {"subject": "Follow up: Signal Summit"},
                          ctx(reader, claim))
    assert not res.ok
    assert f"no task with subject 'Follow up: Signal Summit' linked to contact {c['id']}" in res.reason
    assert "6acfabricated0001" in res.reason


async def test_task_exists_unknown_contact(reader):
    res = await run_check("crm.task_exists", {"contact_id": "doesnotexist0001"}, {}, ctx(reader))
    assert not res.ok and "does not exist" in res.reason


# ---- crm.lookup_matches ----------------------------------------------------

BEN = {"name": "Ben Ortiz", "email": "ben.ortiz@gmail.com", "company": "Quarry Data", "phone": "(415) 555-0119"}


async def test_lookup_matches_email_match(reader):
    marcus = (await reader.find_contacts_by_email("marcus.lee@acme.com"))[0]
    lead = {"name": "marcus lee", "email": "MARCUS.LEE@ACME.COM", "company": "Acme Corp"}
    res = await run_check("crm.lookup_matches", {"lead": lead}, {},
                          ctx(reader, {"result": "matched", "contact_id": marcus["id"]}))
    assert res.ok, res.reason
    bad = await run_check("crm.lookup_matches", {"lead": lead}, {}, ctx(reader, {"result": "none"}))
    assert not bad.ok and "claimed 'none' but REST lookup says 'matched'" in bad.reason
    wrong = await run_check("crm.lookup_matches", {"lead": lead}, {},
                            ctx(reader, {"result": "matched", "contact_id": "someoneElse"}))
    assert not wrong.ok and "claimed contact someoneElse" in wrong.reason


async def test_lookup_matches_fuzzy_candidates(reader):
    ben = (await reader.find_contacts_by_email("benjamin@quarrydata.com"))[0]
    res = await run_check("crm.lookup_matches", {"lead": BEN}, {},
                          ctx(reader, {"result": "ambiguous", "candidates": [{"id": ben["id"]}]}))
    assert res.ok, res.reason
    fake = await run_check("crm.lookup_matches", {"lead": BEN}, {},
                           ctx(reader, {"result": "ambiguous", "candidates": ["fabricated123"]}))
    assert not fake.ok and "fabricated123" in fake.reason
    lie = await run_check("crm.lookup_matches", {"lead": BEN}, {}, ctx(reader, {"result": "matched", "contact_id": ben["id"]}))
    assert not lie.ok and "REST lookup says 'ambiguous'" in lie.reason


async def test_lookup_matches_none_and_accounts(reader, uniq):
    lead = {"name": "Sam Ito", "email": "sam@lumen.io", "company": "Lumen"}
    lumen = sorted(a["id"] for a in await reader.accounts_by_name("Lumen"))
    res = await run_check("crm.lookup_matches", {"lead": lead}, {}, ctx(reader, {"result": "none", "accounts": lumen}))
    assert res.ok, res.reason
    one = await run_check("crm.lookup_matches", {"lead": lead}, {}, ctx(reader, {"result": "none", "accounts": lumen[:1]}))
    assert not one.ok and "claimed accounts" in one.reason
    new = {"name": "Nobody New", "email": f"nobody@{uniq}.test", "company": f"Co {uniq}"}
    assert (await run_check("crm.lookup_matches", {"lead": new}, {}, ctx(reader, {"result": "none"}))).ok
    no_result = await run_check("crm.lookup_matches", {"lead": new}, {}, ctx(reader, {}))
    assert not no_result.ok and "no valid result" in no_result.reason
