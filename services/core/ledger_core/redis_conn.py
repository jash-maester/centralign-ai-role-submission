"""Async Redis connection factory."""

from __future__ import annotations

import redis.asyncio as aioredis

from .settings import get_settings


def connect(url: str | None = None) -> aioredis.Redis:
    return aioredis.from_url(url or get_settings().redis_url, decode_responses=True)
