"""Constrained scheduling. A model can propose swaps; code validates every write."""
from collections import Counter
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo
from sqlalchemy import select
from app.core import eligibility, ranking
from app.core.send_gate import has_open_sensitive_escalation
from app.db import models as m

ACTIVE = eligibility.ACTIVE_ASSIGNMENT_STATUSES

def bounds(month, tz="America/Denver"):
    start = datetime.strptime(month, "%Y-%m").replace(tzinfo=ZoneInfo(tz))
    return start, (start + timedelta(days=32)).replace(day=1)

def shifts_for(session, month, tz="America/Denver"):
    start, end = bounds(month, tz)
    return list(session.scalars(select(m.Shift).join(m.Event).where(
        m.Event.starts_at >= start, m.Event.starts_at < end,
        m.Event.status == "scheduled").order_by(m.Event.starts_at, m.Shift.id)))

def occupied(session, shift):
    return session.scalar(select(m.Assignment).where(m.Assignment.shift_id == shift.id,
                                                     m.Assignment.status.in_(ACTIVE)))

def candidates(session, shift, now, tz):
    ranked = ranking.rank_candidates(session, shift, now, tz=tz)
    return [c for c in ranked if not has_open_sensitive_escalation(session, c.volunteer.id)
            and load(session, c.volunteer.id, shift.event, tz) < c.volunteer.preferences.get("max_per_month", 3)]

def load(session, volunteer_id, event, tz):
    start, end = ranking._month_bounds(event.starts_at, ZoneInfo(tz))
    return len(list(session.scalars(select(m.Assignment).join(m.Shift).join(m.Event).where(
        m.Assignment.volunteer_id == volunteer_id, m.Assignment.status.in_((*ACTIVE, "completed")),
        m.Event.starts_at >= start, m.Event.starts_at < end))))

def propose(session, clock, shift, volunteer, tz):
    if occupied(session, shift):
        return {"error": "shift already occupied"}
    check = eligibility.check(session, volunteer, shift, tz)
    if not check or not volunteer.sms_opt_in or volunteer.is_coordinator or volunteer.is_pastor or has_open_sensitive_escalation(session, volunteer.id):
        return {"error": "ineligible", "reasons": check.reasons}
    if load(session, volunteer.id, shift.event, tz) >= volunteer.preferences.get("max_per_month", 3):
        return {"error": "monthly maximum reached"}
    row = m.Assignment(shift_id=shift.id, volunteer_id=volunteer.id, status="proposed",
                       source="planner", created_at=clock.now(), updated_at=clock.now())
    session.add(row); session.flush()
    return {"assignment_id": row.id}

def draft(session, clock, month, tz="America/Denver"):
    shifts = shifts_for(session, month, tz)
    order = {"critical": 0, "standard": 1, "optional": 2}
    gaps = [s for s in shifts if not occupied(session, s)]
    gaps.sort(key=lambda s: (order.get(s.role.criticality, 2), len(candidates(session, s, clock.now(), tz)), s.event.starts_at, s.id))
    for shift in gaps:
        pool = candidates(session, shift, clock.now(), tz)
        pool.sort(key=lambda c: (load(session, c.volunteer.id, shift.event, tz), -c.score, c.volunteer.id))
        if pool:
            propose(session, clock, shift, pool[0].volunteer, tz)
    return validate(session, month, tz)

def validate(session, month, tz="America/Denver"):
    shifts = shifts_for(session, month, tz)
    violations, gaps, counts = [], [], Counter()
    for shift in shifts:
        rows = list(session.scalars(select(m.Assignment).where(m.Assignment.shift_id == shift.id, m.Assignment.status.in_(ACTIVE))))
        if not rows: gaps.append(shift.id)
        if len(rows) > 1: violations.append({"shift_id": shift.id, "reason": "duplicate assignment"})
        for row in rows:
            vol = session.get(m.Volunteer, row.volunteer_id)
            # The current assignment must not conflict with itself during validation.
            original = row.status
            row.status = "validating"; session.flush()
            result = eligibility.check(session, vol, shift, tz)
            row.status = original; session.flush()
            if not result: violations.append({"assignment_id": row.id, "reasons": result.reasons})
            if not vol.sms_opt_in or has_open_sensitive_escalation(session, vol.id):
                violations.append({"assignment_id": row.id, "reason": "consent or pastoral hold"})
            counts[vol.id] += 1
    for vid, count in counts.items():
        maximum = session.get(m.Volunteer, vid).preferences.get("max_per_month", 3)
        if count > maximum: violations.append({"volunteer_id": vid, "reason": "monthly maximum", "count": count, "maximum": maximum})
    return {"month": month, "total": len(shifts), "filled": len(shifts) - len(gaps),
            "fill_percent": round(100 * (len(shifts)-len(gaps))/len(shifts), 1) if shifts else 100,
            "gaps": gaps, "violations": violations, "loads": dict(counts)}
