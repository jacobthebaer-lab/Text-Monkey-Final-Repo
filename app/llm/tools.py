"""Agent tools (PLAN.md section 14): schemas + implementations.

Gloo chooses the replacements. Tools enforce eligibility, consent, batch
limits and timing; assignments happen only after an affirmative SMS reply.
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
MAX_OUTREACH_BODY = 260


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
    session,
    clock,
    gate: SendGate,
    fill_request: m.FillRequest,
    tz: str = "America/Denver",
    max_candidates: int | None = None,
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
                {
                    "type": q.type,
                    "status": q.status,
                    "expires_on": str(q.expires_on) if q.expires_on else None,
                }
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
                {
                    "assignment_id": a.id,
                    "event": e.title,
                    "role": r.name,
                    "starts_at": e.starts_at.isoformat(),
                }
                for a, e, r in rows
            ]
        }

    def get_shift_context(args: dict) -> dict:
        shift = session.get(m.Shift, int(args["shift_id"]))
        if shift is None:
            return {"error": "no such shift"}
        return shift_context(session, shift, clock.now())

    def find_candidates(args: dict) -> dict:
        if int(args["shift_id"]) != fill_request.shift_id:
            return {"error": "only the current shift may be filled"}
        candidates = replacement_pool(session, fill_request, clock.now(), tz)
        return {"candidates": [candidate_context(c) for c in candidates]}

    def choose_replacements(args: dict) -> dict:
        from app.core import offer_windows as offers
        from app.core import algorithm_outreach as algorithm
        ids = args.get("volunteer_ids")
        reason = args.get("reason")
        if not isinstance(reason, str) or not reason.strip() or len(reason) > 1000:
            return {"error": "explain your choice in 1-1000 characters"}
        if not isinstance(ids, list) or not ids or any(type(v) is not int for v in ids):
            return {"error": "choose at least one eligible volunteer ID"}
        planned = algorithm.selected_ids(session, fill_request)
        if planned:
            if ids != list(planned) or fill_request.state != "in_progress":
                return {"error": "confirm exactly the application-selected batch; recipients cannot be substituted"}
            return {"status": "chosen", "volunteer_ids": ids, "reason": reason.strip()}
        if len(set(ids)) != len(ids) or (
            len(ids) > 1 or (max_candidates is not None and len(ids) > max_candidates)
        ):
            return {"error": "duplicate IDs or batch limit exceeded"}
        existing = session.scalar(
            select(m.Outreach.id).where(
                m.Outreach.fill_request_id == fill_request.id,
                m.Outreach.tranche == fill_request.current_tranche,
            )
        )
        if existing is not None or fill_request.state != "in_progress":
            return {"error": "this batch is already chosen or the fill is closed"}
        # Lock a chosen batch in ID order. NO KEY UPDATE serializes contact
        # checks while allowing inbound/outreach foreign-key inserts to proceed.
        session.scalars(select(m.Volunteer).where(m.Volunteer.id.in_(ids)).order_by(m.Volunteer.id)
                        .with_for_update(key_share=True).execution_options(populate_existing=True)).all()
        pool = {
            c.volunteer.id
            for c in replacement_pool(session, fill_request, clock.now(), tz)
        }
        if not set(ids) <= pool:
            return {
                "error": "choose only available, opted-in, eligible volunteers not already asked"
            }
        for volunteer_id in ids:
            session.add(
                m.Outreach(
                    fill_request_id=fill_request.id,
                    volunteer_id=volunteer_id,
                    tranche=fill_request.current_tranche,
                )
            )
        session.flush()
        return {"status": "chosen", "volunteer_ids": ids, "reason": reason.strip()}

    def request_send_text(args: dict) -> dict:
        purpose = args.get("purpose", "outreach")
        if purpose != "outreach":
            return {
                "error": "the fill agent may only send outreach; other messages are templated by code"
            }
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
            return {"error": "choose_replacements must select this volunteer first"}
        shift = session.get(m.Shift, fill_request.shift_id)
        volunteer = session.scalar(select(m.Volunteer).where(m.Volunteer.id == volunteer_id).with_for_update(key_share=True).execution_options(populate_existing=True))
        if not eligibility.check(session, volunteer, shift, tz=tz):
            return {"error": "volunteer is no longer eligible"}
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
        elif outcome.approval_id:
            approval = session.get(m.Approval, outcome.approval_id)
            approval.payload = {**approval.payload, "outreach_id": outreach.id}
        elif outcome.status not in (SendStatus.HELD_QUIET_HOURS, SendStatus.BLOCKED_TRANSPORT):
            from app.core import offer_windows as offers
            offers.close(session, outreach, "blocked", clock.now())
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

    def schedule_next_tranche(args: dict) -> dict:
        if fill_request.state != "in_progress":
            return {"error": "the fill is no longer in progress"}
        # Dispatch owns the timer; model calls cannot reset or extend it.
        from app.core import offer_windows as offers
        shift = session.get(m.Shift, fill_request.shift_id)
        rows = session.scalars(select(m.Outreach).where(m.Outreach.fill_request_id == fill_request.id,
            m.Outreach.response.in_(offers.OPEN_RESPONSES))).all()
        active = [offers.metadata(session, o) for o in rows]
        deadlines = [r.expires_at for r in active if r and r.state == "offer_active"]
        fill_request.next_action_at = min(deadlines) if deadlines else offers.cutoff(session, shift.event.starts_at)
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
        fill_request.state = "escalated"
        fill_request.next_action_at = None
        session.flush()
        return {"status": "created", "escalation_id": escalation.id}

    volunteer_id_param = {"volunteer_id": {"type": "integer"}}
    return {
        "get_volunteer": ToolDef(
            "get_volunteer",
            "Read a volunteer's profile, preferences, and qualifications.",
            _obj(volunteer_id_param, ["volunteer_id"]),
            get_volunteer,
        ),
        "get_upcoming_assignments": ToolDef(
            "get_upcoming_assignments",
            "List a volunteer's upcoming active assignments.",
            _obj(volunteer_id_param, ["volunteer_id"]),
            get_upcoming_assignments,
        ),
        "get_shift_context": ToolDef(
            "get_shift_context",
            "Role, criticality, timing, coverage, and minimums for a shift.",
            _obj({"shift_id": {"type": "integer"}}, ["shift_id"]),
            get_shift_context,
        ),
        "find_candidates": ToolDef(
            "find_candidates",
            "All eligible replacements, listed by ID. You decide whom to ask using their preferences and history.",
            _obj(
                {
                    "shift_id": {"type": "integer"},
                    "exclude_ids": {"type": "array", "items": {"type": "integer"}},
                },
                ["shift_id"],
            ),
            find_candidates,
        ),
        "choose_replacements": ToolDef(
            "choose_replacements",
            "Choose whom to ask in this batch and explain why. Call before sending texts.",
            _obj(
                {
                    "volunteer_ids": {"type": "array", "items": {"type": "integer"}},
                    "reason": {"type": "string"},
                },
                ["volunteer_ids", "reason"],
            ),
            choose_replacements,
        ),
        "request_send_text": ToolDef(
            "request_send_text",
            "Ask the send gate to text one current-tranche volunteer your short, warm, personal ask "
            "(no guilt, easy out, under 260 chars; the app appends natural yes/no instructions and the local reply deadline). May be held for coordinator approval — that still counts as success.",
            _obj(
                {
                    **volunteer_id_param,
                    "body": {"type": "string"},
                    "purpose": {"type": "string", "enum": ["outreach"]},
                },
                ["volunteer_id", "body"],
            ),
            request_send_text,
        ),
        "set_urgency": ToolDef(
            "set_urgency",
            "Adjust the fill urgency (critical/high/normal/skip) with a stated reason.",
            _obj(
                {
                    "urgency": {"type": "string", "enum": list(URGENCIES)},
                    "reason": {"type": "string"},
                },
                ["urgency", "reason"],
            ),
            set_urgency,
        ),
        "schedule_next_tranche": ToolDef(
            "schedule_next_tranche",
            "Schedule the next tranche timer. Timing comes from policy; it cannot be shortened.",
            _obj({}, []),
            schedule_next_tranche,
        ),
        "create_escalation": ToolDef(
            "create_escalation",
            "Escalate to a human (categories: sensitive, pastoral, unfillable, unclear, system_error, unknown_event).",
            _obj(
                {
                    "category": {"type": "string"},
                    "severity": {"type": "string"},
                    "summary": {"type": "string"},
                },
                ["category", "summary"],
            ),
            create_escalation,
        ),
    }


def replacement_pool(session, fill_request, now, tz):
    """Hard eligibility pool. Clyde's adapter, not model preferences, ranks it."""
    from app.core.offer_windows import sender_busy
    from app.core.algorithm_outreach import contacted_for_event
    exclude = set(
        session.scalars(
            select(m.Outreach.volunteer_id).where(
                m.Outreach.fill_request_id == fill_request.id
            )
        )
    )
    cancelled = (
        session.get(m.Assignment, fill_request.cancelled_assignment_id)
        if fill_request.cancelled_assignment_id
        else None
    )
    if cancelled:
        exclude.add(cancelled.volunteer_id)
    exclude.update(contacted_for_event(session, fill_request))
    shift = session.get(m.Shift, fill_request.shift_id)
    return sorted(
        (c for c in ranking.rank_candidates(session, shift, now, exclude_ids=tuple(exclude), tz=tz)
         if not sender_busy(session, c.volunteer.id)
         and not c.volunteer.preferences.get("consent_pending")
         and not (session.get(m.Policy, "sms_opt_out:" + c.volunteer.phone) and
                  session.get(m.Policy, "sms_opt_out:" + c.volunteer.phone).value.get("value"))),
        key=lambda c: c.volunteer.id,
    )


def candidate_context(candidate):
    return {
        "volunteer_id": candidate.volunteer.id,
        "name": candidate.volunteer.name,
        "preferences": candidate.volunteer.preferences,
        "history_signals": candidate.breakdown,
    }
