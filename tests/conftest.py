"""Shared fixtures. Tests run in a container against a real Redis (never a mock).

Each test gets its own key namespace, so suites from parallel worktrees or
tracks can share one Redis without colliding.
"""

from __future__ import annotations

import os
import uuid

import pytest

from ledger_core.keys import Keys
from ledger_core.redis_conn import connect

REPO = os.environ.get("REPO_DIR", "/repo")


def pytest_collection_modifyitems(config, items):
    if os.environ.get("LLM_LIVE_TESTS") != "1":
        skip = pytest.mark.skip(reason="LLM_LIVE_TESTS != 1 (protects the 50/day free budget)")
        for item in items:
            if "live_llm" in item.keywords:
                item.add_marker(skip)


@pytest.fixture
def ns() -> str:
    return f"t-{uuid.uuid4().hex[:8]}"


@pytest.fixture
def keys(ns: str) -> Keys:
    return Keys(ns)


@pytest.fixture
async def r(ns: str):
    conn = connect()
    yield conn
    async for key in conn.scan_iter(match=f"{ns}:*", count=500):
        await conn.delete(key)
    await conn.aclose()


@pytest.fixture
def uniq() -> str:
    """Unique token for CRM records created by a test (e.g. emails @<uniq>.test)."""
    return uuid.uuid4().hex[:8]


@pytest.fixture
def repo() -> str:
    return REPO
