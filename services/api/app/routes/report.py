"""GET /runs/{id}/report: the evidence report (JSON, or markdown with ?format=md).

Built by ledger_core.report from steps, facts and events only (G1-G3).
"""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Depends, Query, Request
from fastapi.responses import PlainTextResponse

from ledger_core import report as report_mod
from ledger_core.keys import Keys

from ..deps import get_keys, get_r, must_run

router = APIRouter(tags=["report"])


@router.get("/runs/{run_id}/report", response_model=None)
async def get_report(run_id: str, request: Request, format: str | None = Query(None, pattern="^(json|md)$"),
                     r=Depends(get_r), keys: Keys = Depends(get_keys)) -> dict[str, Any] | PlainTextResponse:
    await must_run(r, keys, run_id)
    rep = await report_mod.build_report(r, keys, run_id)
    wants_md = format == "md" or (format is None and "text/markdown" in request.headers.get("accept", ""))
    if wants_md:
        return PlainTextResponse(rep["markdown"], media_type="text/markdown; charset=utf-8", headers={
            "Content-Disposition": f'inline; filename="{run_id}-evidence.md"'})
    return rep
