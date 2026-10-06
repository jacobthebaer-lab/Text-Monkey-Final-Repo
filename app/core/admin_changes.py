"""Bounded coordinator proposals using the existing exact record review.

The model chooses an action and existing IDs. Code builds every before/after
and binds its dependencies so review cannot authorize a changed source.
"""
from datetime import date, datetime, timedelta

from sqlalchemy import select

from app.core import confirmations
from app.core.policies import PolicyStore
from app.db import models as m


ACTIONS = {
    "add_slots": {"event_id", "role_id", "count"},
    "pause_role": {"volunteer_id", "role_id"},
    "mark_unavailable": {"volunteer_id", "dates"},
    "create_event_type": {"name"},
    "create_event": {"title", "event_type_id", "starts_at", "ends_at"},
    "update_event": {"event_id", "title", "event_type_id", "starts_at", "ends_at"},
    "set_recipe": {"event_type_id", "role_id", "count"},
}


def _row(session, label, identity, *, lock=False):
    if type(identity) is not int:
        raise ValueError(f"An existing {label} ID is required")
    cls = getattr(m, label)
    query = select(cls).where(cls.id == identity).execution_options(populate_existing=True)
    row = session.scalar(query.with_for_update() if lock else query)
    if row is None:
        raise ValueError(f"Unknown {label} ID")
    return row


def _text(value, maximum):
    if not isinstance(value, str) or not 0 < len(value.strip()) <= maximum:
        raise ValueError(f"Text must contain 1-{maximum} characters")
    return value.strip()


def _moment(value):
    if not isinstance(value, str):
        raise ValueError("An ISO date/time with an explicit UTC offset is required")
    try:
        result = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        raise ValueError("An ISO date/time with an explicit UTC offset is required") from None
    if result.tzinfo is None:
        raise ValueError("An explicit UTC offset is required")
    return result


def _future(start, end, now):
    start, end = _moment(start), _moment(end)
    if not now < start < end or end - start > timedelta(days=1):
        raise ValueError("Use a future event with a positive duration of at most 24 hours")


def source_problem(session, payload, now, *, lock=False):
    source = payload.get("admin_change_source")
    if not source:
        return None
    try:
        coordinator = _row(session, "Volunteer", source["requested_by"], lock=lock)
        if not coordinator.is_coordinator or coordinator.status != "active":
            return "Coordinator access changed; prepare a new review"
        if source["timezone"] != str(PolicyStore(session).church_tz()):
            return "Church timezone changed; prepare a new review"
        for dep in source["records"]:
            if confirmations.values(_row(session, dep["record"], dep["id"], lock=lock)) != dep["values"]:
                return "Coordinator proposal source changed; prepare a new review"
        after, label = payload["after"], payload["record"]
        if label == "Event":
            _future(after["starts_at"], after["ends_at"], now)
            if after.get("event_type_id") is not None:
                _row(session, "EventType", after["event_type_id"])
            duplicate = session.scalar(select(m.Event.id).where(m.Event.title == after["title"],
                m.Event.starts_at == _moment(after["starts_at"]), m.Event.id != (payload["record_id"] or -1)))
            if duplicate:
                return "This event already exists; review its existing ID"
        elif label == "Shift":
            event = _row(session, "Event", after["event_id"])
            _row(session, "Role", after["role_id"])
            if event.status != "scheduled" or event.starts_at <= now:
                return "Event is no longer scheduled in the future"
            if session.scalar(select(m.Shift.id).where(m.Shift.event_id == event.id,
                    m.Shift.role_id == after["role_id"], m.Shift.slot_index == after["slot_index"])):
                return "This role slot already exists; prepare a new review"
        elif label == "RoleRecipe":
            _row(session, "EventType", after["event_type_id"])
            _row(session, "Role", after["role_id"])
            rows = list(session.scalars(select(m.RoleRecipe.id).where(
                m.RoleRecipe.event_type_id == after["event_type_id"], m.RoleRecipe.role_id == after["role_id"])))
            if rows != ([payload["record_id"]] if payload["record_id"] else []):
                return "Recipe changed or is ambiguous; prepare a new review"
        elif label == "EventType":
            if any(t.name.casefold() == after["name"].casefold() for t in session.scalars(select(m.EventType))):
                return "This event type already exists; use its existing ID"
        elif label == "Availability":
            if any(date.fromisoformat(d) < now.astimezone(PolicyStore(session).church_tz()).date()
                    for d in source["dates"]):
                return "Unavailable dates are now in the past; prepare a new review"
            rows = list(session.scalars(select(m.Availability.id).where(
                m.Availability.volunteer_id == after["volunteer_id"], m.Availability.month == after["month"])))
            if rows != source["availability_ids"]:
                return "Availability source changed; prepare a new review"
    except (KeyError, TypeError, ValueError):
        return "Coordinator proposal source is missing or invalid; prepare a new review"
    return None


def propose(ctx, coordinator, args):
    if not coordinator.is_coordinator or coordinator.status != "active":
        raise ValueError("Active coordinator identity required")
    action = args.get("action")
    if action not in ACTIONS or set(args) - (ACTIONS[action] | {"action"}):
        raise ValueError("Unsupported coordinator action or fields; qualifications require human evidence review")
    session, now = ctx.session, ctx.clock.now()
    dependencies, records = [], []

    def dependency(label, identity):
        row = _row(session, label, identity)
        dependencies.append({"record": label, "id": row.id, "values": confirmations.values(row)})
        return row

    def change(label, after, row=None, **extra):
        records.append({"action": "record_change", "record": label,
            "record_id": row.id if row else None, "before": confirmations.values(row) if row else None,
            "after": after, "reason": f"Coordinator {coordinator.id} proposes {action}; exact review required",
            "transport": "mock_or_twilio", "admin_change_source": {
                "action": action, "requested_by": coordinator.id,
                "timezone": str(PolicyStore(session).church_tz()), "records": list(dependencies), **extra}})

    if action in {"add_slots", "set_recipe"}:
        count = args.get("count")
        maximum = 30 if action == "add_slots" else 20
        minimum = 1 if action == "add_slots" else 0
        if type(count) is not int or not minimum <= count <= maximum:
            raise ValueError(f"Count must be an integer from {minimum} to {maximum}")
        role = dependency("Role", args.get("role_id"))
        if action == "add_slots":
            event = dependency("Event", args.get("event_id"))
            if event.status != "scheduled" or event.starts_at <= now:
                raise ValueError("Choose a scheduled future event")
            slots = list(session.scalars(select(m.Shift.slot_index).where(
                m.Shift.event_id == event.id, m.Shift.role_id == role.id)))
            start = max(slots, default=-1) + 1
            for index in range(start, start + count):
                change("Shift", {"event_id": event.id, "role_id": role.id, "slot_index": index})
        else:
            event_type = dependency("EventType", args.get("event_type_id"))
            rows = list(session.scalars(select(m.RoleRecipe).where(
                m.RoleRecipe.event_type_id == event_type.id, m.RoleRecipe.role_id == role.id)))
            if len(rows) > 1:
                raise ValueError("Recipe is ambiguous; review the duplicate records manually")
            change("RoleRecipe", {"event_type_id": event_type.id, "role_id": role.id, "count": count},
                rows[0] if rows else None)
    elif action == "create_event_type":
        change("EventType", {"name": _text(args.get("name"), 80), "title_patterns": []})
    elif action in {"create_event", "update_event"}:
        event = dependency("Event", args.get("event_id")) if action == "update_event" else None
        if event and (event.status != "scheduled" or event.starts_at <= now):
            raise ValueError("Only scheduled future events may be updated")
        after = confirmations.values(event) if event else {"gcal_event_id": None,
            "event_type_id": None, "status": "scheduled"}
        for key in ("title", "event_type_id", "starts_at", "ends_at"):
            if key in args:
                after[key] = args[key]
        after["title"] = _text(after.get("title"), 200)
        _future(after.get("starts_at"), after.get("ends_at"), now)
        after["starts_at"] = _moment(after["starts_at"]).isoformat()
        after["ends_at"] = _moment(after["ends_at"]).isoformat()
        if after["event_type_id"] is not None:
            dependency("EventType", after["event_type_id"])
        change("Event", after, event)
    elif action == "pause_role":
        volunteer = dependency("Volunteer", args.get("volunteer_id"))
        role = dependency("Role", args.get("role_id"))
        after = confirmations.values(volunteer)
        prefs = dict(volunteer.preferences or {})
        prefs["paused_roles"] = sorted(set(prefs.get("paused_roles", []) + [role.name]))
        after["preferences"] = prefs
        change("Volunteer", after, volunteer)
    elif action == "mark_unavailable":
        volunteer = dependency("Volunteer", args.get("volunteer_id"))
        dates = args.get("dates")
        if not isinstance(dates, list) or not 1 <= len(dates) <= 62 or any(not isinstance(d, str) for d in dates):
            raise ValueError("Provide 1-62 specific ISO dates")
        today = now.astimezone(PolicyStore(session).church_tz()).date()
        dates = sorted({date.fromisoformat(d).isoformat() for d in dates})
        if any(date.fromisoformat(d) < today for d in dates):
            raise ValueError("Dates must be current or future")
        for month in sorted({d[:7] for d in dates}):
            rows = list(session.scalars(select(m.Availability).where(
                m.Availability.volunteer_id == volunteer.id, m.Availability.month == month).order_by(m.Availability.id)))
            row = rows[-1] if rows else None
            if row:
                dependency("Availability", row.id)
            after = confirmations.values(row) if row else {"volunteer_id": volunteer.id,
                "month": month, "available_dates": [], "unavailable_dates": [], "raw_reply": None, "parsed_at": None}
            selected = [d for d in dates if d[:7] == month]
            after["unavailable_dates"] = sorted(set((after["unavailable_dates"] or []) + selected))
            after["available_dates"] = [d for d in (after["available_dates"] or []) if d not in selected]
            after["parsed_at"] = now.isoformat()
            change("Availability", after, row, dates=selected, availability_ids=[r.id for r in rows])
            if row:
                dependencies.pop()  # Each month binds only its own availability.
    for payload in records:
        if problem := source_problem(session, payload, now):
            raise ValueError(problem)
    reviews = [confirmations.stage(session, now, payload, record=True) for payload in records
        if payload["before"] != payload["after"]]
    return {"approval_ids": [a.id for a in reviews], "applied": False,
        "proposed_changes": [{"record": a.payload["record"], "record_id": a.payload["record_id"],
            "changed_fields": [key for key, value in a.payload["after"].items()
                if a.payload["before"] is None or a.payload["before"].get(key) != value]} for a in reviews]}


def after_apply(session, approval, now):
    source = approval.payload.get("admin_change_source", {})
    if source.get("action") == "pause_role":
        role = next(d for d in source["records"] if d["record"] == "Role")
        session.add(m.Escalation(category="unclear", severity="normal", status="open", created_at=now,
            summary="Review existing future assignments after the approved role pause; no assignments were removed.",
            related_ids={"volunteer_id": approval.payload["applied_record_id"], "role_id": role["id"]}))
