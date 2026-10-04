"""Docker access for the API only (agent restart, kill_worker chaos, shell).

The api service is the only container with /var/run/docker.sock mounted
(docker-compose.yml); agent containers never get it. Containers are found by
their compose labels, so parallel stacks (COMPOSE_PROJECT_NAME) never touch
each other's containers. Every call degrades to an honest error when the
socket is missing.
"""

from __future__ import annotations

import os
from typing import Any

SHELL_USER = "10001"  # the non-root `agent` user baked into the ledger images


class DockerUnavailable(RuntimeError):
    pass


class ContainerNotFound(LookupError):
    pass


def client() -> Any:
    try:
        import docker
    except ImportError as exc:  # pragma: no cover - the api image installs it
        raise DockerUnavailable("docker SDK not installed") from exc
    try:
        c = docker.from_env(timeout=10)
        c.ping()
        return c
    except Exception as exc:  # noqa: BLE001 - any failure means no usable socket
        raise DockerUnavailable(f"docker socket unavailable: {exc}") from exc


def project() -> str | None:
    return os.environ.get("COMPOSE_PROJECT_NAME") or None


def find_container(service: str, *, include_stopped: bool = True) -> Any:
    """The compose container for `service` in this project.

    One-off containers (`docker compose run <service>`, e.g. the Playwright runner
    that `make test-gui` starts from the worker-browser-1 image) carry the same
    service label; they are not the agent, so they are excluded. Without this a
    kill_worker could kill the test runner instead of the operator."""
    c = client()
    labels = [f"com.docker.compose.service={service}", "com.docker.compose.oneoff=False"]
    if project():
        labels.append(f"com.docker.compose.project={project()}")
    found = c.containers.list(all=include_stopped, filters={"label": labels})
    if not found:
        raise ContainerNotFound(f"no container for service {service!r}"
                                + (f" in project {project()!r}" if project() else ""))
    return found[0]


def container_state(service: str) -> dict[str, Any]:
    ct = find_container(service)
    state = ct.attrs.get("State", {})
    return {"id": ct.short_id, "name": ct.name, "status": ct.status, "running": bool(state.get("Running")),
            "exit_code": state.get("ExitCode"), "finished_at": state.get("FinishedAt")}


def restart(service: str, timeout: int = 5) -> dict[str, Any]:
    ct = find_container(service)
    ct.restart(timeout=timeout)
    ct.reload()
    return {"id": ct.short_id, "name": ct.name, "status": ct.status}


def kill(service: str) -> dict[str, Any]:
    ct = find_container(service, include_stopped=False)
    ct.kill()
    return {"id": ct.short_id, "name": ct.name, "status": "killed"}
