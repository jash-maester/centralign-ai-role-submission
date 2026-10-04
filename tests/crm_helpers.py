"""Fixtures for tests against the seeded EspoCRM (Track B). Import with
`from crm_helpers import *` in a test module marked `crm`.

Records a test creates use unique emails (x@<uniq>.test) and are deleted with
the admin user afterwards (the writer role cannot delete).
"""

from __future__ import annotations

import httpx
import pytest

from ledger_core.crm_api import CrmReader, CrmWriter, normalize_email, reader_from_redis, writer_from_redis
from ledger_core.keys import Keys
from ledger_core.settings import get_settings

__all__ = ["crm_secrets", "reader", "writer", "cleanup", "Cleanup", "admin_client"]


def admin_client() -> httpx.AsyncClient:
    s = get_settings()
    return httpx.AsyncClient(base_url=f"{s.crm_internal_url}/api/v1",
                             auth=(s.espo_admin_user, s.espo_admin_password), timeout=20)


@pytest.fixture
async def crm_secrets(r):
    secrets = await r.hgetall(Keys().crm_secrets)
    if not secrets:
        pytest.fail("CRM not seeded: run `make seed` first")
    return secrets


@pytest.fixture
async def reader(r, crm_secrets) -> CrmReader:
    rd = await reader_from_redis(r, Keys())
    yield rd
    await rd.aclose()


@pytest.fixture
async def writer(r, crm_secrets) -> CrmWriter:
    wr = await writer_from_redis(r, Keys())
    yield wr
    await wr.aclose()


class Cleanup:
    def __init__(self) -> None:
        self.records: list[tuple[str, str]] = []
        self.emails: list[str] = []

    def add(self, entity: str, record_id: str) -> str:
        self.records.append((entity, record_id))
        return record_id

    def email(self, email: str) -> str:
        self.emails.append(normalize_email(email))
        return email

    async def run(self) -> None:
        async with admin_client() as a:
            for e in self.emails:
                params = {"where[0][type]": "equals", "where[0][attribute]": "emailAddress", "where[0][value]": e}
                for c in (await a.get("/Contact", params=params)).json().get("list", []):
                    self.records.append(("Contact", c["id"]))
            for entity, rid in list(self.records):
                if entity == "Contact":
                    params = {"where[0][type]": "equals", "where[0][attribute]": "parentId", "where[0][value]": rid}
                    for t in (await a.get("/Task", params=params)).json().get("list", []):
                        await a.delete(f"/Task/{t['id']}")
            for entity, rid in reversed(self.records):
                await a.delete(f"/{entity}/{rid}")


@pytest.fixture
async def cleanup():
    c = Cleanup()
    yield c
    await c.run()
