"""W0: the seeded CRM matches data/crm_seed.json and the API keys are scoped."""

import json
from pathlib import Path

import httpx
import pytest

from ledger_core.keys import Keys
from ledger_core.settings import get_settings

pytestmark = pytest.mark.crm


@pytest.fixture
async def crm_secrets(r):
    secrets = await r.hgetall(Keys().crm_secrets)
    if not secrets:
        pytest.fail("CRM not seeded: run `make seed` first")
    return secrets


def _client(api_key: str) -> httpx.Client:
    return httpx.Client(base_url=f"{get_settings().crm_internal_url}/api/v1", headers={"X-Api-Key": api_key}, timeout=20)


def _find_contact(c: httpx.Client, email: str) -> list:
    params = {"where[0][type]": "equals", "where[0][attribute]": "emailAddress", "where[0][value]": email}
    resp = c.get("/Contact", params=params)
    resp.raise_for_status()
    return resp.json()["list"]


async def test_seeded_contacts_exist_exactly_once(crm_secrets, repo):
    spec = json.loads(Path(repo, "data/crm_seed.json").read_text())
    with _client(crm_secrets["verifier_api_key"]) as c:
        for contact in spec["contacts"]:
            assert len(_find_contact(c, contact["emailAddress"])) == 1, contact["emailAddress"]


async def test_benjamin_ortiz_has_an_open_deal(crm_secrets):
    with _client(crm_secrets["verifier_api_key"]) as c:
        rows = c.get("/Opportunity", params={"maxSize": 50}).json()["list"]
    deals = [o for o in rows if o["name"].startswith("Quarry Data")]
    assert deals and deals[0]["stage"] not in ("Closed Won", "Closed Lost")


async def test_two_lumen_accounts(crm_secrets):
    with _client(crm_secrets["verifier_api_key"]) as c:
        rows = c.get("/Account", params={"maxSize": 50}).json()["list"]
    assert sorted(a["name"] for a in rows if a["name"].startswith("Lumen")) == ["Lumen Health", "Lumen Inc"]


async def test_verifier_key_is_read_only(crm_secrets, uniq):
    with _client(crm_secrets["verifier_api_key"]) as c:
        resp = c.post("/Contact", json={"lastName": f"ro-{uniq}"})
    assert resp.status_code == 403


async def test_writer_key_can_create(crm_secrets, uniq):
    with _client(crm_secrets["writer_api_key"]) as c:
        resp = c.post("/Contact", json={"firstName": "Probe", "lastName": uniq, "emailAddress": f"probe@{uniq}.test"})
        assert resp.status_code == 200, resp.text
        # leave no trace: the writer role cannot delete, so the admin cleans up
    admin = httpx.Client(
        base_url=f"{get_settings().crm_internal_url}/api/v1",
        auth=(get_settings().espo_admin_user, get_settings().espo_admin_password),
    )
    admin.delete(f"/Contact/{resp.json()['id']}")
