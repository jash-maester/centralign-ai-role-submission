"""Track C: pure helpers of the browser operator (no browser, no CRM)."""

from __future__ import annotations

from datetime import date

import pytest

formats = pytest.importorskip("browser_worker.formats")
recovery = pytest.importorskip("browser_worker.recovery")
evidence = pytest.importorskip("browser_worker.evidence")

pytestmark = pytest.mark.browser


@pytest.mark.parametrize(
    "fmt,expected",
    [
        ("DD.MM.YYYY", "07.10.2026"),
        ("MM/DD/YYYY", "10/07/2026"),
        ("DD/MM/YYYY", "07/10/2026"),
        ("YYYY-MM-DD", "2026-10-07"),
        ("DD. MM. YYYY", "07. 10. 2026"),
        ("D.M.YYYY", "7.10.2026"),
        ("MMM D, YYYY", "Oct 7, 2026"),
        (None, "07.10.2026"),
    ],
)
def test_format_date(fmt, expected):
    assert formats.format_date(date(2026, 10, 7), fmt) == expected


def test_parse_iso_date_accepts_date_and_datetime_strings():
    assert formats.parse_iso_date("2026-10-07") == date(2026, 10, 7)
    assert formats.parse_iso_date("2026-10-07T09:00:00Z") == date(2026, 10, 7)
    assert formats.parse_iso_date(date(2026, 1, 2)) == date(2026, 1, 2)


@pytest.mark.parametrize(
    "raw,expected",
    [
        ("+1 415 555 0119", "+14155550119"),
        ("(415) 555-0182", "+14155550182"),
        ("+44 20 7946 0958", "+442079460958"),
        ("", None),
        ("n/a", None),
    ],
)
def test_to_e164(raw, expected):
    assert formats.to_e164(raw) == expected


def test_name_and_candidate_scores():
    assert formats.name_score("Ben Ortiz", "Benjamin Ortiz") >= 0.9
    assert formats.name_score("Ben Ortiz", "Maya Patel") < 0.5
    assert formats.candidate_score("Ben Ortiz", "Quarry Data", "Benjamin Ortiz", "Quarry Data") >= 0.9
    assert formats.candidate_score("Ben Ortiz", "Quarry Data", "Benjamin Ortiz", "Lumen Inc") < 0.75
    assert formats.norm_email("  MARCUS.LEE@ACME.COM ") == "marcus.lee@acme.com"


def test_default_recovery_policy_is_small_and_bounded():
    S, A = recovery.PageState, recovery.RecoveryAction
    choose = recovery.default_chooser
    assert choose(S.LOGIN, 1, "401") is A.RELOGIN
    assert choose(S.ERROR, 1, "500") is A.RELOAD
    assert choose(S.UNKNOWN, 2, "timeout") is A.HOME
    assert choose(S.MODAL, 3, "timeout") is A.GIVE_UP
    assert choose(S.LOGIN, 4, "401") is A.GIVE_UP
    assert {a.value for a in A} == {"reload", "relogin", "home", "give_up"}


def test_skill_result_shape_and_safe_labels():
    r = evidence.SkillResult(skill="crm.create_contact")
    r.observe("contact.list", "0 rows", query="x")
    d = r.as_dict()
    assert set(d) >= {"acted", "record_id", "observations", "screenshots"}
    assert d["observations"][0]["page"] == "contact.list" and "ts" in d["observations"][0]
    assert evidence.safe_label("stp_1/../a b") == "stp_1-..-a-b"
    assert "/" not in evidence.safe_label("../../etc/passwd")
