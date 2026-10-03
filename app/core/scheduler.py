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
            result = eligibility.check(session, vol, shift, tz, _exclude_assignment_id=row.id)
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


def preview_problem(session, volunteer, shift, choices, tz):
    """Validate a proposed in-memory choice, including other uncommitted choices."""
    opted_out = session.get(m.Policy, "sms_opt_out:" + volunteer.phone) if volunteer else None
    if (volunteer is None or shift is None or occupied(session, shift) or not volunteer.sms_opt_in
            or (opted_out and opted_out.value.get("value"))
            or volunteer.is_coordinator or volunteer.is_pastor
            or has_open_sensitive_escalation(session, volunteer.id)
            or not eligibility.check(session, volunteer, shift, tz)):
        return "slot or volunteer is no longer eligible"
    other = [c for c in choices if c["shift_id"] != shift.id and c["volunteer_id"] == volunteer.id]
    if load(session, volunteer.id, shift.event, tz) + len(other) >= volunteer.preferences.get("max_per_month", 3):
        return "monthly maximum reached"
    for choice in other:
        event = session.get(m.Shift, choice["shift_id"]).event
        if event.starts_at < shift.event.ends_at and event.ends_at > shift.event.starts_at:
            return "another proposed event overlaps"
    return None


def preview_draft(session, clock, month, tz, choices=None):
    """Build an exact-review plan without writing any Assignment records."""
    choices = list(choices or [])
    shifts = shifts_for(session, month, tz)
    order = {"critical": 0, "standard": 1, "optional": 2}
    shifts.sort(key=lambda s: (order.get(s.role.criticality, 2), len(candidates(session, s, clock.now(), tz)), s.event.starts_at, s.id))
    for shift in shifts:
        if occupied(session, shift) or any(c["shift_id"] == shift.id for c in choices) or shift.event.starts_at <= clock.now():
            continue
        pool = [c for c in candidates(session, shift, clock.now(), tz)
                if not preview_problem(session, c.volunteer, shift, choices, tz)]
        pool.sort(key=lambda c: (load(session, c.volunteer.id, shift.event, tz) + sum(x["volunteer_id"] == c.volunteer.id for x in choices), -c.score, c.volunteer.id))
        if pool:
            choices.append({"shift_id": shift.id, "volunteer_id": pool[0].volunteer.id})
    return choices


def preview_report(session, month, choices, tz):
    report = validate(session, month, tz)
    shift_ids = {s.id for s in shifts_for(session, month, tz)}
    problems = []
    for choice in choices:
        shift = session.get(m.Shift, choice["shift_id"])
        volunteer = session.get(m.Volunteer, choice["volunteer_id"])
        if not shift or shift.id not in shift_ids:
            problems.append({**choice, "reason": "shift is outside the planning month"})
        elif reason := preview_problem(session, volunteer, shift, choices, tz):
            problems.append({**choice, "reason": reason})
    selected = {c["shift_id"] for c in choices}
    if len(selected) != len(choices):
        problems.append({"reason": "duplicate proposed slot"})
    gaps = [s for s in report["gaps"] if s not in selected]
    return {**report, "gaps": gaps, "filled": report["total"] - len(gaps),
            "fill_percent": round(100 * (report["total"] - len(gaps)) / report["total"], 1) if report["total"] else 100,
            "violations": report["violations"] + problems,
            "proposals": [{**c, "assignment_id": -c["shift_id"]} for c in choices]}


def planning_source(shift, volunteer, month):
    return {"month": month, "shift_id": shift.id, "volunteer_id": volunteer.id,
            "event_id": shift.event_id, "role_id": shift.role_id,
            "starts_at": shift.event.starts_at.isoformat(), "ends_at": shift.event.ends_at.isoformat(),
            "event_title": shift.event.title, "role_name": shift.role.name,
            "qualifications": shift.role.required_qualifications, "fill_policy": shift.role.fill_policy}


def planning_problem(session, approval, now):
    source = approval.payload.get("workflow_plan_source", {})
    after = approval.payload.get("after", {})
    shift = session.get(m.Shift, source.get("shift_id"))
    volunteer = session.get(m.Volunteer, source.get("volunteer_id"))
    session.flush()
    if shift is not None:
        session.refresh(shift)
        session.refresh(shift.event)
        session.refresh(shift.role)
    if volunteer is not None:
        session.refresh(volunteer)
        session.expire(volunteer, ["qualifications"])
    tz = approval.payload.get("workflow_plan_timezone", "America/Denver")
    if (approval.payload.get("record") != "Assignment" or after.get("status") != "approved"
            or after.get("source") != "planner" or after.get("shift_id") != source.get("shift_id")
            or after.get("volunteer_id") != source.get("volunteer_id") or not shift or not volunteer
            or shift.event.starts_at <= now or shift.id not in {s.id for s in shifts_for(session, source["month"], tz)}
            or planning_source(shift, volunteer, source["month"]) != source):
        return "planning source changed; request a new exact proposal"
    return preview_problem(session, volunteer, shift, [], tz)
