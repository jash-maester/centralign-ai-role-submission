"""Owner routing (playbook "Owner routing") and follow-up due dates.

The demo owner column in plans/02-features.md "Demo data" is the oracle: the
routing must reproduce it for every row that gets routed.
"""

from __future__ import annotations

import csv
import json
import re
from datetime import date
from pathlib import Path

import pytest

from ledger_core.crm_api import is_probable, name_score, normalize_email, normalize_phone, split_name
from ledger_core.dates import follow_up_due
from ledger_core.routing import company_score, matching_accounts, owner_for, region_for_country

from crm_helpers import *  # noqa: F403

# --------------------------------------------------------------------------
# demo data
# --------------------------------------------------------------------------


def demo_leads(repo: str) -> dict[int, dict]:
    """Rows of data/event_attendees.csv, numbered from 1, minimally cleaned
    (the real parser is Track A's; routing only needs these fields)."""
    with open(Path(repo, "data/event_attendees.csv"), newline="") as f:
        rows = list(csv.reader(f))[1:]
    leads = {}
    for i, row in enumerate(rows, start=1):
        name, email, company, phone, title, country, notes = (c.strip() for c in row)
        first, last = split_name({"name": name})
        leads[i] = {"first_name": first, "last_name": last, "email": normalize_email(email), "company": company,
                    "phone": phone, "title": title, "country": country, "notes": notes}
    return leads


def expected_owners(repo: str) -> dict[int, str | None]:
    """Owner column of the Demo data table; skipped rows are left out."""
    text = Path(repo, "plans/02-features.md").read_text()
    section = text.split("## Demo data", 1)[1]
    out: dict[int, str | None] = {}
    for line in section.splitlines():
        m = re.match(r"\|\s*(\d+)\s*\|", line)
        if not m:
            continue
        cells = [c.strip() for c in line.strip().strip("|").split("|")]
        if "skipped" in cells[3]:
            continue
        out[int(m.group(1))] = None if cells[-1] in ("—", "-", "") else cells[-1]
    return out


def seed_spec(repo: str) -> dict:
    return json.loads(Path(repo, "data/crm_seed.json").read_text())


def offline_existing(lead: dict, spec: dict) -> dict | None:
    """Existing-contact resolution over crm_seed.json with the same rules the
    REST lookup uses (exact email, else a probable fuzzy match)."""
    accounts = {a["key"]: a for a in spec["accounts"]}
    for c in spec["contacts"]:
        acc = accounts[c["account"]]
        rec = {"owner": c["assignedUser"], "accountName": acc["name"], "email": c["emailAddress"]}
        if normalize_email(c["emailAddress"]) == lead["email"]:
            return rec
        cand = {
            "name_score": name_score(lead["first_name"], lead["last_name"], c["firstName"], c["lastName"]),
            "company_score": company_score(lead["company"], acc["name"]),
            "phone_match": bool(lead["phone"]) and normalize_phone(lead["phone"]) == c.get("phoneNumber"),
        }
        if is_probable(cand):
            return rec
    return None


# --------------------------------------------------------------------------
# unit
# --------------------------------------------------------------------------


@pytest.mark.parametrize("country,region", [
    ("United Kingdom", "EMEA"), ("UK", "EMEA"), ("Germany", "EMEA"), ("Netherlands", "EMEA"),
    ("France", "EMEA"), ("spain", "EMEA"), (" United States ", "Americas"), ("USA", "Americas"),
    ("US", "Americas"), ("Canada", "Americas"), ("Brazil", "Americas"), ("", None), (None, None), ("Japan", None),
])
def test_region_for_country(country, region):
    assert region_for_country(country) == region


def test_existing_contact_keeps_owner_over_account_and_region():
    d = owner_for({"country": "Germany", "company": "Fieldstone"}, {"owner": "a.chen"},
                  [{"id": "x", "name": "Fieldstone", "owner": "r.silva"}])
    assert (d.owner, d.reason) == ("a.chen", "existing_contact")


def test_account_owner_wins_over_region():
    d = owner_for({"country": "Germany", "company": "Quarry Data Inc."}, None,
                  [{"id": "q", "name": "Quarry Data", "owner": "a.chen"}, {"id": "z", "name": "Zeta", "owner": "r.silva"}])
    assert (d.owner, d.reason, d.account_id) == ("a.chen", "account", "q")


def test_multiple_accounts_are_ambiguous():
    d = owner_for({"country": "", "company": "Lumen"}, None,
                  [{"id": "1", "name": "Lumen Inc", "owner": "r.silva"}, {"id": "2", "name": "Lumen Health", "owner": "a.chen"}])
    assert d.owner is None and d.reason == "ambiguous_account" and d.ambiguous
    assert {c["id"] for c in d.candidates} == {"1", "2"}


def test_region_routing_and_unknown_region():
    assert owner_for({"country": "UK", "company": "New Co"}).owner == "r.silva"
    assert owner_for({"country": "United States", "company": "New Co"}).owner == "a.chen"
    d = owner_for({"country": "Atlantis", "company": "New Co"})
    assert d.owner is None and d.reason == "unknown_region"


def test_owner_resolved_through_users_by_id():
    d = owner_for({"company": "Acme"}, None, [{"id": "a", "name": "Acme Corp", "assignedUserId": "u1"}], {"u1": "r.silva"})
    assert d.owner == "r.silva"


def test_company_matching():
    assert company_score("Acme Corp", "Acme Corp") == 1.0
    assert company_score("Acme", "Acme Corp") == 1.0  # legal suffix dropped
    assert company_score("Lumen", "Lumen Health") == 0.9  # leading-word prefix
    assert company_score("Northwind", "Fieldstone") < 0.5
    accs = [{"name": "Lumen Inc"}, {"name": "Lumen Health"}, {"name": "Luminous"}]
    assert [a["name"] for a in matching_accounts("Lumen", accs)] == ["Lumen Inc", "Lumen Health"]


def test_routing_reproduces_demo_owner_column_offline(repo):
    leads, spec, want = demo_leads(repo), seed_spec(repo), expected_owners(repo)
    assert want, "Demo data table not found in plans/02-features.md"
    assert set(want) == {1, 2, 3, 4, 5, 7, 8, 9, 10, 12}
    accounts = [{"id": a["key"], "name": a["name"], "owner": a["assignedUser"]} for a in spec["accounts"]]
    got = {}
    for row in want:
        lead = leads[row]
        got[row] = owner_for(lead, offline_existing(lead, spec), accounts).owner
    assert got == want


@pytest.mark.parametrize("event,due", [
    ("2026-10-01", "2026-10-05"),  # Thursday -> Monday
    ("2026-10-02", "2026-10-06"),  # Friday -> Tuesday
    ("2026-10-03", "2026-10-06"),  # Saturday -> Tuesday
    ("2026-10-04", "2026-10-06"),  # Sunday -> Tuesday
    ("2026-10-05", "2026-10-07"),  # Monday -> Wednesday
])
def test_follow_up_due_two_business_days(event, due):
    assert follow_up_due(event).isoformat() == due
    assert follow_up_due(date.fromisoformat(event)) == date.fromisoformat(due)


def test_follow_up_due_custom_days():
    assert follow_up_due("2026-10-02", business_days=5).isoformat() == "2026-10-09"


# --------------------------------------------------------------------------
# against the seeded CRM
# --------------------------------------------------------------------------


@pytest.mark.crm
async def test_routing_reproduces_demo_owner_column_via_rest(repo, reader):
    leads, want = demo_leads(repo), expected_owners(repo)
    got = {}
    for row in want:
        lead = leads[row]
        found = await reader.lookup(lead)
        existing = None
        if found["result"] == "matched":
            existing = await reader.get_contact(found["contact_id"])
        elif found["result"] == "ambiguous" and len(found["candidates"]) == 1:
            # the meta-reviewer confirms the single probable match (demo row 7, 0.91)
            existing = await reader.get_contact(found["candidates"][0]["id"])
        accounts = [] if existing else await reader.accounts_by_name(lead["company"])
        got[row] = owner_for(lead, existing, accounts).owner
    assert got == want
