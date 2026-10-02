"""Monthly greedy solver + draft validator (PLAN.md section 10.5-10.6).

Pure deterministic code. Critical roles first, most-constrained shifts first,
hard rules via eligibility.check, max_per_month, fairness (fewest assignments
this month wins ties), serves_with pairs co-placed when possible. The model
never assigns anyone; it only reviews the finished draft.
"""

from dataclasses import dataclass, field
from datetime import datetime
from zoneinfo import ZoneInfo

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.core import eligibility, ranking
from app.db import models as m

CRITICALITY_ORDER = {"critical": 0, "standard": 1, "optional": 2}
COUNTED_STATUSES = ("proposed", "approved", "confirmed", "completed")


@dataclass
class SolveResult:
    month: str
    assigned: int = 0
    total_shifts: int = 0
    gaps: list = field(default_factory=list)  # [{shift_id, role, event, starts_at}]

    @property
    def fill_pct(self) -> float:
        return round(100.0 * self.assigned / self.total_shifts, 1) if self.total_shifts else 100.0


@dataclass
class ValidationResult:
    violations: list = field(default_factory=list)
    fill_pct: float = 0.0
    gaps: list = field(default_factory=list)
    fairness: dict = field(default_factory=dict)

    @property
    def ok(self) -> bool:
        return not self.violations


def _month_shifts(session: Session, month: str, tz: ZoneInfo) -> list[m.Shift]:
    year, mon = map(int, month.split("-"))
    shifts = []
    for shift in session.scalars(select(m.Shift).join(m.Event, m.Shift.event_id == m.Event.id)):
        local = shift.event.starts_at.astimezone(tz)
        if local.year == year and local.month == mon and shift.event.status == "scheduled":
            shifts.append(shift)
    return shifts


def _open_shifts(shifts: list[m.Shift]) -> list[m.Shift]:
    return [
        s for s in shifts
        if not any(a.status in eligibility.ACTIVE_ASSIGNMENT_STATUSES for a in s.assignments)
    ]


def _month_count(session: Session, volunteer_id: int, month: str, tz: ZoneInfo) -> int:
    year, mon = map(int, month.split("-"))
    count = 0
    for assignment in session.scalars(
        select(m.Assignment).where(
            m.Assignment.volunteer_id == volunteer_id, m.Assignment.status.in_(COUNTED_STATUSES)
        )
    ):
        local = assignment.shift.event.starts_at.astimezone(tz)
        if local.year == year and local.month == mon:
            count += 1
    return count


def solve_month(session: Session, month: str, now: datetime, tz: str = "America/Denver") -> SolveResult:
    zone = ZoneInfo(tz)
    shifts = _month_shifts(session, month, zone)
    open_shifts = _open_shifts(shifts)
    result = SolveResult(month=month, total_shifts=len(shifts), assigned=len(shifts) - len(open_shifts))

    volunteers = [
        v for v in session.scalars(select(m.Volunteer).order_by(m.Volunteer.id))
        if v.status == "active" and v.sms_opt_in and not v.is_coordinator and not v.is_pastor
    ]
    by_id = {v.id: v for v in volunteers}
    month_counts = {v.id: _month_count(session, v.id, month, zone) for v in volunteers}
    recent = {
        v.id: ranking.monthly_assignment_count(session, v.id, _fake_event(now), zone) for v in volunteers
    }

    def eligible_for(shift: m.Shift) -> list[m.Volunteer]:
        out = []
        for vol in volunteers:
            if month_counts[vol.id] >= vol.preferences.get("max_per_month", 3):
                continue
            if eligibility.check(session, vol, shift, tz=tz):
                out.append(vol)
        return out

    # Hardest first: critical roles, then fewest eligible candidates.
    static_counts = {s.id: len(eligible_for(s)) for s in open_shifts}
    open_shifts.sort(
        key=lambda s: (CRITICALITY_ORDER.get(s.role.criticality, 1), static_counts[s.id], s.event.starts_at, s.id)
    )

    def assign(shift: m.Shift, vol: m.Volunteer) -> None:
        row = m.Assignment(
            shift_id=shift.id, volunteer_id=vol.id, status="proposed", source="planner",
            created_at=now, updated_at=now,
        )
        session.add(row)
        session.flush()
        month_counts[vol.id] += 1
        result.assigned += 1

    assigned_shift_ids: set[int] = set()
    for shift in open_shifts:
        if shift.id in assigned_shift_ids:
            continue
        candidates = eligible_for(shift)
        if not candidates:
            result.gaps.append(_gap(shift, zone))
            continue
        tag = ranking.service_tag(shift.event, zone)
        candidates.sort(
            key=lambda v: (
                month_counts[v.id],
                0 if shift.role.name in v.preferences.get("interested_roles", []) else 1,
                0 if tag and tag in v.preferences.get("preferred_services", []) else 1,
                recent[v.id],
                v.id,
            )
        )
        chosen = candidates[0]
        assign(shift, chosen)
        assigned_shift_ids.add(shift.id)

        # serves_with: co-place the partner on another open shift of this event.
        partner_id = chosen.preferences.get("serves_with_volunteer_id")
        partner = by_id.get(partner_id)
        if partner and month_counts[partner.id] < partner.preferences.get("max_per_month", 3):
            for sibling in shift.event.shifts:
                if sibling.id in assigned_shift_ids or sibling.id == shift.id:
                    continue
                if any(a.status in eligibility.ACTIVE_ASSIGNMENT_STATUSES for a in sibling.assignments):
                    continue
                if eligibility.check(session, partner, sibling, tz=tz):
                    assign(sibling, partner)
                    assigned_shift_ids.add(sibling.id)
                    break

    # Flushed-but-unexpired relationship collections (shift.assignments) go
    # stale during the solve; refresh before the final gap count.
    session.expire_all()
    result.gaps = [
        _gap(s, zone) for s in _open_shifts(_month_shifts(session, month, zone))
    ]
    return result


def _gap(shift: m.Shift, zone: ZoneInfo) -> dict:
    return {
        "shift_id": shift.id,
        "role": shift.role.name,
        "event": shift.event.title,
        "starts_at": shift.event.starts_at.astimezone(zone).isoformat(),
    }


def _fake_event(now: datetime):
    class E:
        starts_at = now

    return E()


def validate_month(session: Session, month: str, tz: str = "America/Denver") -> ValidationResult:
    """Re-check every hard rule on the draft. The solver should never produce
    violations; this is the independent safety net."""
    session.expire_all()  # see fresh assignment rows, not cached collections
    zone = ZoneInfo(tz)
    shifts = _month_shifts(session, month, zone)
    result = ValidationResult()
    per_volunteer: dict[int, int] = {}

    for shift in shifts:
        active = [a for a in shift.assignments if a.status in eligibility.ACTIVE_ASSIGNMENT_STATUSES]
        if not active:
            result.gaps.append(_gap(shift, zone))
            continue
        if len(active) > 1:
            result.violations.append(f"shift {shift.id} has {len(active)} active assignments")
        for assignment in active:
            vol = assignment.volunteer
            per_volunteer[vol.id] = per_volunteer.get(vol.id, 0) + 1
            event_date = shift.event.starts_at.astimezone(zone).date()
            quals = {q.type: q for q in vol.qualifications}
            for required in shift.role.required_qualifications:
                q = quals.get(required)
                if q is None or q.status != "verified":
                    result.violations.append(
                        f"{vol.name} lacks verified {required} for {shift.role.name} (shift {shift.id})"
                    )
                elif q.expires_on is not None and q.expires_on < event_date:
                    result.violations.append(
                        f"{vol.name}: {required} expires {q.expires_on} before {event_date} (shift {shift.id})"
                    )
            availability = session.scalar(
                select(m.Availability).where(
                    m.Availability.volunteer_id == vol.id, m.Availability.month == month
                )
            )
            if availability:
                iso = event_date.isoformat()
                if iso in (availability.unavailable_dates or []):
                    result.violations.append(f"{vol.name} said unavailable {iso} (shift {shift.id})")
                elif availability.available_dates and iso not in availability.available_dates:
                    result.violations.append(f"{vol.name}: {iso} not in stated dates (shift {shift.id})")

    # Overlap check across the volunteer's month.
    seen: dict[int, list] = {}
    for shift in shifts:
        for assignment in shift.assignments:
            if assignment.status not in eligibility.ACTIVE_ASSIGNMENT_STATUSES:
                continue
            spans = seen.setdefault(assignment.volunteer_id, [])
            span = (shift.event.starts_at, shift.event.ends_at, shift.id)
            for other_start, other_end, other_id in spans:
                if span[0] < other_end and span[1] > other_start:
                    result.violations.append(
                        f"volunteer {assignment.volunteer_id} double-booked (shifts {other_id}, {shift.id})"
                    )
            spans.append(span)

    # Over max_per_month.
    for vid, count in per_volunteer.items():
        vol = session.get(m.Volunteer, vid)
        if count > vol.preferences.get("max_per_month", 3):
            result.violations.append(f"{vol.name} over max_per_month ({count})")

    filled = len(shifts) - len(result.gaps)
    result.fill_pct = round(100.0 * filled / len(shifts), 1) if shifts else 100.0
    counts = sorted(per_volunteer.values())
    result.fairness = {
        "volunteers_used": len(per_volunteer),
        "min_per_volunteer": counts[0] if counts else 0,
        "max_per_volunteer": counts[-1] if counts else 0,
    }
    return result
