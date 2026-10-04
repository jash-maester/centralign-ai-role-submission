"""Track N acceptance on the demo dataset, against the seeded EspoCRM and Mailpit:
the scripted demo with one fixture changed, driven by the same in-process agents
`make demo` uses (orchestrator, parser, api.espocrm, drafter, verifier,
meta-reviewer, mailer). Needs `make up && make seed` (redis, espocrm, mailpit).

1. 1 of 8 drafts fails the approval policy (batch judge 0.70 for Dana Okafor,
   lead:4): the other 7 are auto-approved and sent at once, ONE batch judge call;
   only lead:4 escalates, on its own review.approval step; approving it sends #8.
2. A draft that goes dead (Omar Haddad, lead:10: the drafter leaves a placeholder on
   every attempt) does not fail the run: lead:10 is handed to a human with
   "send manually / skip", the run ends completed_pending_input, the report
   shows the lane with its reason, and the other 7 emails go out.
"""

from __future__ import annotations

import asyncio
import json
import shutil

import pytest
from crm_helpers import crm_secrets  # noqa: F401 - fixture
from test_email_e2e import BEN, RECIPIENTS, clean_world  # noqa: F401 - fixture

from ledger_core import escalations, ledger, llm, report
from ledger_core.llm_scripted import ScriptedBackend
from ledger_core.mailpit import MailpitClient
from ledger_core.orchestrator import submit_goal
from ledger_core.orchestrator_local import LocalAgents
from ledger_core.protocol import EventType, RunStatus, StepKind, StepStatus

pytestmark = pytest.mark.crm

S, K = StepStatus, StepKind
GOAL = "Add the leads from yesterday's event to the CRM and set up follow-ups."
NEED = ["orchestrator", "verifier", "file.parse", "api.espocrm", "review", "email.draft", "email.send"]


def fixtures(repo: str, tmp_path, *, batch_scores: dict[str, float] | None = None,
             bad_draft: str | None = None) -> str:
    """The demo fixtures with the batch judge's scores changed and/or the drafter
    leaving a placeholder in lane `bad_draft` on every attempt (email.draft_valid
    rejects it deterministically, so the draft goes dead after max_attempts)."""
    out = tmp_path / "llm"
    shutil.copytree(f"{repo}/tests/fixtures/llm", out)
    judge = json.loads((out / "judge.json").read_text())
    for f in judge:
        if f["schema"] == "BatchJudgeVerdict":
            for it in f["response"]["items"]:
                if it["id"] in (batch_scores or {}):
                    it["score"] = batch_scores[it["id"]]
                    it["reasoning"] = "Promises a follow-up demo date; too pushy for the playbook's tone rules."
    (out / "judge.json").write_text(json.dumps(judge, indent=1))
    if bad_draft:
        drafter = json.loads((out / "drafter.json").read_text())
        for f in drafter:
            if f.get("fixture_key") == bad_draft:
                f["response"]["body"] = f["response"]["body"].replace("Alex Chen", "[Your Name]")
        (out / "drafter.json").write_text(json.dumps(drafter, indent=1))
    return str(out)


@pytest.fixture(autouse=True)
def repo_dirs(repo, monkeypatch):
    """As `cli demo` does in the test image: data and playbooks live under /repo."""
    from ledger_core.settings import get_settings

    monkeypatch.setenv("DATA_DIR", f"{repo}/data")
    monkeypatch.setenv("PLAYBOOK_DIR", f"{repo}/playbooks")
    get_settings.cache_clear()
    yield
    get_settings.cache_clear()


async def _until(pred, timeout: float = 240.0, interval: float = 1.0):
    loop = asyncio.get_running_loop()
    deadline = loop.time() + timeout
    while True:
        got = await pred()
        if got:
            return got
        if loop.time() > deadline:
            raise TimeoutError("condition not reached")
        await asyncio.sleep(interval)


class Demo:
    def __init__(self, r, keys, fixtures_dir: str) -> None:
        self.r, self.keys, self.dir = r, keys, fixtures_dir
        self.backend = ScriptedBackend(fixtures_dir, r=r, keys=keys)
        self.calls: list[tuple[str, str]] = []
        inner = self.backend.complete

        async def counting(role, messages, schema, **kw):
            self.calls.append((role, schema.__name__))
            return await inner(role, messages, schema, **kw)

        self.backend.complete = counting  # type: ignore[method-assign]

    async def __aenter__(self):
        self.prev = llm.get_backend()
        llm.set_backend(self.backend)
        self.local = LocalAgents(self.r, self.keys, need=NEED, force=True, own_orchestrator=True)
        await self.local.__aenter__()
        self.run = await submit_goal(self.r, self.keys, GOAL, input_file="event_attendees.csv",
                                     config={"crm_write_path": "api"}, actor="test",
                                     orchestrator=self.local.orchestrator.agent_id)
        return self

    async def __aexit__(self, *exc):
        await self.local.__aexit__(*exc)
        llm.set_backend(self.prev)

    async def steps(self):
        return await ledger.list_steps(self.r, self.keys, self.run.id)

    async def sent(self, mp: MailpitClient) -> dict[str, int]:
        return {lane: len(await mp.messages_to(to)) for lane, to in RECIPIENTS.items()}

    async def open_escalations(self):
        return await escalations.list_escalations(self.r, self.keys, run_id=self.run.id)

    async def state(self) -> str:
        """For a timeout message: run status, escalations, each lane's step statuses."""
        run = await ledger.get_run(self.r, self.keys, self.run.id)
        lanes: dict[str, list[str]] = {}
        for s in await self.steps():
            lanes.setdefault(s.lane or "-", []).append(f"{s.kind.value}:{s.status.value}")
        esc = [e.lane for e in await self.open_escalations()]
        return f"run {run.status.value}; escalations {esc}; " + "; ".join(f"{k} {v}" for k, v in lanes.items())

    async def until(self, pred, timeout: float = 240.0):
        try:
            return await _until(pred, timeout)
        except TimeoutError as exc:
            raise AssertionError(await self.state()) from exc


async def test_one_of_eight_fails_approval_and_only_that_lane_escalates(r, keys, repo, tmp_path, clean_world):
    mp = MailpitClient()
    try:
        async with Demo(r, keys, fixtures(repo, tmp_path, batch_scores={"lead:4": 0.70})) as demo:
            async def settled():
                esc = await demo.open_escalations()
                sends = [s for s in await demo.steps() if s.kind == K.EMAIL_SEND and s.status == S.COMMITTED]
                run = await ledger.get_run(r, keys, demo.run.id)
                ok = len(esc) == 2 and len(sends) == 7 and run.status == RunStatus.COMPLETED_PENDING_INPUT
                return esc if ok else None

            esc = await demo.until(settled)
            by_lane = {e.lane: e for e in esc}
            assert sorted(by_lane) == ["lead:4", "lead:9"]  # Dana's email + Sam Ito, nothing else
            steps = await demo.steps()
            batches = [s for s in steps if s.kind == K.REVIEW_APPROVAL and s.lane is None]
            singles = [s for s in steps if s.kind == K.REVIEW_APPROVAL and s.lane]
            assert len(batches) == 1 and batches[0].status == S.COMMITTED
            assert [(s.lane, s.status) for s in singles] == [("lead:4", S.INPUT_REQUIRED)]
            assert by_lane["lead:4"].step_id == singles[0].id and "0.70" in by_lane["lead:4"].question
            assert demo.calls.count(("verifier", "BatchJudgeVerdict")) == 1  # the judge stayed ONE batch call
            sent = await demo.sent(mp)
            assert sent == {lane: (0 if lane == "lead:4" else 1) for lane in RECIPIENTS}, sent
            run = await ledger.get_run(r, keys, demo.run.id)
            assert run.status == RunStatus.COMPLETED_PENDING_INPUT

            # approving Dana's email sends the 8th, through its own approval step
            await escalations.answer(r, keys, by_lane["lead:4"].id, "approve", by="tester")
            await _until(lambda: _sent_to(mp, RECIPIENTS["lead:4"]), timeout=120)
            send4 = next(s for s in await demo.steps() if s.kind == K.EMAIL_SEND and s.lane == "lead:4")
            assert singles[0].id in send4.depends_on
        assert sum((await demo.sent(mp)).values()) == 8
        for to in BEN:
            assert await mp.messages_to(to) == []
    finally:
        await mp.aclose()


async def _sent_to(mp: MailpitClient, to: str) -> bool:
    return bool(await mp.messages_to(to))


async def test_dead_draft_is_handed_to_a_human_and_the_run_does_not_fail(r, keys, repo, tmp_path, clean_world):
    mp = MailpitClient()
    try:
        async with Demo(r, keys, fixtures(repo, tmp_path, bad_draft="lead:10")) as demo:
            async def settled():
                esc = await demo.open_escalations()
                sends = [s for s in await demo.steps() if s.kind == K.EMAIL_SEND and s.status == S.COMMITTED]
                run = await ledger.get_run(r, keys, demo.run.id)
                ok = len(esc) == 2 and len(sends) == 7 and run.status == RunStatus.COMPLETED_PENDING_INPUT
                return esc if ok else None

            esc = await demo.until(settled)
            by_lane = {e.lane: e for e in esc}
            assert sorted(by_lane) == ["lead:10", "lead:9"]
            hand = by_lane["lead:10"]
            assert [o.value for o in hand.options] == ["manual", "skip"]
            assert sum(t.startswith("attempt ") for t in hand.tried) == 3 and "placeholder" in hand.question.lower()
            steps = await demo.steps()
            draft10 = next(s for s in steps if s.kind == K.EMAIL_DRAFT and s.lane == "lead:10")
            assert draft10.status == S.DEAD and len(draft10.history) == 3
            run = await ledger.get_run(r, keys, demo.run.id)
            assert run.status == RunStatus.COMPLETED_PENDING_INPUT  # not failed
            assert not [e for e in await ledger.run_events(r, keys, run.id) if e.type == EventType.RUN_FAILED]
            sent = await demo.sent(mp)
            assert sent == {lane: (0 if lane == "lead:10" else 1) for lane in RECIPIENTS}, sent
            rep = await report.build_report(r, keys, run.id)
            omar = next(x for x in rep["leads"] if x["n"] == 10)
            assert omar["outcome"] == "created" and "placeholder" in omar["outcome_detail"].lower()
            assert omar["email_status"].startswith("waiting on you")
            assert rep["decision_counts"]["open"] == 2 and "still open" in rep["summary"]
            print("\nREPORT SUMMARY:", rep["summary"], "\nOMAR:", omar["outcome_detail"])
    finally:
        await mp.aclose()
