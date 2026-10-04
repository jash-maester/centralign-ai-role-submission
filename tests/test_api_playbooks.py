"""Track L: GET /playbooks/{name} (agent sheet Content tab)."""

from __future__ import annotations

import pytest

from api_helpers import api_client


@pytest.fixture
def client(ns, r):
    with api_client(ns) as c:
        yield c


def test_get_playbook_sections(client):
    res = client.get("/playbooks/event-leads.md")
    assert res.status_code == 200, res.text
    pb = res.json()
    heads = [s["heading"] for s in pb["sections"]]
    assert "Owner routing" in heads and "Escalation rules" in heads
    assert pb["hash"] and pb["markdown"].startswith("---")
    assert client.get("/playbooks/event-leads").json()["hash"] == pb["hash"]


def test_get_playbook_errors(client):
    assert client.get("/playbooks/nope.md").status_code == 404
    assert client.get("/playbooks/..%2Fsecrets").status_code in (404, 422)
