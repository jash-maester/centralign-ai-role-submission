"""Track C: step kind + inputs -> skill call -> Claim (the worker-loop plug-in point)."""

from __future__ import annotations

import pytest

pytest.importorskip("playwright")
dispatch = pytest.importorskip("browser_worker.dispatch")

from browser_worker.evidence import SkillResult  # noqa: E402
from ledger_core.protocol import Claim, StepKind  # noqa: E402

pytestmark = pytest.mark.browser


class FakeOperator:
    agent_id = "worker-browser-test"

    def __init__(self) -> None:
        self.calls: list[tuple[str, tuple, dict]] = []

    def _rec(self, name):
        async def call(*args, **kw):
            self.calls.append((name, args, kw))
            return SkillResult(skill=name, acted=True, record_id="rec1")

        return call

    def __getattr__(self, name):
        return self._rec(name)


async def test_create_contact_inputs_with_aliases():
    op = FakeOperator()
    await dispatch.execute(
        op,
        "crm.create_contact",
        {"firstName": "Priya", "last_name": "Raman", "email": "p@n.test", "company": "Northwind", "owner": "a.chen"},
        label="stp_1_a1",
    )
    name, args, kw = op.calls[0]
    assert name == "create_contact" and args == ("Priya", "Raman", "p@n.test")
    assert kw["account_name"] == "Northwind" and kw["owner_user_name"] == "a.chen" and kw["label"] == "stp_1_a1"


async def test_search_routes_by_email_or_name():
    op = FakeOperator()
    await dispatch.execute(op, StepKind.CRM_SEARCH_CONTACT, {"email": "x@y.test"})
    await dispatch.execute(op, StepKind.CRM_SEARCH_CONTACT, {"name": "Ben Ortiz", "company": "Quarry Data"})
    assert [c[0] for c in op.calls] == ["search_contact", "search_by_name_company"]


async def test_missing_input_and_foreign_kind_raise():
    op = FakeOperator()
    with pytest.raises(ValueError, match="contact_id"):
        await dispatch.execute(op, StepKind.CRM_CREATE_TASK, {"subject": "x", "due_date": "2026-10-07"})
    with pytest.raises(ValueError, match="cannot execute"):
        await dispatch.execute(op, StepKind.EMAIL_SEND, {})


def test_to_claim_carries_evidence_and_acted():
    r = SkillResult(skill="crm.create_contact", acted=False, record_id="c1", screenshots=["a_before.png", "a_after.png"])
    r.data.update(label="a", existing=[{"id": "c1"}])
    claim = dispatch.to_claim(r, worker="worker-browser-1", fence=3)
    assert isinstance(claim, Claim)
    assert claim.acted is False and claim.fence == 3
    assert claim.evidence == ["a_before.png", "a_after.png"]
    assert claim.data["record_id"] == "c1" and "label" not in claim.data
    assert "already done" in claim.summary
