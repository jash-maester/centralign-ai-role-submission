"""In-process agents for `cli demo` and tests (a dev harness, not a service).

`make demo` must work on a lean stack (redis + espocrm + mailpit + seed). For
every skill the run needs that no live agent serves, LocalAgents starts an
in-process stand-in built from the real pieces:

- file.parse    -> worker_base.Worker + workers.parser handler (same as worker-parser)
- api.espocrm   -> worker_base.Worker + workers.crm_api_skill (writer key; Track F
                   ships the real worker-api service)
- verifier      -> verifier.Verifier with a CheckContext that carries the read-only
                   CRM client (verifier key) and Mailpit, and the orchestrator's
                   replan-aware reject policy (Track F ships the real wiring)
- orchestrator  -> orchestrator.Orchestrator (when the service is not running)
- review        -> meta_reviewer.MetaReviewer (Track J; read-only CRM key)

browser.espocrm cannot run in this image (Playwright lives in the browser
image); a run routed to it needs worker-browser-* up.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
from typing import Any

import redis.asyncio as aioredis

from . import agents, ledger
from .keys import Keys
from .orchestrator import Orchestrator
from .orchestrator_replan import reject_policy
from .postconditions import CheckContext
from .protocol import CRM_KINDS, Skill, Step, StepKind
from .settings import get_settings
from .verifier import Verifier, default_context, verifier_card
from .worker_base import WorkContext, Worker, WorkResult, worker_card

log = logging.getLogger("ledger.local")


def api_worker(r: aioredis.Redis, keys: Keys, agent_id: str = "worker-api-local", **kw: Any) -> Worker:
    from .workers import crm_api_skill

    state: dict[str, Any] = {}

    async def handle(step: Step, ctx: WorkContext) -> WorkResult:
        if "writer" not in state:
            from .crm_api import writer_from_redis

            state["writer"] = await writer_from_redis(r)
        out = await crm_api_skill.handle(step.kind.value, ctx.inputs, writer=state["writer"],
                                         check_then_act=ctx.config.check_then_act)
        await ctx.observe({"skill": "api.espocrm", "acted": out["acted"], "summary": out["summary"]})
        return WorkResult(summary=out["summary"], data=out["data"], acted=out["acted"])

    card = worker_card(agent_id, "CRM REST worker (local)", {Skill.API_ESPOCRM.value: [k.value for k in CRM_KINDS]},
                       side_effects=True, container=agent_id,
                       tools=[{"id": "crm-rest-writer", "type": "rest", "name": "EspoCRM REST (writer key)"}])
    return Worker(r, keys, card, handle, **kw)


def parser_worker(r: aioredis.Redis, keys: Keys, agent_id: str = "worker-parser-local", data_dir: str | None = None,
                  **kw: Any) -> Worker:
    from .workers.parser import make_handler

    card = worker_card(agent_id, "Attendee file parser (local)", {Skill.FILE_PARSE.value: [StepKind.FILE_PARSE.value]},
                       container=agent_id)
    return Worker(r, keys, card, make_handler(data_dir or get_settings().data_dir), **kw)


def drafter_worker(r: aioredis.Redis, keys: Keys, agent_id: str = "worker-drafter-local", **kw: Any) -> Worker:
    """Track K: skill email.draft (LLM role worker), same handler as worker-drafter."""
    from .services.worker_drafter import build_worker

    return build_worker(r, keys, agent_id, container=agent_id, **kw)


def mailer_worker(r: aioredis.Redis, keys: Keys, agent_id: str = "worker-mailer-local", **kw: Any) -> Worker:
    """Track K: skill email.send (SMTP to Mailpit), same handler as worker-mailer."""
    from .services.worker_mailer import build_worker

    return build_worker(r, keys, agent_id, container=agent_id, **kw)


def crm_context_factory(data_dir: str | None = None):
    """Verifier context with the read-only CRM reader and a Mailpit client."""
    state: dict[str, Any] = {}

    async def factory(step: Step, r: aioredis.Redis, keys: Keys) -> CheckContext:
        if "crm" not in state:
            from .crm_api import reader_from_redis

            state["crm"] = await reader_from_redis(r)
        if "mailpit" not in state:
            try:
                from .mailpit import MailpitClient

                state["mailpit"] = MailpitClient()
            except Exception:  # noqa: BLE001
                state["mailpit"] = None
        # Track J: start from the verifier's default context (extra r/keys/config/playbook,
        # which review.decided needs) and add the world handles.
        ctx = await default_context(step, r, keys)
        ctx.crm, ctx.mailpit = state["crm"], state["mailpit"]
        ctx.data_dir = data_dir or get_settings().data_dir
        return ctx

    return factory


def local_verifier(r: aioredis.Redis, keys: Keys, agent_id: str = "verifier-local", data_dir: str | None = None,
                   **kw: Any) -> Verifier:
    return Verifier(r, keys, agent_id=agent_id, context_factory=crm_context_factory(data_dir),
                    reject_policy=reject_policy, card=verifier_card(agent_id), **kw)


async def served(r: aioredis.Redis, keys: Keys) -> dict[str, list[str]]:
    """{skill or role: [live agent ids]} from agent cards + liveness."""
    out: dict[str, list[str]] = {}
    for a in await agents.list_agents(r, keys):
        if not a["alive"]:
            continue
        card = a["card"]
        for sk in card.skills:
            out.setdefault(sk.id.value, []).append(card.id)
        if card.role in ("verifier", "orchestrator"):
            out.setdefault(card.role, []).append(card.id)
    return out


class LocalAgents:
    """Start in-process agents for whatever is missing; stop them on exit."""

    def __init__(self, r: aioredis.Redis, keys: Keys, *, need: list[str], data_dir: str | None = None,
                 force: bool = False, own_orchestrator: bool = False) -> None:
        self.r, self.keys, self.need, self.data_dir, self.force = r, keys, need, data_dir, force
        # own_orchestrator: always run an in-process orchestrator that handles only
        # the runs pinned to it (e.g. a scripted-LLM demo next to a live service)
        self.own_orchestrator = own_orchestrator
        self.started: list[str] = []
        self.orchestrator: Orchestrator | None = None
        self._tasks: list[asyncio.Task] = []
        self._stoppers: list[Any] = []

    async def __aenter__(self) -> LocalAgents:
        live = {} if self.force else await served(self.r, self.keys)
        for what in self.need:
            if live.get(what) and not (what == "orchestrator" and self.own_orchestrator):
                continue
            agent: Any
            if what == Skill.FILE_PARSE.value:
                agent = parser_worker(self.r, self.keys, data_dir=self.data_dir, block_ms=500)
            elif what == Skill.API_ESPOCRM.value:
                agent = api_worker(self.r, self.keys, block_ms=500)
            elif what == Skill.EMAIL_DRAFT.value:
                agent = drafter_worker(self.r, self.keys, block_ms=500)
            elif what == Skill.EMAIL_SEND.value:
                agent = mailer_worker(self.r, self.keys, block_ms=500)
            elif what == "verifier":
                agent = local_verifier(self.r, self.keys, data_dir=self.data_dir, block_ms=500)
            elif what == Skill.REVIEW.value:
                from .meta_reviewer import MetaReviewer

                agent = MetaReviewer(self.r, self.keys, agent_id="meta-reviewer-local", block_ms=500)
            elif what == "orchestrator":
                agent = self.orchestrator = Orchestrator(self.r, self.keys, agent_id="orchestrator-local",
                                                         sweep_interval_s=2.0, block_ms=500,
                                                         owned_only=self.own_orchestrator,
                                                         run_reaper=not live.get(what))
            else:
                log.warning("no live agent serves %s and it cannot run in-process here", what)
                continue
            self._tasks.append(asyncio.create_task(agent.run()))
            self._stoppers.append(agent.stop)
            self.started.append(what)
        await asyncio.sleep(0.2)
        return self

    async def __aexit__(self, *exc: object) -> None:
        for stop in self._stoppers:
            stop()
        for t in self._tasks:
            with contextlib.suppress(asyncio.TimeoutError, asyncio.CancelledError, Exception):
                await asyncio.wait_for(t, timeout=5)
