"""Drafter worker: skill email.draft (plans/02 C8; Track K).

Writes one follow-up email per eligible lead from committed facts only (the
lead fact, the routed owner, the event) plus the playbook's Follow-up policy
(template + tone rules), with one llm.complete(role="worker") call. The prompt
is rebuilt from ledger state on every attempt (prompts.assemble), so a
verifier rejection (email.draft_valid: wrong subject, placeholder, too long,
judge flags ...) is in the history layer of the next attempt (D3).

The recipient is never the model's choice: `to` is the lead's committed email.
Nothing is sent here; the mailer sends only after an approval fact exists.

Step inputs (built by orchestrator_email):
    lead        "fact:lead:<n>" (resolved: name, first_name, email, company, title, notes, ...)
    lane        "lead:<n>"
    to          the lead's normalised email
    first_name, event_name, owner, owner_name, from_email, company_site?
Claim data: {lane, to, subject, body, owner, owner_name, from_email, first_name,
             event_name, word_count}
"""

from __future__ import annotations

import logging
from typing import Any

from pydantic import BaseModel, Field

from .. import agent_config, llm, playbook as playbook_mod
from ..checks.email import template_subject, word_count
from ..protocol import Step, StepKind
from ..worker_base import WorkContext, WorkResult

log = logging.getLogger("ledger.drafter")

PROMPT_KEYS = ("to", "first_name", "event_name", "owner_name", "company_site")
LEAD_KEYS = ("name", "first_name", "company", "title", "notes", "country")


class EmailDraft(BaseModel):
    """What the model returns. The recipient is fixed by the step, not the model."""

    subject: str = Field(description='exactly "Good to meet you at <event name>, <first name>"')
    body: str = Field(description="plain text, under 120 words, signed with the owner's full name")


def prompt_inputs(inputs: dict[str, Any]) -> dict[str, Any]:
    """Only what the draft may use: no ids, no other leads, no CRM internals."""
    lead = inputs.get("lead") if isinstance(inputs.get("lead"), dict) else {}
    out: dict[str, Any] = {k: inputs[k] for k in PROMPT_KEYS if inputs.get(k)}
    out["lead"] = {k: lead[k] for k in LEAD_KEYS if lead.get(k)}
    if inputs.get("event_name") and inputs.get("first_name"):
        out["subject_template"] = template_subject(inputs["event_name"], inputs["first_name"])
    return out


def build_messages(step: Step, ctx: WorkContext, *, playbook_dir: str | None = None,
                   instructions: str | None = None, layers: dict[str, bool] | None = None):
    from .. import prompts

    try:
        pb = playbook_mod.load(ctx.inputs.get("playbook") or playbook_mod.DEFAULT_PLAYBOOK, playbook_dir)
        sections = pb.sections_for(StepKind.EMAIL_DRAFT)
    except Exception:  # noqa: BLE001 - the step inputs still carry the template
        sections = []
    return prompts.assemble(
        "worker", step, prompt_inputs(ctx.inputs), ctx.attempts, sections, EmailDraft, layers,
        instructions=instructions, fixture_key=step.lane or ctx.inputs.get("lane"),
    )


def make_handler(*, playbook_dir: str | None = None):
    async def handle(step: Step, ctx: WorkContext) -> WorkResult:
        inputs = ctx.inputs
        lead = inputs.get("lead") if isinstance(inputs.get("lead"), dict) else {}
        to = (inputs.get("to") or lead.get("email") or "").strip().lower()
        if not to:
            await ctx.observe({"blocked": "no recipient: the lead has no email", "retryable": False})
            return WorkResult(summary="blocked: no recipient", data={"blocked": True}, acted=False)
        instructions, layers = None, None
        try:
            cfg = await agent_config.get_config(ctx.r, ctx.keys, ctx.agent_id)
            layers = agent_config.effective_layers(cfg)
            instructions = await agent_config.prompt_text(ctx.r, ctx.keys, ctx.agent_id, "worker")
        except Exception:  # noqa: BLE001 - defaults are fine
            pass
        prompt = build_messages(step, ctx, playbook_dir=playbook_dir, instructions=instructions, layers=layers)
        if ctx.rejection_reasons:
            await ctx.observe({"retry_after_rejection": ctx.rejection_reasons[-1]})
        draft = await llm.complete("worker", prompt.messages, EmailDraft, run_id=step.run_id, step_id=step.id,
                                   config=ctx.config)
        info = llm.last_call()
        model = info.model if info else None
        subject = " ".join(draft.subject.split())
        body = draft.body.strip()
        words = word_count(body)
        await ctx.observe({"drafted": to, "subject": subject, "word_count": words, "model": model,
                           "prompt_tokens": prompt.tokens})
        data = {
            "lane": step.lane or inputs.get("lane"), "to": to, "subject": subject, "body": body,
            "owner": inputs.get("owner"), "owner_name": inputs.get("owner_name"),
            "from_email": inputs.get("from_email"), "first_name": inputs.get("first_name"),
            "event_name": inputs.get("event_name"), "word_count": words,
        }
        return WorkResult(summary=f"drafted {subject!r} to {to} ({words} words)", data=data, acted=True,
                          model=model)

    return handle
