"""Turning availability replies into dates (PLAN.md section 10.3-10.4).

The model classifies the message and extracts date-ish strings; the
interpretation into concrete available/unavailable dates is deterministic
code here. Anything we can't interpret is stored raw and treated as fully
available (eligibility only blocks on explicit statements).
"""

import calendar
import re
from datetime import date, datetime, timedelta

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.db import models as m

ORDINALS = {
    "1st": 1, "first": 1, "2nd": 2, "second": 2, "3rd": 3, "third": 3,
    "4th": 4, "fourth": 4, "5th": 5, "fifth": 5, "last": -1,
}
MONTHS = {name.lower(): i for i, name in enumerate(calendar.month_name) if name}
MONTHS.update({name.lower(): i for i, name in enumerate(calendar.month_abbr) if name})

NEGATIVE_MARKERS = ("except", "but not", "out of town", "can't", "cant", "away", "gone", "not the")


def month_sundays(month: str) -> list[date]:
    year, mon = map(int, month.split("-"))
    d = date(year, mon, 1)
    d += timedelta(days=(6 - d.weekday()) % 7)
    out = []
    while d.month == mon:
        out.append(d)
        d += timedelta(weeks=1)
    return out


def nth_sunday_dates(month: str, ns: list[int]) -> list[date]:
    sundays = month_sundays(month)
    out = []
    for n in ns:
        if n == -1:
            out.append(sundays[-1])
        elif 1 <= n <= len(sundays):
            out.append(sundays[n - 1])
    return sorted(set(out))


def _extract_day_numbers(text: str, month: str) -> list[date]:
    """'the 11th', 'oct 18', '11/22' -> dates in the target month."""
    year, mon = map(int, month.split("-"))
    days: set[int] = set()
    for match in re.finditer(r"\b(\d{1,2})(?:st|nd|rd|th)\b", text):
        days.add(int(match.group(1)))
    for match in re.finditer(r"\b([a-z]{3,9})\.?\s+(\d{1,2})\b", text):
        if MONTHS.get(match.group(1)) == mon:
            days.add(int(match.group(2)))
    for match in re.finditer(r"\b(\d{1,2})/(\d{1,2})\b", text):
        if int(match.group(1)) == mon:
            days.add(int(match.group(2)))
    out = []
    for day in days:
        try:
            out.append(date(year, mon, day))
        except ValueError:
            pass
    return sorted(out)


def _ordinal_sundays(text: str) -> list[int]:
    found = [n for word, n in ORDINALS.items() if re.search(rf"\b{word}\b", text)]
    return sorted(set(found))


def usual_pattern_dates(session: Session, volunteer_id: int, month: str, now: datetime) -> list[date]:
    """'same as usual': which nth-Sundays they served over the last 3 months."""
    since = now - timedelta(days=92)
    rows = session.execute(
        select(m.Event.starts_at)
        .join(m.Shift, m.Shift.event_id == m.Event.id)
        .join(m.Assignment, m.Assignment.shift_id == m.Shift.id)
        .where(
            m.Assignment.volunteer_id == volunteer_id,
            m.Assignment.status.in_(("approved", "confirmed", "completed")),
            m.Event.starts_at >= since,
            m.Event.starts_at < now,
        )
    ).all()
    ns = set()
    for (starts_at,) in rows:
        local = starts_at.date()
        if starts_at.weekday() == 6 or local.weekday() == 6:
            ns.add((local.day - 1) // 7 + 1)
    return nth_sunday_dates(month, sorted(ns))


def interpret_reply(
    session: Session, volunteer: m.Volunteer, text: str, month: str, now: datetime
) -> tuple[list[date], list[date]]:
    """Returns (available_dates, unavailable_dates); both empty = fully available."""
    lower = text.lower().strip()
    all_month_events = _month_event_dates(session, month)

    if "not this month" in lower or "not available this month" in lower:
        return [], all_month_events

    if "same as usual" in lower or "usual" == lower:
        return usual_pattern_dates(session, volunteer.id, month, now), []

    negative = any(marker in lower for marker in NEGATIVE_MARKERS)
    explicit = _extract_day_numbers(lower, month)
    ordinals = _ordinal_sundays(lower)

    if negative and explicit:
        return [], explicit
    if ordinals and negative:
        return [], nth_sunday_dates(month, ordinals)
    if ordinals:
        return nth_sunday_dates(month, ordinals), []
    if explicit:
        return explicit, []
    return [], []  # uninterpretable: store raw, treat as available


def _month_event_dates(session: Session, month: str) -> list[date]:
    year, mon = map(int, month.split("-"))
    rows = session.scalars(select(m.Event.starts_at)).all()
    return sorted({dt.date() for dt in rows if dt.year == year and dt.month == mon})


def record_reply(
    session: Session, volunteer: m.Volunteer, text: str, month: str, now: datetime
) -> m.Availability:
    available, unavailable = interpret_reply(session, volunteer, text, month, now)
    row = session.scalar(
        select(m.Availability).where(
            m.Availability.volunteer_id == volunteer.id, m.Availability.month == month
        )
    )
    if row is None:
        row = m.Availability(volunteer_id=volunteer.id, month=month)
        session.add(row)
    row.available_dates = [d.isoformat() for d in available]
    row.unavailable_dates = [d.isoformat() for d in unavailable]
    row.raw_reply = text
    row.parsed_at = now
    session.flush()
    return row
