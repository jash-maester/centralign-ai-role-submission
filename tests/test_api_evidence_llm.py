"""Track H: GET /evidence/{path} (path traversal blocked) and GET /llm/budget."""

from __future__ import annotations

import uuid
from pathlib import Path

import pytest
from fastapi import HTTPException

from api_helpers import api_client
from ledger_core.llm_openrouter import utc_day
from ledger_core.settings import get_settings


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


async def test_llm_budget(client, r, keys, monkeypatch):
    from app.routes import llm as llm_route

    calls = []

    async def fake_key_info():
        calls.append(1)
        return {"ok": True, "remaining": 41, "limit": 50}

    monkeypatch.setattr(llm_route, "fetch_key_info", fake_key_info)
    monkeypatch.setitem(llm_route._live_cache, "at", 0.0)
    await r.set(keys.llm_budget(utc_day()), 7)
    await r.hset(keys.llm_spend("run_x"), mapping={"usd": "0.25", "requests": "3", "requests:worker": "2"})
    body = client.get("/llm/budget", params={"run_id": "run_x"}).json()
    limit = get_settings().llm_daily_request_budget
    assert body["used"] == 7 and body["limit"] == limit and body["remaining"] == limit - 7
    assert body["live"] == {"ok": True, "remaining": 41, "limit": 50}
    assert set(body["models"]) == {"orchestrator", "worker", "verifier", "meta_reviewer"}
    assert body["spent_usd"] == 0.25 and body["run"]["requests:worker"] == 2
    client.get("/llm/budget")
    assert len(calls) == 1  # live key check cached for 60 s
    assert client.get("/llm/budget", params={"live": "false"}).json()["live"] is None
