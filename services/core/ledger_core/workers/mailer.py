"""Mailer worker: skill email.send (plans/02 C9; Track K).

Sends one approved draft via SMTP (aiosmtplib) to Mailpit, verified by
`email.sent` through the Mailpit API (a different channel from SMTP).

Hard rules, enforced here and not only by the planner:
- Nothing is sent without a committed approval fact for this email
  (approval.approval_for: `approval:<lane>`, the batch `approval:emails`, or
  the approval step's committed decision) whose decision is "approve" and whose
  recipient / subject / draft step match the draft being sent. Otherwise the
  step claims `blocked` with acted=False and sends nothing.
- The draft is the committed fact (`lead:<n>.draft`), sent unchanged.
- RunConfig.dry_run: nothing is sent; the claim says what would have been.
- Check-then-act: every message carries a deterministic Message-ID derived from
  the run, lane and draft step. Before sending, the Mailpit API is searched for
  that Message-ID; if the message is already there (a previous lease holder
  sent it and died before claiming), the step claims acted=False with the
  existing message instead of sending a second copy (takeover never double-sends).
"""

from __future__ import annotations

import hashlib
import logging
from email.message import EmailMessage
from email.utils import formataddr, formatdate
from typing import Any

from .. import approval
from ..mailpit import MailpitClient
from ..protocol import Step
from ..settings import get_settings
from ..worker_base import WorkContext, WorkResult

log = logging.getLogger("ledger.mailer")

DEFAULT_FROM_DOMAIN = "ledger-demo.test"


class NotApproved(RuntimeError):
    pass


def message_id(run_id: str, lane: str | None, draft_step: str | None, to: str, subject: str) -> str:
    """Stable per (run, lane, draft): the idempotency key of the send."""
    raw = "|".join([run_id, lane or "", draft_step or "", to.lower(), subject])
    return f"ledger-{hashlib.sha256(raw.encode()).hexdigest()[:24]}@ledger.local"


def build_message(draft: dict[str, Any], msg_id: str, *, run_id: str, lane: str | None) -> EmailMessage:
    owner = draft.get("owner") or "ledger"
    from_email = draft.get("from_email") or f"{owner}@{DEFAULT_FROM_DOMAIN}"
    m = EmailMessage()
    m["From"] = formataddr((draft.get("owner_name") or owner, from_email))
    m["To"] = draft["to"]
    m["Subject"] = draft["subject"]
    m["Date"] = formatdate(localtime=False)
    m["Message-ID"] = f"<{msg_id}>"
    m["X-Ledger-Run"] = run_id
    if lane:
        m["X-Ledger-Lane"] = lane
    m.set_content(draft["body"])
    return m


def _draft_from(inputs: dict[str, Any], facts: dict[str, Any], lane: str | None) -> dict[str, Any]:
    d = inputs.get("draft")
    if not isinstance(d, dict) and lane:
        d = facts.get(approval.draft_key(lane))
    if not isinstance(d, dict):
        raise NotApproved("no committed draft fact for this email")
    return d


def check_approval(inputs: dict[str, Any], facts: dict[str, Any], lane: str | None,
                   draft: dict[str, Any]) -> dict[str, Any]:
    """The approval record, or NotApproved with the reason."""
    if not lane:
        raise NotApproved("email.send step has no lane: cannot find its approval")
    rec = approval.approval_for(facts, lane, approval_step=inputs.get("approval_step"))
    if rec is None:
        raise NotApproved(f"no committed approval fact for {lane} (approval:{lane} / {approval.BATCH_KEY})")
    if not approval.is_approved(rec):
        raise NotApproved(f"approval for {lane} is {rec.get('decision')!r}, not approve")
    why = approval.mismatch(rec, draft)
    if why:
        raise NotApproved(f"draft differs from the approved one: {why}")
    return rec


async def find_sent(mp: MailpitClient, to: str, msg_id: str) -> dict[str, Any] | None:
    for m in await mp.messages_to(to):
        if (m.get("MessageID") or "").strip("<>") == msg_id:
            return m
    return None


async def smtp_send(msg: EmailMessage, *, host: str | None = None, port: int | None = None) -> None:
    import aiosmtplib

    s = get_settings()
    await aiosmtplib.send(msg, hostname=host or s.smtp_host, port=port or s.smtp_port, timeout=20)


def make_handler(*, mailpit: Any = None, send=None):
    """`mailpit` (MailpitClient / httpx client) and `send` (async fn(EmailMessage))
    are injectable for tests; defaults: Mailpit API from settings, aiosmtplib."""
    state: dict[str, Any] = {"mp": mailpit}
    sender = send or smtp_send

    async def handle(step: Step, ctx: WorkContext) -> WorkResult:
        inputs, facts = ctx.inputs, ctx.facts
        lane = step.lane or inputs.get("lane")
        try:
            draft = _draft_from(inputs, facts, lane)
            rec = check_approval(inputs, facts, lane, draft)
        except NotApproved as exc:
            await ctx.observe({"blocked": str(exc), "retryable": False, "sent": False})
            return WorkResult(summary=f"blocked: {exc}", data={"blocked": True, "reason": str(exc), "sent": False},
                              acted=False)
        to, subject = draft["to"].strip().lower(), draft["subject"]
        msg_id = message_id(step.run_id, lane, draft.get("draft_step"), to, subject)
        base = {"lane": lane, "to": to, "subject": subject, "message_id": msg_id,
                "draft_step": draft.get("draft_step"), "approval": {k: rec.get(k) for k in (
                    "decision", "source", "decided_by", "score") if rec.get(k) is not None},
                "idempotency_key": inputs.get("idempotency_key")}
        if ctx.config.dry_run:
            await ctx.observe({"dry_run": True, "would_send": {"to": to, "subject": subject}})
            return WorkResult(summary=f"dry run: would send {subject!r} to {to}", acted=False,
                              data={**base, "dry_run": True, "action": "dry_run", "sent": False})
        if state["mp"] is None:
            state["mp"] = MailpitClient()
        mp = MailpitClient.wrap(state["mp"])
        if ctx.config.check_then_act:
            existing = await find_sent(mp, to, msg_id)
            if existing is not None:
                await ctx.observe({"already_sent": existing.get("ID"), "to": to})
                return WorkResult(summary=f"{subject!r} to {to} was already sent (Mailpit {existing.get('ID')}); "
                                          "not sending again", acted=False,
                                  data={**base, "action": "exists", "mailpit_id": existing.get("ID"), "sent": True})
        await sender(build_message(draft, msg_id, run_id=step.run_id, lane=lane))
        await ctx.observe({"sent": True, "to": to, "message_id": msg_id})
        return WorkResult(summary=f"sent {subject!r} to {to}", acted=True,
                          data={**base, "action": "sent", "sent": True})

    return handle
