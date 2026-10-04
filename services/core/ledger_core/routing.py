"""Owner routing per playbooks/event-leads.md "Owner routing". Pure functions.

Order of precedence:
1. an existing contact keeps its current owner;
2. if the lead's company matches exactly one CRM account, the account owner wins
   (and the contact is linked to that account);
3. if it matches more than one account: ambiguous, never guess (review);
4. otherwise route by region: EMEA -> r.silva, Americas -> a.chen.
A lead with no recognisable region and no account gets no owner (review).
"""

from __future__ import annotations

import re
from collections.abc import Iterable, Mapping
from dataclasses import asdict, dataclass
from typing import Any

from rapidfuzz import fuzz

REGION_OWNERS: dict[str, str] = {"EMEA": "r.silva", "Americas": "a.chen"}

_EU = {
    "austria", "belgium", "bulgaria", "croatia", "cyprus", "czechia", "czech republic", "denmark",
    "estonia", "finland", "france", "germany", "greece", "hungary", "ireland", "italy", "latvia",
    "lithuania", "luxembourg", "malta", "netherlands", "poland", "portugal", "romania", "slovakia",
    "slovenia", "spain", "sweden",
}
_EMEA = _EU | {
    "united kingdom", "uk", "u.k.", "gb", "great britain", "britain", "england", "scotland", "wales",
    "northern ireland", "holland", "the netherlands", "deutschland", "switzerland", "norway", "iceland",
    "israel", "united arab emirates", "uae", "saudi arabia", "qatar", "turkey", "egypt",
    "south africa", "nigeria", "kenya", "morocco",
    "de", "nl", "fr",
}
_AMERICAS = {
    "united states", "united states of america", "us", "u.s.", "usa", "u.s.a.", "america",
    "canada", "mexico", "brazil", "argentina", "chile", "colombia", "peru", "uruguay", "paraguay",
    "bolivia", "ecuador", "venezuela", "costa rica", "panama", "guatemala", "puerto rico",
    "latin america", "latam",
}

_LEGAL_SUFFIXES = {
    "inc", "incorporated", "corp", "corporation", "co", "company", "ltd", "limited", "llc", "plc",
    "gmbh", "ag", "sa", "bv", "nv", "srl", "group",
}


def region_for_country(country: str | None) -> str | None:
    """'EMEA', 'Americas' or None when the country is missing or unknown."""
    if not country:
        return None
    c = re.sub(r"\s+", " ", country.strip().lower())
    if c in _EMEA:
        return "EMEA"
    if c in _AMERICAS:
        return "Americas"
    return None


def company_tokens(name: str | None) -> list[str]:
    """Lowercase word tokens with legal suffixes removed ('Acme Corp' -> ['acme'])."""
    words = re.findall(r"[a-z0-9]+", (name or "").lower())
    core = [w for w in words if w not in _LEGAL_SUFFIXES]
    return core or words


def company_score(company: str | None, account_name: str | None) -> float:
    """0..1 similarity of a lead's company to an account name.

    1.0 for the same name once legal suffixes are dropped; 0.9 when one name is a
    leading-word prefix of the other ('Lumen' vs 'Lumen Health'); else rapidfuzz.
    """
    a, b = company_tokens(company), company_tokens(account_name)
    if not a or not b:
        return 0.0
    if a == b:
        return 1.0
    short, long_ = (a, b) if len(a) <= len(b) else (b, a)
    if long_[: len(short)] == short:
        return 0.9
    return round(fuzz.token_sort_ratio(" ".join(a), " ".join(b)) / 100, 3)


def matching_accounts(company: str | None, accounts: Iterable[Mapping[str, Any]], threshold: float = 0.9) -> list[dict]:
    """Accounts whose name matches the company (score >= threshold), best first."""
    scored = []
    for acc in accounts:
        s = company_score(company, acc.get("name"))
        if s >= threshold:
            scored.append({**acc, "match_score": s})
    return sorted(scored, key=lambda a: (-a["match_score"], a.get("name") or ""))


@dataclass
class OwnerDecision:
    owner: str | None  # EspoCRM userName, None when undecided
    reason: str  # existing_contact | account | region | ambiguous_account | unknown_region
    region: str | None = None
    account_id: str | None = None
    account_name: str | None = None
    candidates: list[dict] | None = None  # the competing accounts when ambiguous

    @property
    def ambiguous(self) -> bool:
        return self.owner is None

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


def _owner_of(record: Mapping[str, Any], users_by_id: Mapping[str, str] | None) -> str | None:
    if record.get("owner"):
        return record["owner"]
    uid = record.get("assignedUserId")
    return (users_by_id or {}).get(uid) if uid else None


def owner_for(
    lead: Mapping[str, Any],
    existing_contact: Mapping[str, Any] | None = None,
    accounts: Iterable[Mapping[str, Any]] = (),
    users_by_id: Mapping[str, str] | None = None,
) -> OwnerDecision:
    """Apply the playbook routing.

    `lead` needs `country` and `company`; `accounts` are CRM accounts (any list:
    only the ones whose name matches the lead's company are considered). Records
    carry the owner as `owner` (userName) or `assignedUserId` + `users_by_id`.
    """
    region = region_for_country(lead.get("country"))
    if existing_contact:
        return OwnerDecision(
            owner=_owner_of(existing_contact, users_by_id),
            reason="existing_contact",
            region=region,
            account_id=existing_contact.get("accountId"),
            account_name=existing_contact.get("accountName"),
        )
    matches = matching_accounts(lead.get("company"), accounts)
    if len(matches) > 1:
        return OwnerDecision(
            owner=None,
            reason="ambiguous_account",
            region=region,
            candidates=[
                {"id": a.get("id"), "name": a.get("name"), "owner": _owner_of(a, users_by_id),
                 "country": a.get("billingAddressCountry"), "match_score": a["match_score"]}
                for a in matches
            ],
        )
    if matches:
        acc = matches[0]
        return OwnerDecision(
            owner=_owner_of(acc, users_by_id),
            reason="account",
            region=region,
            account_id=acc.get("id"),
            account_name=acc.get("name"),
        )
    if region is None:
        return OwnerDecision(owner=None, reason="unknown_region")
    return OwnerDecision(owner=REGION_OWNERS[region], reason="region", region=region)
