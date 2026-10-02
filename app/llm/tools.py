"""Agent tools (PLAN.md section 14): schemas + implementations.

Every rule lives in the handler, not the prompt: request_send_text can only
reach volunteers code put in the current tranche, assign_volunteer re-checks
eligibility, schedule_next_tranche ignores any agent-supplied timing. The
model can call tools; it cannot bend them.
"""

import json
from dataclasses import dataclass
from typing import Any, Callable
from zoneinfo import ZoneInfo

from sqlalchemy import select

from app.core import eligibility, ranking
from app.core.send_gate import SendGate, SendStatus
from app.db import models as m

URGENCIES = ("critical", "high", "normal", "skip")
MAX_OUTREACH_BODY = 320


@dataclass
class ToolDef:
    name: str
    description: str
    parameters: dict
    handler: Callable[[dict], Any]

    def schema(self) -> dict:
        # Gloo requires the nested schema even on the Responses API.
        return {
            "type": "function",
            "function": {
                "name": self.name,
                "description": self.description,
                "parameters": self.parameters,
            },
        }


def _obj(properties: dict, required: list[str]) -> dict:
    return {"type": "object", "properties": properties, "required": required}


def shift_context(session, shift: m.Shift, now) -> dict:
    event = shift.event
    role = shift.role
    active = session.scalars(
        select(m.Assignment)
        .join(m.Shift, m.Assignment.shift_id == m.Shift.id)
        .where(
            m.Shift.event_id == event.id,
            m.Shift.role_id == role.id,
            m.Assignment.status.in_(eligibility.ACTIVE_ASSIGNMENT_STATUSES),
        )
    ).all()
    minimum = session.scalar(
        select(m.RoleRecipe.count).where(
            m.RoleRecipe.event_type_id == event.event_type_id, m.RoleRecipe.role_id == role.id
        )
    ) or 1
    return {
        "shift_id": shift.id,
        "event": event.title,
        "starts_at": event.starts_at.isoformat(),
        "hours_until_start": round((event.starts_at - now).total_seconds() / 3600, 1),
        "role": role.name,
        "criticality": role.criticality,
        "fill_policy": role.fill_policy,
        "required_qualifications": role.required_qualifications,
        "others_still_assigned": len(active),
        "minimum_needed": minimum,
    }


def fill_agent_tools(
    session, clock, gate: SendGate, fill_request: m.FillRequest, tz: str = "America/Denver"
) -> dict[str, ToolDef]:
    """Tool registry for one fill request, with the policy checks baked in."""

    def get_volunteer(args: dict) -> dict:
        vol = session.get(m.Volunteer, int(args["volunteer_id"]))
        if vol is None:
            return {"error": "no such volunteer"}
        return {
            "id": vol.id,
            "name": vol.name,
            "status": vol.status,
            "sms_opt_in": vol.sms_opt_in,
            "preferences": vol.preferences,
            "qualifications": [
                {"type": q.type, "status": q.status, "expires_on": str(q.expires_on) if q.expires_on else None}
                for q in vol.qualifications
            ],
        }

    def get_upcoming_assignments(args: dict) -> dict:
        rows = session.execute(
            select(m.Assignment, m.Event, m.Role)
            .join(m.Shift, m.Assignment.shift_id == m.Shift.id)
            .join(m.Event, m.Shift.event_id == m.Event.id)
            .join(m.Role, m.Shift.role_id == m.Role.id)
            .where(
                m.Assignment.volunteer_id == int(args["volunteer_id"]),
                m.Assignment.status.in_(eligibility.ACTIVE_ASSIGNMENT_STATUSES),
                m.Event.starts_at > clock.now(),
            )
            .order_by(m.Event.starts_at)
        ).all()
        return {
            "assignments": [
                {"assignment_id": a.id, "event": e.title, "role": r.name, "starts_at": e.starts_at.isoformat()}
                for a, e, r in rows
            ]
        }

    def get_shift_context(args: dict) -> dict:
        shift = session.get(m.Shift, int(args["shift_id"]))
        if shift is None:
            return {"error": "no such shift"}
        return shift_context(session, shift, clock.now())

    def find_candidates(args: dict) -> dict:
        shift = session.get(m.Shift, int(args["shift_id"]))
        if shift is None:
            return {"error": "no such shift"}
        exclude = tuple(int(x) for x in args.get("exclude_ids", []))
        ranked = ranking.rank_candidates(session, shift, clock.now(), exclude_ids=exclude, tz=tz)[:15]
        return {
            "candidates": [
                {"volunteer_id": c.volunteer.id, "name": c.volunteer.name, "score": c.score, "why": c.breakdown}
                for c in ranked
            ]
        }

    def request_send_text(args: dict) -> dict:
        # The fill agent only ever sends outreach; the purpose is fixed in
        # code, and whatever the model passes is ignored.
        body = args.get("body", "")
        if not body or len(body) > MAX_OUTREACH_BODY:
            return {"error": f"body must be 1-{MAX_OUTREACH_BODY} characters"}
        volunteer_id = int(args["volunteer_id"])
        outreach = session.scalar(
            select(m.Outreach).where(
                m.Outreach.fill_request_id == fill_request.id,
                m.Outreach.volunteer_id == volunteer_id,
                m.Outreach.tranche == fill_request.current_tranche,
                m.Outreach.message_id.is_(None),
                m.Outreach.response == "none",
            )
        )
        if outreach is None:
            return {"error": "volunteer is not in the current tranche (code decides who gets asked)"}
        shift = session.get(m.Shift, fill_request.shift_id)
        volunteer = session.get(m.Volunteer, volunteer_id)
        hours_until = (shift.event.starts_at - clock.now()).total_seconds() / 3600
        outcome = gate.send(
            body=body,
            purpose="outreach",
            volunteer=volunteer,
            kind="ai",
            role=shift.role,
            fill_request_id=fill_request.id,
            urgent=hours_until < 24,
        )
        if outcome.status is SendStatus.SENT:
            outreach.message_id = outcome.message_id
        return {"status": outcome.status.value, "detail": outcome.reason}

    def set_urgency(args: dict) -> dict:
        urgency = args.get("urgency")
        reason = (args.get("reason") or "").strip()
        if urgency not in URGENCIES:
            return {"error": f"urgency must be one of {URGENCIES}"}
        if not reason:
            return {"error": "adjusting urgency requires a stated reason"}
        fill_request.urgency = urgency
        return {"status": "ok", "urgency": urgency, "reason": reason}

    def assign_volunteer(args: dict) -> dict:
        shift = session.get(m.Shift, int(args["shift_id"]))
        volunteer = session.get(m.Volunteer, int(args["volunteer_id"]))
        if shift is None or volunteer is None:
            return {"error": "no such shift or volunteer"}
        result = eligibility.check(session, volunteer, shift, tz=tz)
        if not result:
            return {"error": "not eligible", "reasons": result.reasons}
        now = clock.now()
        assignment = m.Assignment(
            shift_id=shift.id, volunteer_id=volunteer.id, status="confirmed",
            source="fill", created_at=now, updated_at=now,
        )
        session.add(assignment)
        session.flush()
        return {"status": "assigned", "assignment_id": assignment.id}

    def cancel_assignment(args: dict) -> dict:
        assignment = session.get(m.Assignment, int(args["assignment_id"]))
        if assignment is None:
            return {"error": "no such assignment"}
        assignment.status = "cancelled"
        assignment.updated_at = clock.now()
        return {"status": "cancelled", "reason": args.get("reason", "")}

    def schedule_next_tranche(args: dict) -> dict:
        # Timing comes from policy (fill_agent.tranche_plan); the agent can ask
        # for the timer but never shorten it.
        from app.agents.fill_agent import next_wait_for

        wait = next_wait_for(session, fill_request, clock.now())
        fill_request.next_action_at = clock.now() + wait
        return {"status": "scheduled", "next_action_at": fill_request.next_action_at.isoformat()}

    def create_escalation(args: dict) -> dict:
        category = args.get("category", "unclear")
        severity = args.get("severity", "normal")
        if severity not in ("normal", "urgent"):
            severity = "normal"
        escalation = m.Escalation(
            category=category,
            severity=severity,
            summary=str(args.get("summary", ""))[:1000],
            related_ids={"fill_request_id": fill_request.id},
            status="open",
            created_at=clock.now(),
        )
        session.add(escalation)
        session.flush()
        return {"status": "created", "escalation_id": escalation.id}

    volunteer_id_param = {"volunteer_id": {"type": "integer"}}
    return {
        "get_volunteer": ToolDef(
            "get_volunteer", "Read a volunteer's profile, preferences, and qualifications.",
            _obj(volunteer_id_param, ["volunteer_id"]), get_volunteer,
        ),
        "get_upcoming_assignments": ToolDef(
            "get_upcoming_assignments", "List a volunteer's upcoming active assignments.",
            _obj(volunteer_id_param, ["volunteer_id"]), get_upcoming_assignments,
        ),
        "get_shift_context": ToolDef(
            "get_shift_context",
            "Role, criticality, timing, coverage, and minimums for a shift.",
            _obj({"shift_id": {"type": "integer"}}, ["shift_id"]), get_shift_context,
        ),
        "find_candidates": ToolDef(
            "find_candidates",
            "Deterministically ranked eligible replacements for a shift. You cannot override eligibility.",
            _obj({"shift_id": {"type": "integer"}, "exclude_ids": {"type": "array", "items": {"type": "integer"}}}, ["shift_id"]),
            find_candidates,
        ),
        "request_send_text": ToolDef(
            "request_send_text",
            "Ask the send gate to text one current-tranche volunteer your short, warm, personal ask "
            "(no guilt, easy out, under 300 chars). May be held for coordinator approval — that still counts as success.",
            _obj({**volunteer_id_param, "body": {"type": "string"}}, ["volunteer_id", "body"]),
            request_send_text,
        ),
        "set_urgency": ToolDef(
            "set_urgency",
            "Adjust the fill urgency (critical/high/normal/skip) with a stated reason.",
            _obj({"urgency": {"type": "string", "enum": list(URGENCIES)}, "reason": {"type": "string"}}, ["urgency", "reason"]),
            set_urgency,
        ),
        "assign_volunteer": ToolDef(
            "assign_volunteer",
            "Assign a volunteer to a shift. Eligibility is re-checked in code and the call fails if they are not eligible.",
            _obj({"shift_id": {"type": "integer"}, **volunteer_id_param}, ["shift_id", "volunteer_id"]),
            assign_volunteer,
        ),
        "cancel_assignment": ToolDef(
            "cancel_assignment", "Mark an assignment cancelled.",
            _obj({"assignment_id": {"type": "integer"}, "reason": {"type": "string"}}, ["assignment_id"]),
            cancel_assignment,
        ),
        "schedule_next_tranche": ToolDef(
            "schedule_next_tranche",
            "Schedule the next tranche timer. Timing comes from policy; it cannot be shortened.",
            _obj({}, []), schedule_next_tranche,
        ),
        "create_escalation": ToolDef(
            "create_escalation",
            "Escalate to a human (categories: sensitive, pastoral, unfillable, unclear, system_error, unknown_event).",
            _obj({"category": {"type": "string"}, "severity": {"type": "string"}, "summary": {"type": "string"}}, ["category", "summary"]),
            create_escalation,
        ),
    }
