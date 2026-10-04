"""File cache for LLM responses (plans/01-architecture.md §6b).

Key = sha256(role, model, messages, schema, temperature, seed). Files live
under LLM_CACHE_DIR (bind mount ./.cache/llm), two-level fan-out by digest.

Modes (LLM_CACHE):
- on:          read hits, write after every successful live call (default).
- off:         never read (forces live calls), still write so a forced run
               refreshes the cache for later replays.
- replay-only: read only; a miss raises LLMCacheMiss in the backend.
"""

from __future__ import annotations

import hashlib
import json
import os
import tempfile
import time
from pathlib import Path
from typing import Any

MODES = ("on", "off", "replay-only")


def digest(
    role: str,
    model: str,
    messages: list[dict[str, Any]],
    schema: dict[str, Any],
    temperature: float,
    seed: int | None,
) -> str:
    canonical = json.dumps(
        {"role": role, "model": model, "messages": messages, "schema": schema,
         "temperature": temperature, "seed": seed},
        sort_keys=True, separators=(",", ":"), ensure_ascii=False, default=str,
    )
    return hashlib.sha256(canonical.encode()).hexdigest()


class ResponseCache:
    def __init__(self, directory: str | os.PathLike[str], mode: str = "on") -> None:
        mode = (mode or "on").strip().lower()
        if mode not in MODES:
            raise ValueError(f"LLM_CACHE must be one of {MODES}, got {mode!r}")
        self.dir = Path(directory)
        self.mode = mode

    def path(self, key: str) -> Path:
        return self.dir / key[:2] / f"{key}.json"

    def read(self, key: str) -> dict[str, Any] | None:
        if self.mode == "off":
            return None
        try:
            return json.loads(self.path(key).read_text())
        except (OSError, ValueError):
            return None

    def write(self, key: str, record: dict[str, Any]) -> None:
        if self.mode == "replay-only":
            return
        target = self.path(key)
        try:
            target.parent.mkdir(parents=True, exist_ok=True)
            fd, tmp = tempfile.mkstemp(dir=target.parent, suffix=".tmp")
            with os.fdopen(fd, "w") as f:
                json.dump({**record, "key": key, "stored_at": int(time.time())}, f, sort_keys=True, indent=1)
            os.replace(tmp, target)
        except OSError:
            # A read-only or full cache volume must never fail an LLM call.
            pass
