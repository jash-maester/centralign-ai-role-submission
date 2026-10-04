"""Track H: GET /evidence/{path} (path traversal blocked) and GET /llm/budget."""

from __future__ import annotations

import uuid
from pathlib import Path

import pytest

pytest.importorskip("fastapi")

from fastapi import HTTPException  # noqa: E402

from ledger_core.llm_openrouter import utc_day
from ledger_core.settings import get_settings

from api_helpers import api_client


@pytest.fixture
def client(ns, r):
    with api_client(ns) as c:
        yield c


def test_evidence_served_and_traversal_blocked(client, tmp_path):
    root = Path(get_settings().evidence_dir)
    sub = f"t-{uuid.uuid4().hex[:8]}"
    (root / sub).mkdir(parents=True)
    png = root / sub / "stp_1_a1_after.png"
    png.write_bytes(b"\x89PNG\r\n\x1a\nfake")
    try:
        res = client.get(f"/evidence/{sub}/stp_1_a1_after.png")
        assert res.status_code == 200 and res.content.startswith(b"\x89PNG")
        assert res.headers["content-type"] == "image/png"
        assert client.get(f"/evidence/evidence/{sub}/stp_1_a1_after.png").status_code == 200  # GUI prefix
        for bad in ("../etc/passwd", "..%2F..%2Fetc%2Fpasswd", f"{sub}/../../etc/passwd", "%2Fetc%2Fpasswd",
                    f"{sub}/missing.png", ""):
            assert client.get(f"/evidence/{bad}").status_code == 404, bad
    finally:
        png.unlink()
        (root / sub).rmdir()


def test_safe_evidence_path_rejects_escapes(tmp_path):
    from app.routes.evidence import safe_evidence_path

    (tmp_path / "ok.png").write_bytes(b"x")
    outside = tmp_path.parent / f"outside-{uuid.uuid4().hex[:6]}.png"
    outside.write_bytes(b"x")
    (tmp_path / "link.png").symlink_to(outside)
    try:
        assert safe_evidence_path("ok.png", str(tmp_path)) == (tmp_path / "ok.png").resolve()
        for bad in ("../" + outside.name, "/etc/passwd", "link.png", "a/../../x", "x\x00.png"):
            with pytest.raises(HTTPException):
                safe_evidence_path(bad, str(tmp_path))
    finally:
        outside.unlink()


BASE = "https://openrouter.test/api/v1"


@pytest.fixture
def budget(tmp_path, monkeypatch):
    """A Budget with a fake key, a respx-mocked GET /key and its own counter dir."""
    from app.routes import llm as llm_route
    from ledger_core.llm_budget import Budget
    from ledger_core.settings import Settings

    s = Settings(openrouter_api_key="sk-test-not-real", openrouter_base_url=BASE, llm_daily_request_budget=20,
                 llm_budget_reserve=3)
    b = Budget(s, cache_dir=str(tmp_path))
    monkeypatch.setattr(llm_route, "get_budget", lambda: b)
    return b


async def test_llm_budget_reads_openrouter(client, r, keys, budget):
    respx = pytest.importorskip("respx")
    with respx.mock(base_url=BASE) as mock:
        route = mock.get("/key").respond(json={"data": {
            "label": "sk-or-v1-abc...xyz", "limit_remaining": 4.5, "limit": 5, "is_free_tier": True,
            "free_model_daily_requests": {"used": 9, "limit": 50, "remaining": 41}}})
        await r.set(keys.llm_budget(utc_day()), 7)
        await r.hset(keys.llm_spend("run_x"), mapping={"usd": "0.25", "requests": "3", "requests:worker": "2"})
        res = client.get("/llm/budget", params={"run_id": "run_x"})
        body = res.json()
        assert body["source"] == "openrouter" and body["used"] == 9 and body["limit"] == 50
        assert body["remaining"] == 41 and body["usable"] == 38 and body["reserve"] == 3
        assert body["credit_remaining"] == 4.5 and body["ledger_used"] == 7
        assert body["live"]["remaining"] == 41 and body["live"]["limit"] == 50
        assert set(body["models"]) == {"orchestrator", "worker", "verifier", "meta_reviewer"}
        assert body["spent_usd"] == 0.25 and body["run"]["requests:worker"] == 2
        assert "sk-test-not-real" not in res.text and "sk-or-v1" not in res.text  # never the key or label
        budget.record()  # one live request made by this stack after the fetch
        again = client.get("/llm/budget").json()
        assert route.call_count == 1  # cached for 60 s
        assert again["remaining"] == 40 and again["used"] == 10 and again["local"]["used"] == 1
        local = client.get("/llm/budget", params={"live": "false"}).json()
        assert local["live"] is None and local["source"] == "local" and local["limit"] == 20


async def test_llm_budget_falls_back_to_the_local_counter(client, budget):
    respx = pytest.importorskip("respx")
    with respx.mock(base_url=BASE) as mock:
        mock.get("/key").respond(401)
        budget.counter.incr(n=6)
        body = client.get("/llm/budget").json()
    assert body["source"] == "local" and body["used"] == 6 and body["limit"] == 20 and body["remaining"] == 14
    assert body["live"] == {"ok": False, "status": 401}
