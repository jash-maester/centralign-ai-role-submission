"""email.draft_valid (deterministic part) and email.sent (Mailpit)."""

from __future__ import annotations

from email.message import EmailMessage

import aiosmtplib
import pytest

from ledger_core.checks.email import find_placeholders, policy_flags, word_count
from ledger_core.mailpit import MailpitClient
from ledger_core.postconditions import CheckContext, load_all, run_check
from ledger_core.settings import get_settings


@pytest.fixture(autouse=True)
def _load_checks():
    load_all()


GOOD = {
    "to": "priya@northwind.com",
    "subject": "Good to meet you at Signal Summit, Priya",
    "body": (
        "Hi Priya,\n\nThanks for stopping by our booth at Signal Summit yesterday. "
        "You mentioned you are thinking about a Q1 pilot, and I would be glad to hear more about what your "
        "sales team needs. Would you be open to a 20-minute call next week?\n\nBest,\nAlex Chen"
    ),
}
ARGS = {"recipient": "Priya@Northwind.com", "first_name": "Priya", "event_name": "Signal Summit",
        "owner_name": "Alex Chen", "allowed_domains": ["ledger-demo.test"]}


async def check(draft, **over):
    return await run_check("email.draft_valid", {**ARGS, **over, "draft": draft}, {}, CheckContext(run_id="r"))


async def test_good_draft_passes():
    res = await check(GOOD)
    assert res.ok, res.reason
    assert res.observed["flags"] == [] and res.observed["word_count"] <= 120


async def test_draft_from_claim_when_no_draft_arg():
    res = await run_check("email.draft_valid", ARGS, {}, CheckContext(run_id="r", claim=GOOD))
    assert res.ok, res.reason


async def test_wrong_recipient_rejected():
    res = await check({**GOOD, "to": "someone@else.com"})
    assert not res.ok and "recipient someone@else.com is not the lead's email priya@northwind.com" in res.reason


async def test_missing_recipient_fact_rejected():
    res = await run_check("email.draft_valid", {"draft": GOOD}, {}, CheckContext(run_id="r"))
    assert not res.ok and "no committed recipient" in res.reason


@pytest.mark.parametrize("snippet", ["[Name]", "{{company}}", "{first_name}", "<first name>", "TODO"])
async def test_placeholders_rejected(snippet):
    res = await check({**GOOD, "body": GOOD["body"].replace("Thanks", f"Thanks {snippet}")})
    assert not res.ok and "placeholder" in res.reason and snippet in str(res.observed["placeholders"])


async def test_too_long_rejected():
    res = await check({**GOOD, "body": GOOD["body"] + " really" * 100})
    assert not res.ok and "words > 120" in res.reason


async def test_merge_fields_and_template_subject():
    res = await check({**GOOD, "subject": "Hello", "body": GOOD["body"].replace("Priya", "there").replace("Alex Chen", "Us")})
    assert not res.ok
    assert "subject 'Hello' != 'Good to meet you at Signal Summit, Priya'" in res.reason
    assert "first_name" in res.reason and "owner_name" in res.reason


@pytest.mark.parametrize("sentence,policy", [
    ("Our plans start at $99 per seat.", "pricing"),
    ("I can offer a 20% off discount.", "pricing"),
    ("We guarantee the integration will be delivered by Q1.", "promise"),
    ("I have attached our deck.", "attachment"),
    ("See https://evil.example.com/offer for details.", "link"),
])
async def test_policy_flags(sentence, policy):
    res = await check({**GOOD, "body": GOOD["body"].replace("Best,", f"{sentence}\n\nBest,")})
    assert not res.ok
    assert policy in {f["policy"] for f in res.observed["flags"]}


def test_helpers():
    assert word_count("Hi Priya, it's good.") == 4
    assert find_placeholders("Hi Priya") == []
    assert policy_flags("Visit https://www.ledger-demo.test/about", ["ledger-demo.test"]) == []
    assert policy_flags("Visit https://ledger-demo.test.evil.io", ["ledger-demo.test"])[0]["policy"] == "link"


# ---- email.sent against Mailpit ------------------------------------------


async def send(to: str, subject: str) -> None:
    s = get_settings()
    msg = EmailMessage()
    msg["From"] = "a.chen@ledger-demo.test"
    msg["To"] = to
    msg["Subject"] = subject
    msg.set_content("Hi there")
    await aiosmtplib.send(msg, hostname=s.smtp_host, port=s.smtp_port)


@pytest.fixture
async def mailpit():
    mp = MailpitClient()
    yield mp
    await mp.aclose()


@pytest.mark.crm
async def test_email_sent_found_and_fabricated(mailpit, uniq):
    to = f"Lead@{uniq}.test"
    subject = "Good to meet you at Signal Summit, Lead"
    ctx = CheckContext(run_id="r", mailpit=mailpit)
    try:
        missing = await run_check("email.sent", {"to": to}, {"subject": subject},
                                  CheckContext(run_id="r", mailpit=mailpit, claim={"message_id": "fake-123"}))
        assert not missing.ok
        assert f"Mailpit has no message to lead@{uniq}.test" in missing.reason and "fake-123" in missing.reason

        await send(to, subject)
        msgs = await mailpit.messages_to(to)
        assert len(msgs) == 1
        detail = await mailpit.message(msgs[0]["ID"])
        assert detail["Subject"] == subject

        ok = await run_check("email.sent", {"to": to}, {"subject": subject, "exactly_once": True}, ctx)
        assert ok.ok, ok.reason
        assert ok.observed["message_id"] == msgs[0]["ID"]

        claimed = await run_check("email.sent", {"to": to}, {"subject": subject},
                                  CheckContext(run_id="r", mailpit=mailpit, claim={"message_id": msgs[0]["ID"]}))
        assert claimed.ok, claimed.reason

        wrong = await run_check("email.sent", {"to": to}, {"subject": "Something else"}, ctx)
        assert not wrong.ok and "with subject 'Something else'" in wrong.reason

        await send(to, subject)
        twice = await run_check("email.sent", {"to": to}, {"subject": subject, "exactly_once": True}, ctx)
        assert not twice.ok and "expected exactly one" in twice.reason
    finally:
        await mailpit.delete_to(to.lower())


@pytest.mark.crm
async def test_email_sent_accepts_plain_httpx_client(uniq):
    import httpx

    to = f"plain@{uniq}.test"
    async with httpx.AsyncClient(base_url=get_settings().mailpit_api_url) as http:
        await send(to, "Hello")
        res = await run_check("email.sent", {"to": to, "subject": "Hello"}, {}, CheckContext(run_id="r", mailpit=http))
        assert res.ok, res.reason
        await MailpitClient(http=http).delete_to(to)
