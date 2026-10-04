"""GET /evidence/{path}: screenshots and other evidence files under EVIDENCE_DIR.

The path is resolved and must stay inside EVIDENCE_DIR (no `..`, no absolute
paths, no symlinks pointing out).
"""

from __future__ import annotations

from pathlib import Path

from fastapi import APIRouter, HTTPException
from fastapi.responses import FileResponse

from ledger_core.settings import get_settings

router = APIRouter(tags=["evidence"])


def safe_evidence_path(rel: str, base: str | None = None) -> Path:
    root = Path(base or get_settings().evidence_dir).resolve()
    rel = rel.removeprefix("evidence/")
    if not rel or "\x00" in rel or rel.startswith("/"):
        raise HTTPException(404, "not found")
    path = (root / rel).resolve()
    if root not in path.parents:
        raise HTTPException(404, "not found")
    if not path.is_file():
        raise HTTPException(404, "not found")
    return path


@router.get("/evidence/{path:path}")
async def get_evidence(path: str) -> FileResponse:
    return FileResponse(safe_evidence_path(path), headers={"Cache-Control": "private, max-age=3600"})
