"""Inbound message routing (PLAN.md section 8).

The Twilio webhook and the phone simulator both call handle_inbound(). The
parser is injected as a callable so tests and the simulator can run without
Gloo; production passes functools.partial(parse_inbound, gloo_client).

Agent hand-offs (fill agent, planning, admin agent) land in later phases;
until then the router returns routed_to so callers and tests can assert the
decision. Everything deterministic — logging, escalation, approval
resolution, confirmations, outreach response matching — happens here in code.
"""

from dataclasses import dataclass, field
from datetime import timedelta

from sqlalchemy import select

from app.clock import Clock
from app.core import templates
from app.core.policies import PolicyStore
from app.core.send_gate import SendGate, handle_stop_start
from app.db import models as m
from app.llm.parser import ParsedMessage
from app.sms.provider import SMSProvider

APPROVAL_YES = {"YES", "Y", "APPROVE", "OK"}
APPROVAL_NO = {"NO", "N", "REJECT"}
CONFIDENCE_FLOOR = 0.7
CLARIFY_WINDOW_HOURS = 24


@dataclass
class InboundResult:
    routed_to: str
    parsed: ParsedMessage | None = None
    escalation_id: int | None = None
    approval_id: int | None = None
    notes: list[str] = field(default_factory=list)


def handle_inbound(
    session, clock: Clock, provider: SMSProvider, phone: str, body: str, parser
) -> InboundResult:
    now = clock.now()
    gate = SendGate(session, clock, provider)
    policies = PolicyStore(session)
    volunteer = session.scalar(select(m.Volunteer).where(m.Volunteer.phone == phone))

    session.add(
        m.Message(
            direction="in",
            volunteer_id=volunteer.id if volunteer else None,
            phone=phone,
            body=body,
            kind="inbound",
            status="received",
            created_at=now,
        )
    )
    session.flush()

    # 1. Unknown numbers get one polite template and nothing else.
    if volunteer is None:
        gate.send(body=templates.unknown_number(policies.church_name()), purpose="unknown_number", phone=phone)
        return InboundResult(routed_to="unknown_number")

    # 2. Opt-out keywords beat everything.
    keyword = handle_stop_start(session, clock, provider, volunteer, body)
    if keyword:
        return InboundResult(routed_to=keyword)

    # 3. The coordinator: approval replies first, everything else to the admin agent.
    if volunteer.is_coordinator:
        return _handle_coordinator(session, gate, volunteer, body, now)

    # 4. Classify. The parser applies the keyword backstop itself.
    parsed: ParsedMessage = parser(body)
    result = InboundResult(routed_to="", parsed=parsed)

    # 5. Sensitive check on every volunteer message. Escalate to the pastor,
    #    block automated replies (the escalation row makes the send gate refuse),
    #    and keep routing the logistics without messaging them.
    if parsed.sensitive:
        result.escalation_id = _escalate_sensitive(session, gate, volunteer, body, parsed, now)

    # 6. Route by intent.
    if parsed.parse_error:
        result.routed_to = "escalated_unclear"
        result.escalation_id = result.escalation_id or _escalate(
            session, "system_error", "normal",
            f"Could not classify message from {volunteer.name}: {body!r}", volunteer, now,
        )
        return result

    intent = parsed.intent
    if intent != "confirm" and parsed.confidence < CONFIDENCE_FLOOR:
        intent = "unclear"

    if intent == "cancel":
        if not parsed.sensitive:
            gate.send(body=templates.cancellation_ack(volunteer.name), purpose="cancellation_ack", volunteer=volunteer)
        result.routed_to = "fill_agent"
    elif intent in ("accept", "decline", "partial"):
        matched = _record_outreach_response(session, volunteer, intent, now)
        result.notes.append(f"outreach_matched={matched}")
        result.routed_to = "fill_agent" if matched else "unmatched_reply"
        if not matched:
            result.routed_to = _clarify_or_escalate(session, gate, volunteer, body, now, result)
    elif intent == "availability":
        result.routed_to = "planning"
    elif intent == "confirm":
        confirmed = _confirm_next_assignment(session, volunteer, now)
        result.notes.append(f"confirmed_assignment={confirmed}")
        result.routed_to = "confirmed" if confirmed else "unmatched_reply"
    else:  # question, other, unclear, low confidence
        result.routed_to = _clarify_or_escalate(session, gate, volunteer, body, now, result)

    return result


def _handle_coordinator(session, gate: SendGate, coordinator, body: str, now) -> InboundResult:
    normalized = body.strip().upper().rstrip("!.")
    if normalized in APPROVAL_YES | APPROVAL_NO:
        approval = session.scalar(
            select(m.Approval).where(m.Approval.status == "pending").order_by(m.Approval.requested_at)
        )
        if approval is None:
            return InboundResult(routed_to="admin_agent", notes=["no pending approval"])
        approval.decided_at = now
        approval.decided_by = coordinator.name
        approval.via = "sms"
        if normalized in APPROVAL_YES:
            approval.status = "approved"
            if approval.kind == "send_outreach":
                outcome = gate.send_approved(approval)
                return InboundResult(
                    routed_to="approval", approval_id=approval.id, notes=[f"approved, send={outcome.status.value}"]
                )
            return InboundResult(routed_to="approval", approval_id=approval.id, notes=["approved"])
        approval.status = "rejected"
        return InboundResult(routed_to="approval", approval_id=approval.id, notes=["rejected"])
    return InboundResult(routed_to="admin_agent")


def _escalate(session, category: str, severity: str, summary: str, volunteer, now) -> int:
    pastor_or_coordinator = session.scalar(
        select(m.Volunteer).where(
            m.Volunteer.is_pastor if category == "sensitive" else m.Volunteer.is_coordinator
        )
    )
    escalation = m.Escalation(
        category=category,
        severity=severity,
        summary=summary,
        related_ids={"volunteer_id": volunteer.id},
        assigned_to=pastor_or_coordinator.id if pastor_or_coordinator else None,
        status="open",
        created_at=now,
    )
    session.add(escalation)
    session.flush()
    return escalation.id


def _escalate_sensitive(session, gate: SendGate, volunteer, body: str, parsed: ParsedMessage, now) -> int:
    escalation_id = _escalate(
        session,
        "sensitive",
        parsed.severity,
        f"{volunteer.name} may need personal care: {body!r}",
        volunteer,
        now,
    )
    pastor = session.scalar(select(m.Volunteer).where(m.Volunteer.is_pastor))
    if pastor is not None:
        gate.send(
            body=templates.pastor_alert(volunteer.name, body),
            purpose="escalation_notify",
            volunteer=pastor,
            urgent=parsed.severity == "urgent",
        )
    return escalation_id


def _record_outreach_response(session, volunteer, intent: str, now) -> bool:
    """Match a yes/no/partial to their most recent unanswered outreach."""
    outreach = session.scalar(
        select(m.Outreach)
        .join(m.FillRequest, m.Outreach.fill_request_id == m.FillRequest.id)
        .where(
            m.Outreach.volunteer_id == volunteer.id,
            m.Outreach.response == "none",
            m.FillRequest.state.in_(("open", "in_progress", "waiting_approval")),
        )
        .order_by(m.Outreach.id.desc())
    )
    if outreach is None:
        return False
    outreach.response = {"accept": "yes", "decline": "no", "partial": "partial"}[intent]
    outreach.responded_at = now
    return True


def _confirm_next_assignment(session, volunteer, now) -> bool:
    assignment = session.scalar(
        select(m.Assignment)
        .join(m.Shift, m.Assignment.shift_id == m.Shift.id)
        .join(m.Event, m.Shift.event_id == m.Event.id)
        .where(
            m.Assignment.volunteer_id == volunteer.id,
            m.Assignment.status == "approved",
            m.Event.starts_at > now,
        )
        .order_by(m.Event.starts_at)
    )
    if assignment is None:
        return False
    assignment.status = "confirmed"
    assignment.updated_at = now
    return True


def _clarify_or_escalate(session, gate: SendGate, volunteer, body: str, now, result: InboundResult) -> str:
    """One clarifying template question; if we already asked recently, escalate."""
    recent_clarify = session.scalar(
        select(m.Message)
        .where(
            m.Message.volunteer_id == volunteer.id,
            m.Message.direction == "out",
            m.Message.purpose == "clarify",
            m.Message.created_at >= now - timedelta(hours=CLARIFY_WINDOW_HOURS),
        )
        .order_by(m.Message.id.desc())
    )
    if recent_clarify is not None:
        result.escalation_id = result.escalation_id or _escalate(
            session, "unclear", "normal",
            f"Still unclear after a clarifying question. {volunteer.name} said: {body!r}",
            volunteer, now,
        )
        return "escalated_unclear"
    gate.send(body=templates.clarify_generic(volunteer.name), purpose="clarify", volunteer=volunteer)
    return "clarify"
