"""Constrained scheduling. A model can propose swaps; code validates every write."""
from collections import Counter
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo
from sqlalchemy import select
from app.core import eligibility, ranking
from app.core.recurring_availability import global_frequency_limit, normalize_role_frequency_caps
from app.core.send_gate import has_open_sensitive_escalation
from app.db import models as m

ACTIVE = eligibility.ACTIVE_ASSIGNMENT_STATUSES

def bounds(month, tz="America/Denver"):
    start = datetime.strptime(month, "%Y-%m").replace(tzinfo=ZoneInfo(tz))
    return start, (start + timedelta(days=32)).replace(day=1)

def shifts_for(session, month, tz="America/Denver"):
    start, end = bounds(month, tz)
    return list(session.scalars(select(m.Shift).join(m.Event).where(
        m.Shift.starts_at >= start, m.Shift.starts_at < end,
        m.Event.status == "scheduled", ~m.Shift.coverage_children.any()).order_by(m.Shift.starts_at, m.Shift.id)))

def occupied(session, shift):
    return session.scalar(select(m.Assignment).where(m.Assignment.shift_id == shift.id,
                                                     m.Assignment.status.in_(ACTIVE)))

def candidates(session, shift, now, tz, paired_shift_ids=()):
    ranked = ranking.rank_candidates(session, shift, now, tz=tz, _paired_shift_ids=tuple(paired_shift_ids))
    return [c for c in ranked if not has_open_sensitive_escalation(session, c.volunteer.id)
            and not monthly_problem(session, c.volunteer, shift, tz)]

def load(session, volunteer_id, event, tz, role_id=None):
    start, end = ranking._month_bounds(event.starts_at, ZoneInfo(tz))
    return len(list(session.scalars(select(m.Assignment).join(m.Shift).join(m.Event).where(
        m.Assignment.volunteer_id == volunteer_id, m.Assignment.status.in_((*ACTIVE, "completed")),
        m.Shift.role_id == role_id if role_id is not None else True,
        m.Shift.starts_at >= start, m.Shift.starts_at < end))))

def monthly_problem(session, volunteer, shift, tz, choices=()):
    """Check only monthly limits; virtual choices count for their own role/month."""
    try:
        maximum = global_frequency_limit(volunteer.preferences)
        caps = normalize_role_frequency_caps(volunteer.preferences.get("role_frequency_caps", []),
                                            session.scalars(select(m.Role)).all())
    except (ValueError, TypeError):
        return "serving frequency needs a valid role mapping and limit"
    if maximum is not None and load(session, volunteer.id, shift.interval_event, tz) + len(choices) >= maximum:
        return "monthly maximum reached"
    cap = next((c for c in caps if c["role_id"] == shift.role_id), None)
    if cap:
        start, end = ranking._month_bounds(shift.starts_at, ZoneInfo(tz))
        same_role = sum(1 for choice in choices
            if (other := session.get(m.Shift, choice["shift_id"])) is not None
            and other.role_id == shift.role_id and start <= other.starts_at < end)
        if load(session, volunteer.id, shift.interval_event, tz, shift.role_id) + same_role >= cap["max_per_month"]:
            return "role-specific monthly maximum reached: " + cap["role_name"]
    return None

def propose(session, clock, shift, volunteer, tz):
    from app.core.split_coverage import pending_child
    if pending_child(session, shift):
        return {"error": "initial split bookings require atomic exact coverage review"}
    if occupied(session, shift):
        return {"error": "shift already occupied"}
    check = eligibility.check(session, volunteer, shift, tz)
    if not check or not volunteer.sms_opt_in or volunteer.is_coordinator or volunteer.is_pastor or has_open_sensitive_escalation(session, volunteer.id):
        return {"error": "ineligible", "reasons": check.reasons}
    if reason := monthly_problem(session, volunteer, shift, tz):
        return {"error": reason}
    row = m.Assignment(shift_id=shift.id, volunteer_id=volunteer.id, status="proposed",
                       source="planner", created_at=clock.now(), updated_at=clock.now())
    session.add(row); session.flush()
    return {"assignment_id": row.id}

def draft(session, clock, month, tz="America/Denver"):
    shifts = shifts_for(session, month, tz)
    order = {"critical": 0, "standard": 1, "optional": 2}
    gaps = [s for s in shifts if not occupied(session, s)]
    gaps.sort(key=lambda s: (order.get(s.role.criticality, 2), len(candidates(session, s, clock.now(), tz)), s.starts_at, s.id))
    for shift in gaps:
        pool = candidates(session, shift, clock.now(), tz)
        pool.sort(key=lambda c: (load(session, c.volunteer.id, shift.interval_event, tz), -c.score, c.volunteer.id))
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
        try:
            maximum = global_frequency_limit(session.get(m.Volunteer, vid).preferences)
        except ValueError:
            violations.append({"volunteer_id": vid, "reason": "invalid global serving frequency"})
            continue
        if maximum is not None and count > maximum:
            violations.append({"volunteer_id": vid, "reason": "monthly maximum", "count": count, "maximum": maximum})
    return {"month": month, "total": len(shifts), "filled": len(shifts) - len(gaps),
            "fill_percent": round(100 * (len(shifts)-len(gaps))/len(shifts), 1) if shifts else 100,
            "gaps": gaps, "violations": violations, "loads": dict(counts)}


def preview_problem(session, volunteer, shift, choices, tz):
    """Validate a proposed in-memory choice, including other uncommitted choices."""
    from app.core.split_coverage import pending_child
    if shift and pending_child(session, shift):
        return "initial split bookings require atomic exact coverage review"
    opted_out = session.get(m.Policy, "sms_opt_out:" + volunteer.phone) if volunteer else None
    paired_ids = tuple(c['shift_id'] for c in choices if volunteer and c['volunteer_id'] == volunteer.id)
    if (volunteer is None or shift is None or occupied(session, shift) or not volunteer.sms_opt_in
            or (opted_out and opted_out.value.get("value"))
            or volunteer.is_coordinator or volunteer.is_pastor
            or has_open_sensitive_escalation(session, volunteer.id)
            or not eligibility.check(session, volunteer, shift, tz, _paired_shift_ids=paired_ids)):
        return "slot or volunteer is no longer eligible"
    other = [c for c in choices if c["shift_id"] != shift.id and c["volunteer_id"] == volunteer.id]
    if reason := monthly_problem(session, volunteer, shift, tz, other):
        return reason
    for choice in other:
        event = session.get(m.Shift, choice["shift_id"]).interval_event
        if event.starts_at < shift.ends_at and event.ends_at > shift.starts_at:
            return "another proposed event overlaps"
    return None


def pair_options(session, volunteer, shift, month, tz, choices):
    """Return complete, valid same-date alternatives without changing records."""
    from app.core import paired_planning as pairs
    if pairs.rule_problem(session, volunteer):
        return []
    partner = pairs.partner_role(session, volunteer, shift.role_id)
    if partner is None:
        return []
    day = shift.starts_at.astimezone(ZoneInfo(tz)).date()
    options = []
    for other in shifts_for(session, month, tz):
        if (other.role_id != partner or other.starts_at.astimezone(ZoneInfo(tz)).date() != day
                or occupied(session, other) or any(c['shift_id'] == other.id for c in choices)):
            continue
        group = [{'shift_id':s.id, 'volunteer_id':volunteer.id} for s in (shift, other)]
        if all(not preview_problem(session, volunteer, s, choices+group, tz) for s in (shift, other)):
            options.append(group)
    return options


def preview_draft(session, clock, month, tz, choices=None):
    """Build an exact-review plan without writing any Assignment records."""
    choices = list(choices or [])
    shifts = shifts_for(session, month, tz)
    order = {"critical": 0, "standard": 1, "optional": 2}
    shifts.sort(key=lambda s: (order.get(s.role.criticality, 2), len(candidates(session, s, clock.now(), tz)), s.starts_at, s.id))
    for shift in shifts:
        if occupied(session, shift) or any(c["shift_id"] == shift.id for c in choices) or shift.starts_at <= clock.now():
            continue
        options = [(c, [{'shift_id':shift.id, 'volunteer_id':c.volunteer.id}])
                   for c in candidates(session, shift, clock.now(), tz)
                   if not preview_problem(session, c.volunteer, shift, choices, tz)]
        for volunteer in session.scalars(select(m.Volunteer).order_by(m.Volunteer.id)):
            for group in pair_options(session, volunteer, shift, month, tz, choices):
                if any(session.get(m.Shift,c['shift_id']).starts_at <= clock.now() for c in group):
                    continue
                candidate = next((c for c in candidates(session, shift, clock.now(), tz,
                    [g['shift_id'] for g in group]) if c.volunteer.id == volunteer.id), None)
                if candidate:
                    options.append((candidate, group))
        options.sort(key=lambda value: (load(session, value[0].volunteer.id, shift.interval_event, tz)
            + sum(x['volunteer_id'] == value[0].volunteer.id for x in choices), -value[0].score, value[0].volunteer.id))
        if options:
            choices.extend(options[0][1])
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
    from app.core import paired_planning as pairs
    held = []
    for volunteer in session.scalars(select(m.Volunteer).order_by(m.Volunteer.id)):
        if problem := pairs.rule_problem(session, volunteer):
            held.append({'volunteer_id':volunteer.id, 'reason':problem})
            continue
        for pair in pairs.rules(session, volunteer)['same_day_role_pairs']:
            relevant = [s for s in shifts_for(session, month, tz) if s.role_id in pair['role_ids']]
            recorded = {}
            for s in relevant:
                row = occupied(session, s)
                if row and row.volunteer_id == volunteer.id and row.status in ('approved','confirmed'):
                    recorded.setdefault(s.starts_at.astimezone(ZoneInfo(tz)).date(), set()).add(s.role_id)
            if (relevant and not any(set(pair['role_ids']).issubset(ids) for ids in recorded.values())
                    and not any(c['volunteer_id'] == volunteer.id and c['shift_id'] in {s.id for s in relevant} for c in choices)):
                held.append({'volunteer_id':volunteer.id, 'role_ids':pair['role_ids'],
                    'reason':'Required same-date pair has no proposed placement under current eligibility and role limits.'})
    return {**report, "gaps": gaps, "filled": report["total"] - len(gaps),
            "fill_percent": round(100 * (report["total"] - len(gaps)) / report["total"], 1) if report["total"] else 100,
            "violations": report["violations"] + problems, "held_constraints":held,
            "proposals": [{**c, "assignment_id": -c["shift_id"]} for c in choices]}


def planning_source(shift, volunteer, month):
    from app.core.paired_planning import fingerprint
    return {"month": month, "shift_id": shift.id, "volunteer_id": volunteer.id,
            "event_id": shift.event_id, "role_id": shift.role_id,
            "starts_at": shift.starts_at.isoformat(), "ends_at": shift.ends_at.isoformat(),
            "event_title": shift.event.title, "role_name": shift.role.name,
            "qualifications": shift.role.required_qualifications, "fill_policy": shift.role.fill_policy,
            "preferences_hash":fingerprint(volunteer.preferences or {}),
            "qualifications_hash":fingerprint([{'id':q.id, 'type':q.type, 'status':q.status,
                'expires_on':str(q.expires_on), 'verified_by':q.verified_by,
                'verified_at':q.verified_at.isoformat() if q.verified_at else None}
                for q in sorted(volunteer.qualifications, key=lambda q:q.id)])}


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
            or shift.starts_at <= now or shift.id not in {s.id for s in shifts_for(session, source["month"], tz)}
            or planning_source(shift, volunteer, source["month"]) != source):
        return "planning source changed; request a new exact proposal"
    return preview_problem(session, volunteer, shift, [], tz)
