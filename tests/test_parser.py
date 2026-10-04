"""C1: deterministic parsing and normalisation of the messy attendee CSV."""

from __future__ import annotations

from pathlib import Path

import pytest

from ledger_core.workers.parser import (
    canon_header,
    normalise_country,
    normalise_name,
    normalise_phone,
    parse_file,
)


@pytest.fixture(scope="module")
def demo():
    return parse_file(Path("/repo/data/event_attendees.csv"))


def _row(res, n):
    return next(x for x in res.rows if x["row"] == n)


def test_twelve_rows_ten_usable(demo):
    assert demo.total_rows == 12
    assert len(demo.rows) == 10
    assert [x["row"] for x in demo.rows] == [1, 2, 3, 4, 5, 7, 8, 9, 10, 12]
    assert demo.stats["usable"] == 10 and demo.stats["flagged"] == 2


def test_row6_is_in_file_duplicate_of_row3(demo):
    [dup] = [f for f in demo.flagged if f["reason"] == "duplicate_in_file"]
    assert dup["row"] == 6 and dup["duplicate_of"] == 3
    assert dup["record"]["email"] == "lena@kestrel-labs.io"


def test_row11_is_phone_only(demo):
    [po] = [f for f in demo.flagged if f["reason"] == "phone_only"]
    assert po["row"] == 11 and po["record"]["phone"] == "+14155550182" and po["record"]["email"] is None
    assert "phone-only" in po["detail"]


def test_messy_headers_are_mapped(demo):
    m = demo.stats["headers_mapped"]
    assert m[" E-mail Address "] == "email" and m["Full Name"] == "name"
    assert m["Company / Org"] == "company" and m["Phone #"] == "phone"
    assert demo.stats["headers_unmapped"] == []


def test_normalisation_on_real_rows(demo):
    marcus = _row(demo, 2)
    assert marcus["name"] == "Marcus Lee" and marcus["email"] == "marcus.lee@acme.com"
    assert marcus["phone"] == "+442079460958" and marcus["country"] == "United Kingdom"
    assert marcus["region"] == "EMEA" and marcus["first_name"] == "Marcus"
    assert _row(demo, 5)["name"] == "Tom Becker"  # trailing space trimmed
    assert _row(demo, 8)["email"] == "hannah@fieldstone.dev"
    assert _row(demo, 1)["phone"] == "+14155550101" and _row(demo, 1)["region"] == "Americas"
    assert _row(demo, 4)["phone"] == "+16175550144"  # dotted US number
    assert _row(demo, 10)["country"] == "United States"  # " United States " trimmed
    assert _row(demo, 7)["email_domain"] == "gmail.com"
    sam = _row(demo, 9)
    assert sam["phone"] is None and sam["country"] is None and sam["region"] is None
    assert sam["notes"] == 'Badge says "Lumen"'


def test_stats(demo):
    s = demo.stats
    assert s["emails_lowercased"] == 3  # rows 2, 6, 8
    assert s["phones_normalised"] == 11
    assert s["fields_trimmed"] == 3
    assert s["names_title_cased"] == 1
    assert s["flagged_by_reason"] == {"duplicate_in_file": 1, "phone_only": 1}
    assert len(demo.sha256) == 64


@pytest.mark.parametrize("raw,want", [
    (" E-mail Address ", "email"), ("Email", "email"), ("Full Name", "name"), ("Company / Org", "company"),
    ("Phone #", "phone"), ("Job Title", "title"), ("Country", "country"), ("Notes", "notes"), ("Badge ID", None),
])
def test_canon_header(raw, want):
    assert canon_header(raw) == want


def test_name_casing_keeps_mixed_case():
    assert normalise_name("  marcus  lee ") == "Marcus Lee"
    assert normalise_name("ANNE-MARIE O'NEIL") == "Anne-Marie O'Neil"
    assert normalise_name("Jan McDonald") == "Jan McDonald"


def test_phone_and_country():
    assert normalise_country("UK") == ("United Kingdom", "GB", "EMEA")
    assert normalise_country("usa")[0] == "United States"
    assert normalise_country("Atlantis") == ("Atlantis", None, None)
    assert normalise_phone("020 7946 0958", "GB") == "+442079460958"
    assert normalise_phone("(415) 555-0101", None) == "+14155550101"  # default region US
    assert normalise_phone("12", "US") is None
    assert normalise_phone("", "US") is None


def test_other_messy_file(tmp_path):
    p = tmp_path / "x.csv"
    p.write_text(
        "Name,EMAIL,Organization,Mobile,Country\n"
        "ada lovelace, ADA@Example.com ,Engines,020 7946 0000,uk\n"
        ",,,,\n"
        "Bob,,Acme,,US\n"
        "Ada Lovelace,ada@example.com,Engines,,UK\n"
    )
    res = parse_file(p)
    assert res.total_rows == 3  # blank line ignored
    assert [x["row"] for x in res.rows] == [1]
    assert res.rows[0]["email"] == "ada@example.com" and res.rows[0]["phone"] == "+442079460000"
    assert {f["row"]: f["reason"] for f in res.flagged} == {2: "no_contact", 3: "duplicate_in_file"}
