"""Seed EspoCRM for the demo (Phase 0). Idempotent: run it any number of times.

Creates, by find-or-create:
- sales users a.chen, r.silva (owners) and the admin-type browser operator;
- two API users with their own roles:
    ledger-verifier  read-only  -> verifier + meta-reviewer (the "different channel")
    ledger-writer    read/write -> only the api.espocrm fallback skill (C11)
- accounts, contacts and the open deal from data/crm_seed.json.

API keys are written to Redis (Keys().crm_secrets) for the services to read.
"""

from __future__ import annotations

import hashlib
import json
import logging
import secrets
import sys
import time
from pathlib import Path
from typing import Any

import httpx
import redis

from .keys import Keys
from .settings import get_settings

log = logging.getLogger("seed")

ENTITIES = ("Contact", "Account", "Task", "Opportunity", "Lead")
READ_ONLY = {"create": "no", "read": "all", "edit": "no", "delete": "no", "stream": "all"}
READ_WRITE = {"create": "yes", "read": "all", "edit": "all", "delete": "no", "stream": "all"}
# Both API users may read the user list (owner lookup by userName); never edit it.
USER_READ = {"read": "all", "edit": "no"}


class Espo:
    def __init__(self, base_url: str, user: str, password: str) -> None:
        self.http = httpx.Client(base_url=f"{base_url}/api/v1", auth=(user, password), timeout=30)

    def wait_ready(self, timeout_s: int = 300) -> None:
        deadline = time.time() + timeout_s
        while True:
            try:
                r = self.http.get("/App/user")
                if r.status_code == 200:
                    return
                log.info("waiting for EspoCRM (HTTP %s)", r.status_code)
            except httpx.HTTPError as e:
                log.info("waiting for EspoCRM (%s)", e.__class__.__name__)
            if time.time() > deadline:
                raise SystemExit("EspoCRM did not become ready")
            time.sleep(3)

    def find(self, entity: str, attribute: str, value: Any) -> dict | None:
        params = {
            "where[0][type]": "equals",
            "where[0][attribute]": attribute,
            "where[0][value]": value,
            "maxSize": 2,
        }
        r = self.http.get(f"/{entity}", params=params)
        r.raise_for_status()
        rows = r.json().get("list", [])
        return rows[0] if rows else None

    def get(self, entity: str, record_id: str) -> dict:
        r = self.http.get(f"/{entity}/{record_id}")
        r.raise_for_status()
        return r.json()

    def create(self, entity: str, data: dict) -> dict:
        r = self.http.post(f"/{entity}", json=data, headers={"X-Skip-Duplicate-Check": "true"})
        if r.status_code >= 400:
            raise RuntimeError(f"create {entity} failed: {r.status_code} {r.text[:300]}")
        return r.json()

    def update(self, entity: str, record_id: str, data: dict) -> dict:
        r = self.http.put(f"/{entity}/{record_id}", json=data)
        if r.status_code >= 400:
            raise RuntimeError(f"update {entity} failed: {r.status_code} {r.text[:300]}")
        return r.json()

    def ensure(self, entity: str, attribute: str, value: Any, data: dict) -> tuple[dict, bool]:
        found = self.find(entity, attribute, value)
        if found:
            return found, False
        return self.create(entity, data), True


def ensure_role(espo: Espo, name: str, perms: dict, extra: dict | None = None) -> str:
    """Find-or-create a role. `extra` holds role-level permissions (e.g.
    assignmentPermission); it and the scope table are re-applied to an existing
    role when they drift, so re-running the seed upgrades older stacks."""
    extra = extra or {}
    scopes = {e: perms for e in ENTITIES} | {"User": USER_READ}
    data = {"name": name, "data": scopes, "fieldData": {}, **extra}
    role, created = espo.ensure("Role", "name", name, data)
    if not created:
        current = espo.get("Role", role["id"])
        drift = [k for k, v in extra.items() if current.get(k) != v]
        if (current.get("data") or {}).get("User") != USER_READ:
            drift.append("data.User")
        if drift:
            espo.update("Role", role["id"], {"data": scopes, **extra})
            log.info("role %-28s updated %s", name, drift)
    log.info("role %-28s %s", name, "created" if created else "exists")
    return role["id"]


def ensure_api_user(espo: Espo, user_name: str, role_id: str) -> str:
    user, created = espo.ensure(
        "User",
        "userName",
        user_name,
        {"userName": user_name, "type": "api", "authMethod": "ApiKey", "rolesIds": [role_id]},
    )
    log.info("api user %-24s %s", user_name, "created" if created else "exists")
    api_key = espo.get("User", user["id"]).get("apiKey")
    if not api_key:
        raise RuntimeError(f"EspoCRM returned no apiKey for {user_name}")
    return api_key


def seed(data_path: Path) -> dict[str, str]:
    s = get_settings()
    spec = json.loads(data_path.read_text())
    espo = Espo(s.crm_internal_url, s.espo_admin_user, s.espo_admin_password)
    espo.wait_ready()

    user_ids: dict[str, str] = {}
    for u in spec["users"]:
        # Owners never log in during the demo; give them an unguessable password.
        password = secrets.token_urlsafe(16)
        user, created = espo.ensure(
            "User",
            "userName",
            u["userName"],
            {
                "userName": u["userName"],
                "firstName": u["firstName"],
                "lastName": u["lastName"],
                "emailAddress": u["emailAddress"],
                "type": "regular",
                "password": password,
                "passwordConfirm": password,
            },
        )
        user_ids[u["userName"]] = user["id"]
        log.info("user %-28s %s", u["userName"], "created" if created else "exists")

    op, created = espo.ensure(
        "User",
        "userName",
        s.espo_operator_user,
        {
            "userName": s.espo_operator_user,
            "firstName": "Ledger",
            "lastName": "Operator",
            "type": "admin",
            "password": s.espo_operator_password,
            "passwordConfirm": s.espo_operator_password,
        },
    )
    log.info("user %-28s %s", s.espo_operator_user, "created" if created else "exists")

    ro_role = ensure_role(espo, "Ledger verifier (read-only)", READ_ONLY)
    # The writer assigns contacts/tasks to their owners (playbook routing), which
    # EspoCRM forbids unless the role may assign to any user.
    rw_role = ensure_role(espo, "Ledger writer (fallback)", READ_WRITE, {"assignmentPermission": "all"})
    verifier_key = ensure_api_user(espo, "ledger-verifier", ro_role)
    writer_key = ensure_api_user(espo, "ledger-writer", rw_role)

    account_ids: dict[str, str] = {}
    for a in spec["accounts"]:
        data = {k: v for k, v in a.items() if k not in ("key", "assignedUser")}
        data["assignedUserId"] = user_ids[a["assignedUser"]]
        acc, created = espo.ensure("Account", "name", a["name"], data)
        account_ids[a["key"]] = acc["id"]
        log.info("account %-25s %s", a["name"], "created" if created else "exists")

    contact_ids: dict[str, str] = {}
    for c in spec["contacts"]:
        data = {k: v for k, v in c.items() if k not in ("account", "assignedUser")}
        data["accountId"] = account_ids[c["account"]]
        data["assignedUserId"] = user_ids[c["assignedUser"]]
        con, created = espo.ensure("Contact", "emailAddress", c["emailAddress"], data)
        contact_ids[c["emailAddress"]] = con["id"]
        log.info("contact %-25s %s", c["emailAddress"], "created" if created else "exists")

    for o in spec["opportunities"]:
        data = {k: v for k, v in o.items() if k not in ("account", "contacts", "assignedUser")}
        data["accountId"] = account_ids[o["account"]]
        data["assignedUserId"] = user_ids[o["assignedUser"]]
        data["contactsIds"] = [contact_ids[e] for e in o["contacts"]]
        _, created = espo.ensure("Opportunity", "name", o["name"], data)
        log.info("opportunity %-21s %s", o["name"][:21], "created" if created else "exists")

    return {
        "verifier_api_key": verifier_key,
        "writer_api_key": writer_key,
        "operator_user": s.espo_operator_user,
        "operator_user_id": op["id"],
        "user_ids": json.dumps(user_ids),
        "account_ids": json.dumps(account_ids),
        "public_url": s.crm_public_url,
        "internal_url": s.crm_internal_url,
    }


def snapshot() -> str:
    """Canonical digest of the seeded entities, to prove `make seed` is idempotent."""
    s = get_settings()
    espo = Espo(s.crm_internal_url, s.espo_admin_user, s.espo_admin_password)
    espo.wait_ready()
    state: dict[str, list] = {}
    for entity, fields in {
        "User": ("userName", "type"),
        "Role": ("name",),
        "Account": ("name", "assignedUserName"),
        "Contact": ("emailAddress", "accountName", "assignedUserName"),
        "Opportunity": ("name", "stage"),
    }.items():
        r = espo.http.get(f"/{entity}", params={"maxSize": 200, "select": ",".join(fields)})
        r.raise_for_status()
        state[entity] = sorted(tuple(row.get(f) for f in fields) for row in r.json()["list"])
    blob = json.dumps(state, sort_keys=True, default=str)
    for entity, rows in state.items():
        print(f"{entity:12} {len(rows)}")
    digest = hashlib.sha256(blob.encode()).hexdigest()[:16]
    print(f"digest       {digest}")
    return digest


def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(name)s %(message)s")
    logging.getLogger("httpx").setLevel(logging.WARNING)
    if "--snapshot" in sys.argv:
        snapshot()
        return
    s = get_settings()
    result = seed(Path(s.data_dir) / "crm_seed.json")
    r = redis.Redis.from_url(s.redis_url, decode_responses=True)
    r.hset(Keys(s.ledger_ns).crm_secrets, mapping=result)
    log.info("seed complete; CRM keys stored in Redis %s", Keys(s.ledger_ns).crm_secrets)


if __name__ == "__main__":
    sys.exit(main())
