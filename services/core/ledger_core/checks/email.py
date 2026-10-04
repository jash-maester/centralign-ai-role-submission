"""Email postconditions (plans/01-architecture.md §8).

email.draft_valid  deterministic part only; the verifier layers the LLM tone
                   judge on top when this passes (D4). Policy flags are returned
                   in observed["flags"] for the meta-reviewer's approval rule.
    args    draft {to, subject, body} (else ctx.claim), recipient (the lead's
            committed email; or lead.email), first_name, event_name, owner_name,
            allowed_domains [..] (links allowed, e.g. our company site), max_words (120)
    expect  subject (exact), else the playbook template when first_name and
            event_name are known: "Good to meet you at <event>, <first name>"

email.sent         Mailpit (a different channel from SMTP) shows the message.
    args    to (required), subject
    expect  subject, exactly_once (bool), message_id (Mailpit ID or Message-ID)
"""

from __future__ import annotations

import re
from typing import Any

import httpx

from ..crm_api import normalize_email
from ..mailpit import MailpitClient
from ..postconditions import CheckContext, CheckResult, register

MAX_WORDS = 120

PLACEHOLDER_PATTERNS = [
    r"\[[^\]\n]{1,40}\]",  # [Name], [Company]
    r"\{\{[^}\n]*\}\}",  # {{company}}
    r"\{[A-Za-z_][\w.]*\}",  # {first_name}
    r"<[A-Za-z][A-Za-z _]{1,30}>",  # <first name>
    r"%\(?\w*\)?s\b",  # %s, %(name)s
    r"\b(?:XXX+|TBD|TODO|FIXME)\b",
    r"\blorem ipsum\b",
]

POLICY_PATTERNS: dict[str, list[str]] = {
    "pricing": [
        r"[$€£]\s?\d",
        r"\b\d+\s?(?:%|percent)\s?off\b",
        r"\b(?:price|prices|pricing|priced|cost|costs|discounts?|discounted|coupon|promo(?:tion)?|free trial)\b",
    ],
    "promise": [
        r"\b(?:guarantee[sd]?|promise[sd]?|commit(?:ted)? to deliver)\b",
        r"\bwill (?:be )?(?:deliver(?:ed)?|ship(?:ped)?|launch(?:ed)?|release[d]?|available)\b",
        r"\bby (?:q[1-4]|the end of|next (?:week|month|quarter|year))\b",
        r"\b(?:roadmap|release date|delivery date|go-live date)\b",
    ],
    "attachment": [r"\battach(?:ed|ment|ments|ing)?\b", r"\benclosed\b"],
}
URL_RE = re.compile(r"(?:https?://|www\.)[^\s<>()\"']+", re.I)


def word_count(text: str) -> int:
    return len(re.findall(r"\b[\w'’-]+\b", text or ""))


def find_placeholders(text: str) -> list[str]:
    found: list[str] = []
    for pat in PLACEHOLDER_PATTERNS:
        found += [m.group(0) for m in re.finditer(pat, text or "", re.I)]
    return found


def policy_flags(text: str, allowed_domains: list[str] | tuple[str, ...] = ()) -> list[dict[str, str]]:
    """Playbook tone rules: no pricing, discounts, promises, attachments, or
    links other than the allowed (company) site."""
    flags: list[dict[str, str]] = []
    for policy, pats in POLICY_PATTERNS.items():
        for pat in pats:
            for m in re.finditer(pat, text or "", re.I):
                flags.append({"policy": policy, "match": m.group(0)})
    allowed = [d.lower().lstrip(".") for d in allowed_domains]
    for m in URL_RE.finditer(text or ""):
        host = re.sub(r"^(?:https?://)?(?:www\.)?", "", m.group(0).lower()).split("/")[0].split(":")[0]
        if not any(host == d or host.endswith("." + d) for d in allowed):
            flags.append({"policy": "link", "match": m.group(0)})
    return flags


def template_subject(event_name: str, first_name: str) -> str:
    return f"Good to meet you at {event_name}, {first_name}"


def _norm_ws(s: str | None) -> str:
    return re.sub(r"\s+", " ", (s or "").strip())


@register("email.draft_valid")
async def draft_valid(args: dict[str, Any], expect: dict[str, Any], ctx: CheckContext) -> CheckResult:
    draft = args.get("draft") or ctx.claim or {}
    lead = args.get("lead") or {}
    to = normalize_email(draft.get("to"))
    subject = _norm_ws(draft.get("subject"))
    body = (draft.get("body") or "").strip()
    recipient = normalize_email(args.get("recipient") or lead.get("email"))
    first = args.get("first_name") or lead.get("first_name")
    event = args.get("event_name")
    owner_name = args.get("owner_name")
    max_words = int(args.get("max_words", MAX_WORDS))

    problems: list[str] = []
    if not recipient:
        problems.append("no committed recipient to compare with")
    elif to != recipient:
        problems.append(f"recipient {to or '(none)'} is not the lead's email {recipient}")
    if not subject:
        problems.append("empty subject")
    if not body:
        problems.append("empty body")

    want_subject = expect.get("subject") or (template_subject(event, first) if event and first else None)
    if want_subject and subject.lower() != _norm_ws(want_subject).lower():
        problems.append(f"subject {subject!r} != {want_subject!r}")
    missing = []
    if first and not re.search(rf"\b{re.escape(first)}\b", body):
        missing.append("first_name")
    if event and event.lower() not in (body + " " + subject).lower():
        missing.append("event_name")
    if owner_name and owner_name.lower() not in body.lower():
        missing.append("owner_name (signature)")
    if missing:
        problems.append("merge fields not filled: " + ", ".join(missing))

    placeholders = find_placeholders(subject + "\n" + body)
    if placeholders:
        problems.append(f"placeholder text {placeholders}")
    words = word_count(body)
    if words > max_words:
        problems.append(f"{words} words > {max_words}")
    flags = policy_flags(subject + "\n" + body, args.get("allowed_domains") or ())
    if flags:
        problems.append("policy flags: " + ", ".join(f"{f['policy']} ({f['match']!r})" for f in flags))

    observed = {
        "recipient": to,
        "expected_recipient": recipient,
        "subject": subject,
        "word_count": words,
        "placeholders": placeholders,
        "missing_merge_fields": missing,
        "flags": flags,
    }
    if problems:
        return CheckResult(False, "; ".join(problems), observed)
    return CheckResult(True, f"draft to {to}: {words} words, merge fields filled, no placeholders or policy flags",
                       observed)


@register("email.sent")
async def sent(args: dict[str, Any], expect: dict[str, Any], ctx: CheckContext) -> CheckResult:
    to = normalize_email(args.get("to"))
    if not to:
        return CheckResult(False, "email.sent needs args.to")
    subject = _norm_ws(expect.get("subject") or args.get("subject"))
    mp = MailpitClient.wrap(ctx.mailpit)
    try:
        msgs = await mp.messages_to(to)
    except httpx.HTTPError as e:
        return CheckResult(False, f"Mailpit query failed: {e.__class__.__name__}: {e}", {"to": to})
    finally:
        if mp is not ctx.mailpit and not isinstance(ctx.mailpit, httpx.AsyncClient):
            await mp.aclose()
    matching = [m for m in msgs if not subject or _norm_ws(m.get("Subject")).lower() == subject.lower()]
    observed: dict[str, Any] = {
        "to": to,
        "messages_to_recipient": len(msgs),
        "subjects": [m.get("Subject") for m in msgs][:10],
        "count": len(matching),
    }
    # Track K (additive): RunConfig.dry_run. The postcondition becomes "nothing
    # was sent": the claim's dry_run is trusted only when the run config says so.
    cfg = (ctx.extra or {}).get("config")
    if args.get("dry_run") or ((ctx.claim or {}).get("dry_run") and getattr(cfg, "dry_run", False)):
        mid = (ctx.claim or {}).get("message_id")
        sent = [m for m in matching if not mid or mid in (m.get("ID"), m.get("MessageID"))]
        observed["dry_run"] = True
        if sent:
            return CheckResult(False, f"dry run, yet Mailpit shows {len(sent)} message(s) to {to}", observed)
        return CheckResult(True, f"dry run: nothing sent to {to}, as configured", observed)
    if not matching:
        what = f" with subject {subject!r}" if subject else ""
        claimed = (ctx.claim or {}).get("message_id")
        tail = f"; claim said message {claimed}" if claimed else ""
        return CheckResult(False, f"Mailpit has no message to {to}{what}{tail}", observed)
    want_id = expect.get("message_id") or (ctx.claim or {}).get("message_id")
    if want_id:
        hit = [m for m in matching if want_id in (m.get("ID"), m.get("MessageID"), f"<{m.get('MessageID')}>")]
        if not hit:
            return CheckResult(False, f"claimed message {want_id} not found among messages to {to}", observed)
        matching = hit
    if expect.get("exactly_once") and len(matching) != 1:
        return CheckResult(False, f"{len(matching)} messages to {to} with that subject (expected exactly one)", observed)
    m = matching[0]
    observed.update({"message_id": m.get("ID"), "message_header_id": m.get("MessageID"), "created": m.get("Created"),
                     "subject": m.get("Subject")})
    return CheckResult(True, f"Mailpit shows {len(matching)} message(s) to {to}: {m.get('Subject')!r}", observed)
