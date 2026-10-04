"""Business-day arithmetic for the follow-up policy (playbook: due 2 business days after the event)."""

from __future__ import annotations

from datetime import date, datetime, timedelta


def as_date(value: date | datetime | str) -> date:
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, date):
        return value
    return date.fromisoformat(str(value).strip()[:10])


def add_business_days(start: date | datetime | str, days: int) -> date:
    """Move `days` business days (Mon-Fri) forward from `start`. Holidays are not modelled."""
    d = as_date(start)
    step = 1 if days >= 0 else -1
    remaining = abs(days)
    while remaining:
        d += timedelta(days=step)
        if d.weekday() < 5:
            remaining -= 1
    return d


def follow_up_due(event_date: date | datetime | str, business_days: int = 2) -> date:
    """Due date of the follow-up task: `business_days` business days after the event.

    An event on a Friday, Saturday or Sunday is due the following Tuesday.
    """
    return add_business_days(event_date, business_days)
