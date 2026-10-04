---
playbook: event-leads
title: Event leads SOP
version: 3
owner: Revenue Operations
# Run-config defaults (plans/01-architecture.md §6a). Per-run changes override these.
review_auto_threshold: 0.80
approval_auto_threshold: 0.90
always_ask_human_email: false
fuzzy_match_threshold: 0.85
lease_ttl_s: 15
max_attempts: 3
replan_after_rejections: 2
spend_cap_usd: 2.00
---

# Event leads SOP

How Revenue Operations turns an event attendee export into CRM records and
follow-ups. Applies to every field event, booth and conference. The CRM of
record is EspoCRM. When this SOP and someone's habit disagree, this SOP wins.

## Dedupe rules

- Normalise before comparing: trim whitespace, lowercase email addresses,
  title-case names, phone numbers to E.164 (default region US when no country
  code is present).
- An attendee **is an existing contact** when their normalised email equals a
  CRM contact's email. Update that contact; never create a second one.
- A **probable match** is a contact with the same company and either the same
  phone number or a name similarity at or above the fuzzy-match threshold
  (e.g. "Ben" / "Benjamin"). Personal addresses (gmail.com, outlook.com,
  yahoo.com, icloud.com) are common on badge scans. Probable matches go to
  review; when confirmed, update the existing contact and add the new email as
  a secondary address.
- The same person appearing twice in one file is one lead. Keep the first row.
- Rows with no email are **phone-only**. Skip them with the reason logged,
  unless the company is on the strategic accounts list (currently empty).
- Never merge two existing CRM contacts. Never delete anything.

## Owner routing

- Existing contacts keep their current owner.
- New contacts are assigned by the attendee's region:
  - **EMEA** (United Kingdom, Germany, Netherlands, France, any EU country):
    `r.silva` (Rita Silva).
  - **Americas** (United States, Canada, Latin America): `a.chen` (Alex Chen).
- If the attendee's company already exists as an account, link the contact to
  that account; the account owner takes precedence over region.
- If a company name matches more than one account, do not guess. Send it to
  review.

## Follow-up policy

- Every new or updated contact gets one follow-up **Task**: subject
  "Follow up: <event name>", assigned to the contact's owner, due **2 business
  days** after the event, linked to the contact.
- Every contact with an email gets one follow-up email from the owner, except:
  - contacts with an **open deal** (an opportunity not Closed Won/Lost): no
    automated email; the owner follows up personally. Still create the task.
  - skipped or unresolved rows.
- Email template:
  - Subject: "Good to meet you at <event name>, <first name>"
  - Body: greeting by first name; one sentence referencing the event; one
    sentence personalised from the attendee's notes or title when available;
    an offer of a 20-minute call; signature with the owner's full name.
- Tone rules: plain, warm, specific, under 120 words. No pricing, discounts,
  promises of features or delivery dates, attachments, or links other than the
  company site. No placeholder text such as "[Name]" or "{{company}}".

## Approval policy

- Every external email requires approval before it is sent.
- The meta-reviewer may approve emails when all deterministic checks pass (right
  recipient, merge fields filled, no placeholders, under the length limit) and
  the tone judge scores every draft at or above the approval threshold, with
  no policy flags (pricing, promises, attachments).
- Set `always_ask_human_email: true` to require a human for every email.

## Escalation rules

- The meta-reviewer may decide, when its confidence is at or above the
  ambiguity threshold:
  - whether a probable match is the same person;
  - whether to skip a phone-only row;
  - which account to link when the evidence clearly points to one.
- It must escalate to a human, with the options and what it tried, when:
  - a company matches more than one account and nothing distinguishes them;
  - confidence is below the threshold;
  - any decision would merge or overwrite existing CRM data beyond adding a
    secondary email or phone.
- An open escalation only holds its own lead. Everything else continues.

## Definitions of done

- Every usable row is a CRM contact exactly once (no duplicates by email).
- Existing contacts were enriched, not duplicated.
- Each contact from the event has a follow-up task due in 2 business days,
  owned per the routing rules.
- Owners are assigned per the routing rules.
- Phone-only rows were skipped with a reason.
- Follow-up emails were sent only with an approval on record, and each one is
  confirmed delivered.
