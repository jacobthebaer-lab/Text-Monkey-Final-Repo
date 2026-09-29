"""Hard eligibility rules. These live in code, never in prompts.

No one is ever assigned to a role they don't hold current, admin-verified
qualifications for. The agent's find_candidates and assign_volunteer tools
both call check(); assignment re-checks at write time.
"""

from dataclasses import dataclass, field
from zoneinfo import ZoneInfo

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.db import models as m

# Assignment statuses that occupy the volunteer.
ACTIVE_ASSIGNMENT_STATUSES = ("proposed", "approved", "confirmed")


@dataclass
class EligibilityResult:
    eligible: bool
    reasons: list[str] = field(default_factory=list)

    def __bool__(self) -> bool:
        return self.eligible


def check(
    session: Session,
    volunteer: m.Volunteer,
    shift: m.Shift,
    tz: str = "America/Denver",
) -> EligibilityResult:
    """All hard rules for serving `shift`. Returns every failed rule, not just the first."""
    event = shift.event
    role = shift.role
    event_date = event.starts_at.astimezone(ZoneInfo(tz)).date()
    reasons: list[str] = []

    if volunteer.status != "active":
        reasons.append(f"volunteer is {volunteer.status}")

    # Qualifications: verified by an admin and unexpired on the event date.
    quals = {q.type: q for q in volunteer.qualifications}
    for required in role.required_qualifications:
        q = quals.get(required)
        if q is None:
            reasons.append(f"missing qualification: {required}")
        elif q.status != "verified":
            reasons.append(f"qualification not verified: {required} ({q.status})")
        elif q.expires_on is not None and q.expires_on < event_date:
            reasons.append(f"qualification expired: {required} (expired {q.expires_on})")

    # Double-booking: any active assignment to an overlapping event.
    overlapping = session.execute(
        select(m.Event.title, m.Event.starts_at)
        .join(m.Shift, m.Shift.event_id == m.Event.id)
        .join(m.Assignment, m.Assignment.shift_id == m.Shift.id)
        .where(
            m.Assignment.volunteer_id == volunteer.id,
            m.Assignment.status.in_(ACTIVE_ASSIGNMENT_STATUSES),
            m.Event.starts_at < event.ends_at,
            m.Event.ends_at > event.starts_at,
        )
    ).all()
    for title, starts_at in overlapping:
        reasons.append(f"already booked: {title} at {starts_at.isoformat()}")

    # Stated availability for the event's month, when we have it.
    month = event.starts_at.astimezone(ZoneInfo(tz)).strftime("%Y-%m")
    availability = session.scalar(
        select(m.Availability)
        .where(m.Availability.volunteer_id == volunteer.id, m.Availability.month == month)
        .order_by(m.Availability.id.desc())
    )
    if availability is not None:
        date_iso = event_date.isoformat()
        if date_iso in (availability.unavailable_dates or []):
            reasons.append(f"said unavailable on {date_iso}")
        elif availability.available_dates and date_iso not in availability.available_dates:
            reasons.append(f"{date_iso} not in stated available dates")

    return EligibilityResult(eligible=not reasons, reasons=reasons)
