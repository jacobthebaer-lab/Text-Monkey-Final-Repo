"""Gloo reads coordinator context and stages validated, exact record proposals."""
from pathlib import Path

from sqlalchemy import select

from app.config import get_settings
from app.core import admin_changes, confirmations
from app.db import models as m
from app.llm.agent_loop import RunLogger, run_agent
from app.llm.tools import ToolDef

PROMPT = Path(__file__).resolve().parents[2] / "prompts/admin_agent.md"


def prepare(ctx, coordinator, command):
    if not coordinator.is_coordinator or coordinator.status != "active":
        raise ValueError("Active coordinator identity required")
    settings = getattr(ctx.gloo, "settings", get_settings())
    logger = RunLogger(ctx.session, ctx.clock, agent="admin_agent", trigger=command,
        model=settings.agent_model, log_dir=ctx.log_dir)
    from app.llm.parser import keyword_sensitive
    if keyword_sensitive(command):
        ctx.session.add(m.Escalation(category="sensitive", summary="Coordinator request needs private human review.",
            severity="normal", related_ids={"coordinator_id": coordinator.id}, status="open", created_at=ctx.clock.now()))
        result = {"outcome": "human_review", "final_text": None, "approval_ids": [], "applied": False}
        logger.step("decision", result=result)
        logger.close(result["outcome"])
        return result
    existing_reviews = set(ctx.session.scalars(select(m.Approval.id).where(
        m.Approval.kind == "confirm_record", m.Approval.status == "pending")))
    created = []
    context_read = [False]

    def read(args):
        from app.core.policies import PolicyStore
        context_read[0] = True
        session = ctx.session
        events = list(session.scalars(select(m.Event).where(m.Event.starts_at >= ctx.clock.now())
            .order_by(m.Event.starts_at).limit(60)))
        shifts = list(session.scalars(select(m.Shift).where(m.Shift.event_id.in_([e.id for e in events]))))
        return {
            "church_timezone": str(PolicyStore(session).church_tz()), "now": ctx.clock.now().isoformat(),
            "events": [{"id": e.id, **{k: v for k, v in confirmations.values(e).items()
                if k != "gcal_event_id"}} for e in events],
            "event_types": [{"id": t.id, "name": t.name} for t in session.scalars(select(m.EventType))],
            "recipes": [{"id": r.id, **confirmations.values(r)} for r in session.scalars(select(m.RoleRecipe))],
            "roles": [{"id": r.id, **confirmations.values(r)} for r in session.scalars(select(m.Role))],
            "volunteers": [{"id": v.id, "name": v.name, "status": v.status,
                "paused_roles": v.preferences.get("paused_roles", [])} for v in session.scalars(select(m.Volunteer))],
            "shifts": [{"id": s.id, "event_id": s.event_id, "role_id": s.role_id} for s in shifts],
            "assignments": [{"id": a.id, "shift_id": a.shift_id, "volunteer_id": a.volunteer_id, "status": a.status}
                for a in session.scalars(select(m.Assignment).where(m.Assignment.shift_id.in_([s.id for s in shifts]),
                    m.Assignment.status.in_(("proposed", "approved", "confirmed"))))],
        }

    def proposal(args):
        if not context_read[0]:
            return {"error": "Read current context before choosing existing IDs or dates"}
        result = admin_changes.propose(ctx, coordinator, args)
        created.extend(i for i in result["approval_ids"] if i not in created)
        return result

    tools = {
        "read_context": ToolDef("read_context", "Read saved schedules, assignments, roles, recipes and people",
            {"type": "object", "properties": {}, "additionalProperties": False}, read),
        "propose_change": ToolDef("propose_change", "Prepare exact human review. Never apply changes or contact anyone.",
            {"type": "object", "additionalProperties": False, "properties": {
                "action": {"type": "string", "enum": list(admin_changes.ACTIONS)},
                "event_id": {"type": "integer"}, "event_type_id": {"type": ["integer", "null"]},
                "role_id": {"type": "integer"}, "volunteer_id": {"type": "integer"},
                "count": {"type": "integer"}, "name": {"type": "string"}, "title": {"type": "string"},
                "starts_at": {"type": "string"}, "ends_at": {"type": "string"},
                "dates": {"type": "array", "items": {"type": "string"}}}, "required": ["action"]}, proposal),
    }
    result = run_agent(ctx.gloo, logger, model=settings.agent_model, instructions=PROMPT.read_text(),
        user_input=command, tools=tools, max_steps=settings.max_agent_steps)
    if result["outcome"] != "completed":
        # A partial tool run cannot leave its new proposals available after an outage.
        for identity in created:
            if identity not in existing_reviews:
                ctx.session.get(m.Approval, identity).status = "expired"
        ctx.session.add(m.Escalation(category="system_error", summary="Coordinator command held for Gloo review.",
            severity="normal", related_ids={"approval_ids": created}, status="open", created_at=ctx.clock.now()))
    logger.step("decision", result={**result, "approval_ids": created, "applied": False})
    logger.close(result["outcome"])
    return {**result, "approval_ids": created, "applied": False}


def apply(ctx, approval):
    """Compatibility for existing mock legacy reviews, never connected use."""
    from app.sms.mock_provider import MockSMSProvider
    if confirmations.enabled(ctx.session) or not isinstance(ctx.provider, MockSMSProvider):
        raise ValueError("Use signed-in exact record review")
    if approval.status != "approved" or approval.kind != "admin_change":
        raise ValueError("Explicit coordinator approval required")
    p, session = approval.payload, ctx.session
    if p["action"] == "add_slots":
        event, role = session.get(m.Event, p["event_id"]), session.get(m.Role, p["role_id"])
        if not event or event.status != "scheduled" or event.starts_at <= ctx.clock.now() or not role:
            raise ValueError("Event or role changed; review again")
        slots = list(session.scalars(select(m.Shift.slot_index).where(m.Shift.event_id == event.id, m.Shift.role_id == role.id)))
        start = max(slots, default=-1) + 1
        for index in range(start, start + p["count"]):
            session.add(m.Shift(event_id=event.id, role_id=role.id, slot_index=index))
    elif p["action"] == "pause_role":
        volunteer, role = session.get(m.Volunteer, p["volunteer_id"]), session.get(m.Role, p["role_id"])
        if not volunteer or not role:
            raise ValueError("Volunteer or role changed")
        prefs = dict(volunteer.preferences)
        prefs["paused_roles"] = sorted(set(prefs.get("paused_roles", []) + [role.name]))
        volunteer.preferences = prefs
        session.add(m.Escalation(category="unclear", severity="normal", status="open", created_at=ctx.clock.now(),
            summary=f"Review {volunteer.name}'s existing {role.name} assignments after the approved pause.",
            related_ids={"volunteer_id": volunteer.id, "role_id": role.id}))
    elif p["action"] == "mark_unavailable":
        for d in p["dates"]:
            row = session.scalar(select(m.Availability).where(m.Availability.volunteer_id == p["volunteer_id"],
                m.Availability.month == d[:7]).order_by(m.Availability.id.desc()))
            if row is None:
                row = m.Availability(volunteer_id=p["volunteer_id"], month=d[:7], available_dates=[], unavailable_dates=[])
                session.add(row)
            row.unavailable_dates = sorted(set((row.unavailable_dates or []) + [d]))
            row.parsed_at = ctx.clock.now()
    else:
        raise ValueError("Unknown approved action")
    session.flush()
    return {"applied": p["action"]}
