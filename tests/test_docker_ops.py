"""docker_ops.find_container ignores one-off `docker compose run` containers (W4 gate).

`make test-gui` runs Playwright in a one-off container of the worker-browser-1
service; it carries the same compose service label. kill_worker must kill the
operator, never that runner.
"""

from __future__ import annotations

import pytest

pytest.importorskip("fastapi")

from app import docker_ops  # noqa: E402


class _Containers:
    def __init__(self) -> None:
        self.filters: list[dict] = []

    def list(self, all: bool, filters: dict):  # noqa: A002 - docker SDK signature
        self.filters.append(filters)
        return [object()]


class _Client:
    def __init__(self) -> None:
        self.containers = _Containers()


def test_find_container_excludes_oneoff(monkeypatch: pytest.MonkeyPatch) -> None:
    fake = _Client()
    monkeypatch.setattr(docker_ops, "client", lambda: fake)
    monkeypatch.setenv("COMPOSE_PROJECT_NAME", "ledger-x")
    docker_ops.find_container("worker-browser-1", include_stopped=False)
    labels = fake.containers.filters[0]["label"]
    assert "com.docker.compose.oneoff=False" in labels
    assert "com.docker.compose.service=worker-browser-1" in labels
    assert "com.docker.compose.project=ledger-x" in labels
