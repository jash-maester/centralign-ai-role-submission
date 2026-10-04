"""e2e fixtures (Track M). The suite needs the whole stack on the scripted LLM
backend: run it with `make test-e2e` (tests/e2e/compose.e2e.yml), never with
`make test` (which deselects the `e2e` marker)."""

from __future__ import annotations

import pytest
from e2e_helpers import Api, Crm, Docker, Mailpit, reset_world


def pytest_collection_modifyitems(config, items):
    for item in items:
        if "/e2e/" in str(item.fspath):
            item.add_marker(pytest.mark.e2e)


@pytest.fixture(scope="session")
def api() -> Api:
    a = Api()
    if not a.healthy():
        pytest.fail("API not reachable at http://api:8000: run `make test-e2e` (it starts the stack)")
    return a


@pytest.fixture(scope="session")
def crm() -> Crm:
    c = Crm()
    yield c
    # leave the CRM as `make seed` made it, so `make test` (CRM lookups) is not
    # polluted by the demo contacts and review edits the scenarios wrote
    c.reset()


@pytest.fixture(scope="session")
def mail() -> Mailpit:
    return Mailpit()


@pytest.fixture(scope="session")
def docker() -> Docker:
    if not Docker.available():
        pytest.fail("no docker socket in the test container: run `make test-e2e` (tests/e2e/compose.e2e.yml)")
    return Docker()


@pytest.fixture(scope="session")
def shared() -> dict:
    """Results one scenario leaves for another (e.g. a finished run to replay)."""
    return {}


@pytest.fixture
def clean(api: Api, crm: Crm, mail: Mailpit) -> None:
    reset_world(api, crm, mail)
