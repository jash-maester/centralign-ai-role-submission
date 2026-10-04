"""Human escalations (Track J; plans/01 §9, §10).

GET  /escalations                 open escalations, oldest first (plain list of protocol.Escalation)
                                  ?run_id=  one run; ?status=open|answered|all
GET  /escalations/{id}            one escalation
POST /escalations/{id}            {answer, save_as_rule?, by?, note?}: commits the decision fact,
                                  emits input.answered, releases that lane (step -> ready on
                                  queue:review; the meta-reviewer claims the human decision and the
                                  verifier commits it). save_as_rule appends a playbook rule.
"""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel

from ledger_core import escalations
from ledger_core.keys import Keys
from ledger_core.playbook import PlaybookError
from ledger_core.protocol import Escalation

from ..deps import get_keys, get_r

router = APIRouter(tags=["escalations"])


class AnswerBody(BaseModel):
    answer: str
    save_as_rule: bool = False
    by: str = "human"
    note: str | None = None


@router.get("/escalations", response_model=list[Escalation])
async def list_open(run_id: str | None = None, status: str = "open", r=Depends(get_r),
                    keys: Keys = Depends(get_keys)) -> list[Escalation]:
    if status not in ("open", "answered", "all"):
        raise HTTPException(422, "status must be open, answered or all")
    return await escalations.list_escalations(r, keys, run_id=run_id, status=status)


@router.get("/escalations/{esc_id}", response_model=Escalation)
async def get_one(esc_id: str, r=Depends(get_r), keys: Keys = Depends(get_keys)) -> Escalation:
    esc = await escalations.get(r, keys, esc_id)
    if esc is None:
        raise HTTPException(404, f"escalation {esc_id} not found")
    return esc


@router.post("/escalations/{esc_id}")
async def answer(esc_id: str, body: AnswerBody, r=Depends(get_r), keys: Keys = Depends(get_keys)) -> dict[str, Any]:
    try:
        return await escalations.answer(r, keys, esc_id, body.answer, by=body.by or "human",
                                        save_as_rule=body.save_as_rule, note=body.note)
    except escalations.EscalationError as exc:
        msg = str(exc)
        raise HTTPException(404 if "not found" in msg else 409 if "already answered" in msg else 422, msg) from exc
    except (PlaybookError, OSError) as exc:
        raise HTTPException(500, f"could not save the playbook rule: {exc}") from exc
