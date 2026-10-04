"""Screenshots and observations: what a browser skill saw, as evidence (C7).

Screenshots land in EVIDENCE_DIR as `<label>_<phase>.png` (phase = before,
after, recovery-N, error). Skill results reference them by the path relative
to EVIDENCE_DIR, which is what the API serves at `/evidence/{path}`.

Observations are small JSON-able dicts suitable for `step.observation` event
payloads: `{"page": "contact.list", "seen": "1 row matching x@y.test", ...}`.
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from playwright.async_api import Page

from ledger_core.protocol import now_ms

log = logging.getLogger(__name__)

_SAFE = re.compile(r"[^A-Za-z0-9._-]+")


def safe_label(label: str) -> str:
    cleaned = _SAFE.sub("-", label).strip("-.")
    return cleaned[:120] or "shot"


def observation(page: str, seen: str, **extra: Any) -> dict[str, Any]:
    obs: dict[str, Any] = {"page": page, "seen": seen, "ts": now_ms()}
    obs.update({k: v for k, v in extra.items() if v is not None})
    return obs


@dataclass
class SkillResult:
    """What one skill call did. Track I turns it into a Claim + observation events."""

    skill: str
    acted: bool = False
    record_id: str | None = None
    ok: bool = True
    reason: str = ""
    observations: list[dict[str, Any]] = field(default_factory=list)
    screenshots: list[str] = field(default_factory=list)
    data: dict[str, Any] = field(default_factory=dict)

    def observe(self, page: str, seen: str, **extra: Any) -> dict[str, Any]:
        obs = observation(page, seen, **extra)
        self.observations.append(obs)
        log.info("[%s] %s: %s", self.skill, page, seen)
        return obs

    def as_dict(self) -> dict[str, Any]:
        return {
            "skill": self.skill,
            "ok": self.ok,
            "acted": self.acted,
            "record_id": self.record_id,
            "reason": self.reason,
            "observations": list(self.observations),
            "screenshots": list(self.screenshots),
            "data": dict(self.data),
        }


class Evidence:
    """Saves screenshots under one directory; never lets a failed shot fail a skill."""

    def __init__(self, evidence_dir: str | Path) -> None:
        self.root = Path(evidence_dir)

    def path_for(self, label: str, phase: str) -> Path:
        return self.root / f"{safe_label(label)}_{safe_label(phase)}.png"

    async def shot(self, page: Page, label: str, phase: str, result: SkillResult | None = None) -> str | None:
        path = self.path_for(label, phase)
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            await page.screenshot(path=str(path), full_page=True)
        except Exception as e:  # evidence is best effort; the verifier is the source of truth
            log.warning("screenshot %s failed: %s", path, e)
            return None
        rel = str(path.relative_to(self.root))
        if result is not None:
            result.screenshots.append(rel)
        return rel
