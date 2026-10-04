"""Async EspoCRM REST clients (plans/01-architecture.md §8, features C3-C6, C11).

Two scoped clients, keys seeded by `make seed` into Redis Keys().crm_secrets:

- CrmReader  (verifier_api_key, read-only role). The only CRM channel the
  verifier and meta-reviewer use.
- CrmWriter  (writer_api_key, read/write role). Only for the api.espocrm
  fallback skill (workers/crm_api_skill.py). Never give it to the verifier.

Records are returned as EspoCRM JSON dicts, enriched with `owner` (the
assigned user's userName) where it can be resolved.
"""

from __future__ import annotations

import json
import re
from datetime import date
from typing import Any

import httpx
import phonenumbers
from rapidfuzz import fuzz

from .keys import Keys
from .routing import company_score, matching_accounts
from .settings import get_settings

CLOSED_STAGES = frozenset({"Closed Won", "Closed Lost"})
PERSONAL_DOMAINS = frozenset({"gmail.com", "outlook.com", "yahoo.com", "icloud.com", "hotmail.com"})


class CrmError(RuntimeError):
    def __init__(self, status: int, message: str) -> None:
        super().__init__(f"EspoCRM {status}: {message}")
        self.status = status


# --------------------------------------------------------------------------
# normalisation (playbook "Dedupe rules")
# --------------------------------------------------------------------------


def normalize_email(email: str | None) -> str:
    return (email or "").strip().lower()


def normalize_phone(phone: str | None, region: str = "US") -> str | None:
    """E.164, default region US; None when it does not parse."""
    if not phone or not str(phone).strip():
        return None
    try:
        num = phonenumbers.parse(str(phone), region)
    except phonenumbers.NumberParseException:
        return None
    return phonenumbers.format_number(num, phonenumbers.PhoneNumberFormat.E164)


def split_name(lead: dict[str, Any]) -> tuple[str, str]:
    """(first, last) from first_name/last_name or a full `name`."""
    first = (lead.get("first_name") or lead.get("firstName") or "").strip()
    last = (lead.get("last_name") or lead.get("lastName") or "").strip()
    if not (first or last):
        full = re.sub(r"\s+", " ", (lead.get("name") or lead.get("full_name") or "").strip())
        if " " in full:
            first, last = full.split(" ", 1)
        else:
            last = full
    return first.title() if first.islower() else first, last.title() if last.islower() else last


def name_score(first_a: str, last_a: str, first_b: str, last_b: str) -> float:
    """0..1 person-name similarity: half last name, half first name.

    First names score 1.0 when equal, 0.85 when one is a >=3-letter prefix of the
    other (nicknames: Ben / Benjamin), else the rapidfuzz ratio.
    """
    fa, fb, la, lb = (s.strip().lower() for s in (first_a, first_b, last_a, last_b))
    last = fuzz.ratio(la, lb) / 100 if la and lb else 0.0
    if fa and fb and fa == fb:
        first = 1.0
    elif fa and fb and min(len(fa), len(fb)) >= 3 and (fa.startswith(fb) or fb.startswith(fa)):
        first = 0.85
    else:
        first = fuzz.ratio(fa, fb) / 100 if fa and fb else 0.0
    return round(0.5 * last + 0.5 * first, 3)


def _where(conditions: list[dict[str, Any]], prefix: str = "where") -> dict[str, Any]:
    """Flatten EspoCRM where-clauses into bracket query params (nested `or` supported)."""
    params: dict[str, Any] = {}

    def walk(node: Any, key: str) -> None:
        if isinstance(node, dict):
            for k, v in node.items():
                walk(v, f"{key}[{k}]")
        elif isinstance(node, list):
            for i, v in enumerate(node):
                walk(v, f"{key}[{i}]")
        else:
            params[key] = node

    for i, cond in enumerate(conditions):
        walk(cond, f"{prefix}[{i}]")
    return params


# --------------------------------------------------------------------------
# clients
# --------------------------------------------------------------------------


class _EspoClient:
    def __init__(
        self,
        api_key: str,
        base_url: str | None = None,
        public_url: str | None = None,
        user_ids: dict[str, str] | None = None,
        http: httpx.AsyncClient | None = None,
        timeout: float = 20.0,
    ) -> None:
        s = get_settings()
        self.base_url = (base_url or s.crm_internal_url).rstrip("/")
        self.public_url = (public_url or s.crm_public_url).rstrip("/")
        self._fallback_user_ids = dict(user_ids or {})
        self._users: dict[str, dict[str, Any]] | None = None
        self.http = http or httpx.AsyncClient(
            base_url=f"{self.base_url}/api/v1", headers={"X-Api-Key": api_key}, timeout=timeout
        )

    async def aclose(self) -> None:
        await self.http.aclose()

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc: object) -> None:
        await self.aclose()

    # -- raw -----------------------------------------------------------------

    async def _request(self, method: str, path: str, **kw: Any) -> Any:
        r = await self.http.request(method, path, **kw)
        if r.status_code >= 400:
            reason = r.headers.get("x-status-reason") or r.text[:200]
            raise CrmError(r.status_code, f"{method} {path}: {reason}")
        return r.json() if r.content else None

    async def list(self, entity: str, where: list[dict] | None = None, select: str | None = None,
                   max_size: int = 200, order_by: str | None = None) -> list[dict]:
        params: dict[str, Any] = {"maxSize": max_size, **_where(where or [])}
        if select:
            params["select"] = select
        if order_by:
            params["orderBy"] = order_by
        data = await self._request("GET", f"/{entity}", params=params)
        return data.get("list", [])

    async def get(self, entity: str, record_id: str) -> dict | None:
        try:
            return await self._request("GET", f"/{entity}/{record_id}")
        except CrmError as e:
            if e.status in (403, 404):
                return None
            raise

    # -- users ---------------------------------------------------------------

    async def users(self) -> dict[str, dict[str, Any]]:
        """{userName: {id, name, type}}. Falls back to the seed's user_ids map."""
        if self._users is None:
            users: dict[str, dict[str, Any]] = {}
            try:
                for u in await self.list("User", select="id,userName,name,type"):
                    users[u["userName"]] = {"id": u["id"], "name": u.get("name"), "type": u.get("type")}
            except CrmError:
                pass
            for user_name, uid in self._fallback_user_ids.items():
                users.setdefault(user_name, {"id": uid, "name": None, "type": None})
            self._users = users
        return self._users

    async def users_by_id(self) -> dict[str, str]:
        return {v["id"]: k for k, v in (await self.users()).items()}

    async def user_id(self, user_name: str) -> str:
        uid = (await self.users()).get(user_name, {}).get("id")
        if not uid:
            raise CrmError(404, f"unknown CRM user {user_name!r}")
        return uid

    async def _with_owner(self, rec: dict | None) -> dict | None:
        if rec is not None and rec.get("assignedUserId"):
            rec["owner"] = (await self.users_by_id()).get(rec["assignedUserId"])
        return rec

    def contact_url(self, contact_id: str) -> str:
        return f"{self.public_url}/#Contact/view/{contact_id}"

    def task_url(self, task_id: str) -> str:
        return f"{self.public_url}/#Task/view/{task_id}"


class CrmReader(_EspoClient):
    """Read-only access (verifier key). No method here writes."""

    async def get_contact(self, contact_id: str) -> dict | None:
        c = await self.get("Contact", contact_id)
        if c is None:
            return None
        c["emails"] = contact_emails(c)
        c["phones"] = contact_phones(c)
        c["url"] = self.contact_url(contact_id)
        return await self._with_owner(c)

    async def find_contacts_by_email(self, email: str) -> list[dict]:
        """Every contact having this address as primary or secondary (case-insensitive)."""
        e = normalize_email(email)
        if not e:
            return []
        rows = await self.list("Contact", [{"type": "equals", "attribute": "emailAddress", "value": e}], select="id")
        out = []
        for row in rows:
            c = await self.get_contact(row["id"])
            if c is not None and e in c["emails"]:
                out.append(c)
        return out

    async def search_contacts(self, name: str = "", company: str = "", phone: str | None = None,
                              first_name: str = "", last_name: str = "") -> list[dict]:
        """Fuzzy candidates by name + company (+ phone), scored with rapidfuzz, best first.

        Each candidate: id, name, accountName, emailAddress, phoneNumber, owner,
        name_score, company_score, phone_match, score.
        """
        if not (first_name or last_name):
            first_name, last_name = split_name({"name": name})
        phone_e164 = normalize_phone(phone)
        ors: list[dict] = []
        if last_name:
            ors.append({"type": "equals", "attribute": "lastName", "value": last_name})
        if len(first_name) >= 3:
            ors.append({"type": "startsWith", "attribute": "firstName", "value": first_name[:3]})
        tokens = re.findall(r"[A-Za-z0-9]+", company or "")
        if tokens:
            ors.append({"type": "contains", "attribute": "accountName", "value": tokens[0]})
        if phone_e164:
            ors.append({"type": "equals", "attribute": "phoneNumber", "value": phone_e164})
        if not ors:
            return []
        rows = await self.list(
            "Contact",
            [{"type": "or", "value": ors}],
            select="id,name,firstName,lastName,accountId,accountName,emailAddress,phoneNumber,assignedUserId",
        )
        by_id = await self.users_by_id()
        out = []
        for c in rows:
            ns = name_score(first_name, last_name, c.get("firstName") or "", c.get("lastName") or "")
            cs = company_score(company, c.get("accountName")) if company else 0.0
            pm = bool(phone_e164) and normalize_phone(c.get("phoneNumber")) == phone_e164
            out.append({
                "id": c["id"],
                "name": c.get("name"),
                "accountId": c.get("accountId"),
                "accountName": c.get("accountName"),
                "emailAddress": c.get("emailAddress"),
                "phoneNumber": c.get("phoneNumber"),
                "owner": by_id.get(c.get("assignedUserId") or ""),
                "name_score": ns,
                "company_score": cs,
                "phone_match": pm,
                "score": round(0.6 * ns + 0.4 * cs, 3),
            })
        return sorted(out, key=lambda c: (-c["score"], c["id"]))

    async def accounts(self) -> list[dict]:
        rows = await self.list(
            "Account", select="id,name,website,billingAddressCountry,description,assignedUserId,assignedUserName"
        )
        return [await self._with_owner(a) for a in rows]

    async def accounts_by_name(self, company: str, threshold: float = 0.9) -> list[dict]:
        """Accounts whose name matches the company (suffix-insensitive, prefix-aware), best first."""
        if not company or not company.strip():
            return []
        token = re.findall(r"[A-Za-z0-9]+", company)
        where = [{"type": "contains", "attribute": "name", "value": token[0]}] if token else []
        rows = await self.list(
            "Account", where,
            select="id,name,website,billingAddressCountry,description,assignedUserId,assignedUserName",
        )
        rows = [await self._with_owner(a) for a in rows]
        return matching_accounts(company, rows, threshold)

    async def tasks_for_contact(self, contact_id: str) -> list[dict]:
        rows = await self.list(
            "Task",
            [{"type": "equals", "attribute": "parentType", "value": "Contact"},
             {"type": "equals", "attribute": "parentId", "value": contact_id}],
            select="id,name,status,dateEnd,dateEndDate,parentType,parentId,contactId,assignedUserId,assignedUserName",
        )
        return [await self._with_owner(t) for t in rows]

    async def open_opportunities_for_contact(self, contact_id: str) -> list[dict]:
        data = await self._request("GET", f"/Contact/{contact_id}/opportunities", params={"maxSize": 200})
        return [o for o in data.get("list", []) if o.get("stage") not in CLOSED_STAGES]

    async def lookup(self, lead: dict[str, Any], threshold: float = 0.85) -> dict[str, Any]:
        """The search-step result for a lead (C3), used by the skill and the check.

        result: "matched"   exact normalised email match (contact_id set);
                "ambiguous" no email match but >=1 probable match (playbook: same
                            company and same phone or name similarity >= threshold);
                            candidates go to review;
                "none"      neither.
        """
        email = normalize_email(lead.get("email"))
        if email:
            hits = await self.find_contacts_by_email(email)
            if hits:
                return {
                    "result": "matched" if len(hits) == 1 else "ambiguous",
                    "match_type": "email",
                    "contact_id": hits[0]["id"] if len(hits) == 1 else None,
                    "candidates": [_cand(c, 1.0) for c in hits],
                }
        first, last = split_name(lead)
        cands = await self.search_contacts(
            company=lead.get("company") or "", phone=lead.get("phone"), first_name=first, last_name=last
        )
        probable = [c for c in cands if is_probable(c, threshold)]
        return {
            "result": "ambiguous" if probable else "none",
            "match_type": "fuzzy" if probable else None,
            "contact_id": None,
            "candidates": probable,
        }


def is_probable(candidate: dict[str, Any], threshold: float = 0.85) -> bool:
    """Playbook probable match: same company and (same phone or name similarity >= threshold)."""
    return candidate.get("company_score", 0) >= 0.9 and (
        bool(candidate.get("phone_match")) or candidate.get("name_score", 0) >= threshold
    )


def _cand(c: dict[str, Any], score: float) -> dict[str, Any]:
    return {"id": c["id"], "name": c.get("name"), "accountName": c.get("accountName"),
            "emailAddress": c.get("emailAddress"), "owner": c.get("owner"), "score": score}


def contact_emails(c: dict[str, Any]) -> list[str]:
    data = c.get("emailAddressData") or []
    emails = [normalize_email(e.get("emailAddress")) for e in data if e.get("emailAddress")]
    primary = normalize_email(c.get("emailAddress"))
    if primary and primary not in emails:
        emails.insert(0, primary)
    return emails


def contact_phones(c: dict[str, Any]) -> list[str]:
    data = c.get("phoneNumberData") or []
    phones = [normalize_phone(p.get("phoneNumber")) for p in data if p.get("phoneNumber")]
    primary = normalize_phone(c.get("phoneNumber"))
    if primary and primary not in phones:
        phones.insert(0, primary)
    return [p for p in phones if p]


def fillable(contact: dict[str, Any], field_name: str) -> bool:
    """True when enrichment may set this field: it is empty, and storable.
    EspoCRM keeps a contact's `title` on its account link (AccountContact.role),
    so a contact without an account cannot hold one."""
    if field_name == "title" and not contact.get("accountId"):
        return False
    return contact.get(field_name) in (None, "")


class CrmWriter(CrmReader):
    """Read/write access (writer key). Only the api.espocrm fallback skill gets one."""

    async def create_contact(
        self,
        *,
        first_name: str,
        last_name: str,
        email: str | None = None,
        phone: str | None = None,
        title: str | None = None,
        account_id: str | None = None,
        owner: str | None = None,
        description: str | None = None,
    ) -> dict:
        data: dict[str, Any] = {"firstName": first_name, "lastName": last_name or first_name}
        if email:
            data["emailAddress"] = normalize_email(email)
        if phone:
            data["phoneNumber"] = normalize_phone(phone) or phone
        if title:
            data["title"] = title
        if account_id:
            data["accountId"] = account_id
        if owner:
            data["assignedUserId"] = await self.user_id(owner)
        if description:
            data["description"] = description
        # Dedupe is by email (check-then-act in the skill); EspoCRM's own
        # name-based duplicate check would 409 on namesakes, so skip it.
        created = await self._request("POST", "/Contact", json=data, headers={"X-Skip-Duplicate-Check": "true"})
        return await self.get_contact(created["id"]) or created

    async def update_contact(
        self,
        contact_id: str,
        *,
        add_emails: list[str] | None = None,
        add_phones: list[str] | None = None,
        fill: dict[str, Any] | None = None,
    ) -> dict:
        """Enrich without overwriting: append secondary emails/phones that are not
        present yet, and set `fill` fields only where the CRM value is empty.
        Returns {"contact": ..., "changed": [field, ...]}."""
        current = await self.get_contact(contact_id)
        if current is None:
            raise CrmError(404, f"contact {contact_id} not found")
        patch: dict[str, Any] = {}
        changed: list[str] = []

        new_emails = [normalize_email(e) for e in (add_emails or []) if normalize_email(e)]
        new_emails = [e for i, e in enumerate(new_emails) if e not in current["emails"] and e not in new_emails[:i]]
        if new_emails:
            ead = [dict(x) for x in (current.get("emailAddressData") or [])]
            for x in ead:
                x.pop("lower", None)
            ead += [{"emailAddress": e, "primary": not ead and i == 0, "optOut": False, "invalid": False}
                    for i, e in enumerate(new_emails)]
            patch["emailAddressData"] = ead
            changed += [f"email:{e}" for e in new_emails]

        new_phones = [p for p in (normalize_phone(x) for x in (add_phones or [])) if p]
        new_phones = [p for i, p in enumerate(new_phones) if p not in current["phones"] and p not in new_phones[:i]]
        if new_phones:
            pnd = [dict(x) for x in (current.get("phoneNumberData") or [])]
            pnd += [{"phoneNumber": p, "type": "Mobile", "primary": not pnd and i == 0, "optOut": False,
                     "invalid": False} for i, p in enumerate(new_phones)]
            patch["phoneNumberData"] = pnd
            changed += [f"phone:{p}" for p in new_phones]

        for field_name, value in (fill or {}).items():
            if value not in (None, "") and fillable(current, field_name):
                patch[field_name] = value
                changed.append(field_name)

        if patch:
            await self._request("PUT", f"/Contact/{contact_id}", json=patch)
            current = await self.get_contact(contact_id) or current
        return {"contact": current, "changed": changed}

    async def create_task(
        self,
        *,
        contact_id: str,
        subject: str,
        due: date | str,
        owner: str,
        description: str | None = None,
    ) -> dict:
        due_s = due.isoformat() if isinstance(due, date) else str(due)[:10]
        data: dict[str, Any] = {
            "name": subject,
            "parentType": "Contact",
            "parentId": contact_id,
            "assignedUserId": await self.user_id(owner),
            "dateEndDate": due_s,
            "dateEnd": None,
            "status": "Not Started",
        }
        if description:
            data["description"] = description
        task = await self._request("POST", "/Task", json=data)
        return await self._with_owner(task) or task


# --------------------------------------------------------------------------
# factories (keys from Redis, written by `make seed`)
# --------------------------------------------------------------------------


async def crm_secrets(redis: Any, keys: Keys | None = None) -> dict[str, str]:
    keys = keys or Keys(get_settings().ledger_ns)
    secrets = await redis.hgetall(keys.crm_secrets)
    if not secrets:
        raise CrmError(503, f"CRM not seeded: {keys.crm_secrets} is empty (run `make seed`)")
    return secrets


def _user_ids(secrets: dict[str, str]) -> dict[str, str]:
    try:
        return json.loads(secrets.get("user_ids") or "{}")
    except ValueError:
        return {}


async def reader_from_redis(redis: Any, keys: Keys | None = None) -> CrmReader:
    s = await crm_secrets(redis, keys)
    return CrmReader(s["verifier_api_key"], public_url=s.get("public_url") or None, user_ids=_user_ids(s))


async def writer_from_redis(redis: Any, keys: Keys | None = None) -> CrmWriter:
    s = await crm_secrets(redis, keys)
    return CrmWriter(s["writer_api_key"], public_url=s.get("public_url") or None, user_ids=_user_ids(s))
