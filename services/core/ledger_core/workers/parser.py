"""Parser worker: messy attendee CSV -> normalised leads (C1). Deterministic, no LLM.

Rules from playbooks/event-leads.md "Dedupe rules":
- map messy headers onto canonical columns (" E-mail Address " -> email, ...)
- trim every field; lowercase emails; title-case names written all in lower
  or upper case (mixed-case names such as "McDonald" are kept); phones to
  E.164 (default region from the row's country, else US)
- the same person twice in one file is one lead: keep the first row
  (same normalised email)
- rows with no email are phone-only: flagged, not usable (the meta-reviewer
  decides to skip them); rows with neither email nor phone are flagged too

parse_file(path) -> ParseResult; handle(step, ctx) is the worker handler.
Row numbers are 1-based data rows (the header is row 0).
"""

from __future__ import annotations

import csv
import hashlib
import os
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import phonenumbers

CANONICAL = ("name", "email", "company", "phone", "title", "country", "notes")

# header with everything but letters removed, lowercased -> canonical column
HEADER_ALIASES: dict[str, str] = {
    "name": "name", "fullname": "name", "attendee": "name", "attendeename": "name", "contactname": "name",
    "email": "email", "emailaddress": "email", "mail": "email", "workemail": "email",
    "company": "company", "companyorg": "company", "organization": "company", "organisation": "company",
    "org": "company", "account": "company", "companyname": "company",
    "phone": "phone", "phonenumber": "phone", "mobile": "phone", "tel": "phone", "telephone": "phone",
    "title": "title", "jobtitle": "title", "role": "title", "position": "title",
    "country": "country", "countryregion": "country", "location": "country",
    "notes": "notes", "note": "notes", "comments": "notes", "comment": "notes",
}

# country spelling -> (canonical name, ISO region for phonenumbers, sales region)
_EMEA, _AMER = "EMEA", "Americas"
COUNTRIES: dict[str, tuple[str, str, str]] = {
    "united states": ("United States", "US", _AMER), "usa": ("United States", "US", _AMER),
    "us": ("United States", "US", _AMER), "u.s.": ("United States", "US", _AMER),
    "u.s.a.": ("United States", "US", _AMER), "america": ("United States", "US", _AMER),
    "canada": ("Canada", "CA", _AMER), "mexico": ("Mexico", "MX", _AMER), "brazil": ("Brazil", "BR", _AMER),
    "argentina": ("Argentina", "AR", _AMER), "chile": ("Chile", "CL", _AMER), "colombia": ("Colombia", "CO", _AMER),
    "united kingdom": ("United Kingdom", "GB", _EMEA), "uk": ("United Kingdom", "GB", _EMEA),
    "u.k.": ("United Kingdom", "GB", _EMEA), "great britain": ("United Kingdom", "GB", _EMEA),
    "england": ("United Kingdom", "GB", _EMEA),
    "germany": ("Germany", "DE", _EMEA), "deutschland": ("Germany", "DE", _EMEA),
    "netherlands": ("Netherlands", "NL", _EMEA), "the netherlands": ("Netherlands", "NL", _EMEA),
    "holland": ("Netherlands", "NL", _EMEA), "france": ("France", "FR", _EMEA), "spain": ("Spain", "ES", _EMEA),
    "italy": ("Italy", "IT", _EMEA), "ireland": ("Ireland", "IE", _EMEA), "belgium": ("Belgium", "BE", _EMEA),
    "portugal": ("Portugal", "PT", _EMEA), "austria": ("Austria", "AT", _EMEA), "sweden": ("Sweden", "SE", _EMEA),
    "denmark": ("Denmark", "DK", _EMEA), "finland": ("Finland", "FI", _EMEA), "poland": ("Poland", "PL", _EMEA),
    "switzerland": ("Switzerland", "CH", _EMEA), "norway": ("Norway", "NO", _EMEA),
}

DEFAULT_PHONE_REGION = "US"


@dataclass
class ParseResult:
    source: str
    sha256: str
    total_rows: int
    rows: list[dict[str, Any]] = field(default_factory=list)  # usable, normalised
    flagged: list[dict[str, Any]] = field(default_factory=list)  # {row, reason, detail, record}
    stats: dict[str, Any] = field(default_factory=dict)

    def as_claim_data(self) -> dict[str, Any]:
        return {"source": self.source, "sha256": self.sha256, "total_rows": self.total_rows,
                "rows": self.rows, "flagged": self.flagged, "stats": self.stats}


def canon_header(raw: str) -> str | None:
    return HEADER_ALIASES.get(re.sub(r"[^a-z]", "", raw.lower()))


def _clean(value: str | None) -> str:
    return re.sub(r"\s+", " ", (value or "").strip())


def normalise_name(raw: str) -> str:
    name = _clean(raw)
    if name and (name == name.lower() or name == name.upper()):
        return re.sub(r"[^\W\d_]+", lambda m: m[0].capitalize(), name.lower())
    return name


def normalise_country(raw: str) -> tuple[str | None, str | None, str | None]:
    """-> (country, iso region, sales region); unknown countries keep their text."""
    c = _clean(raw)
    if not c:
        return None, None, None
    hit = COUNTRIES.get(c.lower())
    return hit if hit else (c, None, None)


def normalise_phone(raw: str, iso_region: str | None) -> str | None:
    text = _clean(raw)
    if not text:
        return None
    try:
        num = phonenumbers.parse(text, iso_region or DEFAULT_PHONE_REGION)
    except phonenumbers.NumberParseException:
        return None
    if not phonenumbers.is_possible_number(num):
        return None
    return phonenumbers.format_number(num, phonenumbers.PhoneNumberFormat.E164)


def file_sha256(path: str | Path) -> str:
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def parse_file(path: str | Path) -> ParseResult:
    path = Path(path)
    with path.open(newline="", encoding="utf-8-sig") as f:
        reader = csv.reader(f)
        header = next(reader, [])
        raw_rows = [row for row in reader if any(cell.strip() for cell in row)]

    mapping: dict[int, str] = {}
    headers_mapped: dict[str, str] = {}
    for i, h in enumerate(header):
        c = canon_header(h)
        if c and c not in mapping.values():
            mapping[i] = c
            headers_mapped[h] = c
    unmapped = [h for i, h in enumerate(header) if i not in mapping]

    res = ParseResult(source=path.name, sha256=file_sha256(path), total_rows=len(raw_rows))
    stats = {"emails_lowercased": 0, "phones_normalised": 0, "phones_unparseable": 0,
             "fields_trimmed": 0, "names_title_cased": 0}
    seen_email: dict[str, int] = {}

    for n, raw in enumerate(raw_rows, start=1):
        rec_raw = {col: (raw[i] if i < len(raw) else "") for i, col in mapping.items()}
        for col in CANONICAL:
            rec_raw.setdefault(col, "")
        stats["fields_trimmed"] += sum(1 for v in rec_raw.values() if v != _clean(v))

        name = normalise_name(rec_raw["name"])
        if name != _clean(rec_raw["name"]):
            stats["names_title_cased"] += 1
        email = _clean(rec_raw["email"]).lower() or None
        if email and email != _clean(rec_raw["email"]):
            stats["emails_lowercased"] += 1
        country, iso, region = normalise_country(rec_raw["country"])
        phone = normalise_phone(rec_raw["phone"], iso)
        if phone:
            if phone != _clean(rec_raw["phone"]):
                stats["phones_normalised"] += 1
        elif _clean(rec_raw["phone"]):
            stats["phones_unparseable"] += 1
        first, _, last = name.partition(" ")
        record = {
            "row": n, "name": name, "first_name": first, "last_name": last, "email": email,
            "company": _clean(rec_raw["company"]) or None, "phone": phone,
            "phone_raw": _clean(rec_raw["phone"]) or None, "title": _clean(rec_raw["title"]) or None,
            "country": country, "region": region, "notes": _clean(rec_raw["notes"]) or None,
            "email_domain": email.split("@", 1)[1] if email and "@" in email else None,
        }

        if email and email in seen_email:
            res.flagged.append({"row": n, "reason": "duplicate_in_file", "duplicate_of": seen_email[email],
                                "detail": f"same email as row {seen_email[email]}; keeping the first row",
                                "record": record})
        elif not email and phone:
            res.flagged.append({"row": n, "reason": "phone_only",
                                "detail": "no email address; phone-only rows are skipped unless the company "
                                          "is a strategic account (playbook Dedupe rules)", "record": record})
        elif not email:
            res.flagged.append({"row": n, "reason": "no_contact", "detail": "neither email nor phone",
                                "record": record})
        elif "@" not in email:
            res.flagged.append({"row": n, "reason": "invalid_email", "detail": f"{email!r} is not an email",
                                "record": record})
        else:
            seen_email[email] = n
            res.rows.append(record)

    reasons: dict[str, int] = {}
    for f in res.flagged:
        reasons[f["reason"]] = reasons.get(f["reason"], 0) + 1
    res.stats = {"headers_mapped": headers_mapped, "headers_unmapped": unmapped, **stats,
                 "total": res.total_rows, "usable": len(res.rows), "flagged": len(res.flagged),
                 "flagged_by_reason": reasons}
    return res


def resolve_path(file: str, data_dir: str | None = None) -> Path:
    """Inputs name files relative to DATA_DIR; absolute paths are used as is."""
    p = Path(file)
    if p.is_absolute():
        return p
    return Path(data_dir or os.environ.get("DATA_DIR", "/app/data")) / p


def make_handler(data_dir: str | None = None):
    """Worker handler for kind file.parse. Step inputs: {"file": "<name under DATA_DIR>"}."""
    from ..worker_base import WorkContext, WorkResult

    async def handle(step, ctx: WorkContext) -> WorkResult:
        file = ctx.inputs.get("file") or ctx.inputs.get("input_file")
        if not file:
            raise ValueError("file.parse step needs inputs.file")
        res = parse_file(resolve_path(file, data_dir))
        await ctx.observe({"parsed": res.source, "total": res.total_rows, "usable": len(res.rows),
                           "flagged": [{"row": f["row"], "reason": f["reason"]} for f in res.flagged]})
        return WorkResult(
            summary=f"parsed {res.total_rows} rows from {res.source}: {len(res.rows)} usable, "
                    f"{len(res.flagged)} flagged",
            data=res.as_claim_data(),
        )

    return handle
