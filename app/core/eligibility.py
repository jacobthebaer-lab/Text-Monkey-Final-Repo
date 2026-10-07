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
    _exclude_assignment_id: int | None = None,
    _paired_shift_ids: tuple[int, ...] = (),
) -> EligibilityResult:
    """All hard rules for serving `shift`. Returns every failed rule, not just the first."""
    event = shift.interval_event
    role = shift.role
    event_date = event.starts_at.astimezone(ZoneInfo(tz)).date()
    reasons: list[str] = []

    from app.core.split_coverage import children, child_problem
    # Canonical imports also inspect an unsaved whole-slot probe. It has no
    # persisted child scope; querying children(None) would select every root.
    if shift.id is not None:
        if children(session, shift.id):
            reasons.append('partitioned parent is covered only by its reviewed child intervals')
        if problem := child_problem(session, shift):
            reasons.append(problem)

    if volunteer.status != "active":
        reasons.append(f"volunteer is {volunteer.status}")

    prefs = volunteer.preferences or {}
    if prefs.get("onboarding_stage") in ("interests", "availability"):
        reasons.append("text signup is not finished")
    if event.status in ("cancelled", "completed"):
        reasons.append("event is closed")
    has_windows = "recurring_windows" in prefs and prefs["recurring_windows"] != []
    if has_windows:
        from app.core.recurring_availability import recurring_window_reasons
        reasons.extend(recurring_window_reasons(session, prefs, shift, tz))
    else:
        days = prefs.get("availability_weekdays", [])
        local_start = event.starts_at.astimezone(ZoneInfo(tz))
        if days and local_start.weekday() not in days:
            reasons.append("outside preferred available weekdays")
        services = prefs.get("preferred_services", [])
        if services and local_start.weekday() == 6 and f"sun_{local_start.hour}" not in services:
            reasons.append("outside preferred service times")
    roles = prefs.get("interested_roles", [])
    if not has_windows and prefs.get("onboarding_stage") == "complete" and roles and role.name not in roles:
        reasons.append("outside chosen serving roles")

    from app.core.recurring_availability import role_frequency_reasons
    reasons.extend(role_frequency_reasons(session, volunteer, shift, tz, _exclude_assignment_id))
    from app.core.paired_planning import eligibility_problem
    if problem := eligibility_problem(session, volunteer, shift, tz, _paired_shift_ids):
        reasons.append(problem)
    from app.core.planning_patterns import calendar_reasons
    reasons.extend(calendar_reasons(prefs, event.starts_at, tz))

    if role.name in prefs.get("paused_roles", []):
        reasons.append("role paused by coordinator")

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
        select(m.Event.title, m.Shift.starts_at)
        .join(m.Shift, m.Shift.event_id == m.Event.id)
        .join(m.Assignment, m.Assignment.shift_id == m.Shift.id)
        .where(
            m.Assignment.volunteer_id == volunteer.id,
            m.Assignment.id != _exclude_assignment_id if _exclude_assignment_id is not None else True,
            m.Assignment.status.in_(ACTIVE_ASSIGNMENT_STATUSES),
            m.Shift.starts_at < event.ends_at,
            m.Shift.ends_at > event.starts_at,
        )
    ).all()
    for title, starts_at in overlapping:
        reasons.append(f"already booked: {title} at {starts_at.isoformat()}")

    from app.core.cancellation_refusal import problem as cancellation_problem
    if problem := cancellation_problem(session, volunteer, shift):
        reasons.append(problem)

    from app.integrations.planning_center_sync import native_availability_problem
    if problem := native_availability_problem(session, volunteer, shift):
        reasons.append(problem)

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
