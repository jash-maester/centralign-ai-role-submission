"""Pure helpers: date formats, phone and email normalisation, candidate scoring.

No Playwright here, so these are unit-testable without a browser.
"""

from __future__ import annotations

import re
from datetime import date

import phonenumbers
from rapidfuzz import fuzz

DEFAULT_DATE_FORMAT = "DD.MM.YYYY"  # EspoCRM's install default

_MONTHS = [
    "January", "February", "March", "April", "May", "June",
    "July", "August", "September", "October", "November", "December",
]  # fmt: skip
_TOKEN = re.compile(r"YYYY|YY|MMMM|MMM|MM|M|DD|Do|D")


def _ordinal(n: int) -> str:
    if 10 <= n % 100 <= 20:
        return f"{n}th"
    return f"{n}{ {1: 'st', 2: 'nd', 3: 'rd'}.get(n % 10, 'th') }"


def format_date(d: date, moment_format: str | None) -> str:
    """Render `d` the way the EspoCRM UI expects it (moment.js tokens)."""
    fmt = moment_format or DEFAULT_DATE_FORMAT
    repl = {
        "YYYY": f"{d.year:04d}",
        "YY": f"{d.year % 100:02d}",
        "MMMM": _MONTHS[d.month - 1],
        "MMM": _MONTHS[d.month - 1][:3],
        "MM": f"{d.month:02d}",
        "M": str(d.month),
        "DD": f"{d.day:02d}",
        "Do": _ordinal(d.day),
        "D": str(d.day),
    }
    return _TOKEN.sub(lambda m: repl[m.group(0)], fmt)


def parse_iso_date(value: str | date) -> date:
    if isinstance(value, date):
        return value
    return date.fromisoformat(value[:10])


def norm_email(email: str | None) -> str:
    return (email or "").strip().lower()


def to_e164(phone: str | None, default_region: str = "US") -> str | None:
    """'+1 (415) 555-0119' -> '+14155550119'. None when unparseable."""
    if not phone or not phone.strip():
        return None
    raw = phone.strip()
    try:
        num = phonenumbers.parse(raw, None if raw.startswith("+") else default_region)
    except phonenumbers.NumberParseException:
        return None
    if not phonenumbers.is_possible_number(num):
        return None
    return phonenumbers.format_number(num, phonenumbers.PhoneNumberFormat.E164)


def name_score(query: str, candidate: str) -> float:
    """0..1 similarity of person names; 'Ben Ortiz' ~ 'Benjamin Ortiz' scores high."""
    q = query.lower().split()
    c = candidate.lower().split()
    if not q or not c:
        return 0.0
    base = fuzz.token_sort_ratio(" ".join(q), " ".join(c)) / 100
    # Same last name and first name is a prefix (nickname / short form).
    if len(q) >= 2 and len(c) >= 2 and q[-1] == c[-1] and (c[0].startswith(q[0]) or q[0].startswith(c[0])):
        base = max(base, 0.9)
    return round(base, 3)


def company_score(query: str | None, account: str | None) -> float:
    if not query or not account:
        return 0.0
    return round(fuzz.token_set_ratio(query.lower(), account.lower()) / 100, 3)


def candidate_score(name: str, company: str | None, cand_name: str, cand_account: str | None) -> float:
    n = name_score(name, cand_name)
    if not company:
        return n
    return round(0.6 * n + 0.4 * company_score(company, cand_account), 3)
