"""GET /playbooks/{name}: the playbook the agents read (Track L, additive).

Read-only; the GUI's agent sheet "Content" tab renders it. Saved escalation
rules (POST /escalations/{id} with save_as_rule) show up here because they are
appended to the same file.
"""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, HTTPException

from .runs import load_playbook

router = APIRouter(tags=["playbooks"])


@router.get("/playbooks/{name}")
async def get_playbook(name: str) -> dict[str, Any]:
    if "/" in name or "\\" in name or name.startswith("."):
        raise HTTPException(422, "playbook name must be a file name")
    pb = load_playbook(name if name.endswith(".md") else f"{name}.md")
    if pb is None:
        raise HTTPException(404, f"playbook {name!r} not found")
    return {
        "name": pb.name,
        "version": pb.version,
        "hash": pb.content_hash,
        "sections": [{"heading": s.title, "body": s.body} for s in pb.sections],
        "markdown": pb.text,
    }
