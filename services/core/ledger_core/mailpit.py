"""Async Mailpit API client (http://mailpit:8025/api/v1). Read side of email.sent."""

from __future__ import annotations

from typing import Any

import httpx

from .settings import get_settings


class MailpitClient:
    def __init__(self, base_url: str | None = None, http: httpx.AsyncClient | None = None, timeout: float = 10.0):
        root = (base_url or get_settings().mailpit_api_url).rstrip("/")
        if http is None:
            http = httpx.AsyncClient(base_url=f"{root}/api/v1", timeout=timeout)
            self._prefix = ""
        else:
            # A caller-supplied client may be rooted at the server or at /api/v1.
            self._prefix = "" if str(http.base_url).rstrip("/").endswith("/api/v1") else "/api/v1"
        self.http = http

    @classmethod
    def wrap(cls, handle: Any) -> MailpitClient:
        """Accept a MailpitClient, an httpx.AsyncClient, or None (default from settings)."""
        if isinstance(handle, MailpitClient):
            return handle
        if isinstance(handle, httpx.AsyncClient):
            return cls(http=handle)
        return cls()

    async def aclose(self) -> None:
        await self.http.aclose()

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc: object) -> None:
        await self.aclose()

    async def _get(self, path: str, **params: Any) -> Any:
        r = await self.http.get(f"{self._prefix}{path}", params=params or None)
        r.raise_for_status()
        return r.json()

    async def messages_to(self, recipient: str, limit: int = 100) -> list[dict[str, Any]]:
        """Message summaries (ID, Subject, To, From, Created, ...) addressed to `recipient`,
        newest first. Filtered again locally on the exact address (case-insensitive)."""
        addr = recipient.strip().lower()
        data = await self._get("/search", query=f'to:"{addr}"', limit=limit)
        out = []
        for m in data.get("messages") or []:
            to = [(t.get("Address") or "").lower() for t in (m.get("To") or [])]
            if addr in to:
                out.append(m)
        return out

    async def message(self, message_id: str) -> dict[str, Any]:
        """Full message: Subject, From, To, Cc, Bcc, Text, HTML, Attachments, Date, ..."""
        return await self._get(f"/message/{message_id}")

    async def delete_to(self, recipient: str) -> None:
        """Test helper: delete every message to `recipient`."""
        r = await self.http.request("DELETE", f"{self._prefix}/search", params={"query": f'to:"{recipient}"'})
        r.raise_for_status()
