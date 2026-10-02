"""Inbound message routing (PLAN.md section 8).

The Twilio webhook and the phone simulator both call handle_inbound(). The
parser is injected as a callable so tests and the simulator can run without
Gloo; production passes functools.partial(parse_inbound, gloo_client).

Pass a fill_agent.FillContext as `ctx` to dispatch cancellations and outreach
replies to the real fill agent (Phase 4). Without it the router returns
routed_to markers only, which keeps it testable without Gloo. Planning and
admin agent hand-offs land in later phases. Everything deterministic —
logging, escalation, approval resolution, confirmations, outreach response
matching — happens here in code.
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
    session,
    clock: Clock,
    provider: SMSProvider,
    phone: str,
    body: str,
    parser,
    ctx=None,
    allow_signup: bool = False,
) -> InboundResult:
    now = clock.now()
    gate = SendGate(session, clock, provider)
    policies = PolicyStore(session)
    volunteer = session.scalar(select(m.Volunteer).where(m.Volunteer.phone == phone))

    incoming_message = m.Message(
            direction="in",
            volunteer_id=volunteer.id if volunteer else None,
            phone=phone,
            body=body,
            kind="inbound",
            status="received",
            created_at=now,
        )
    session.add(incoming_message)
    session.flush()

    # 1. Unknown numbers get one polite template and nothing else.
    if volunteer is None:
        if allow_signup and ctx is not None:
            from app.core.signup import request_signup

            if body.strip().upper() in {
                "STOP",
                "STOPALL",
                "UNSUBSCRIBE",
                "END",
                "QUIT",
            }:
                key = "sms_opt_out:" + phone
                if session.get(m.Policy, key) is None:
                    session.add(m.Policy(key=key, value={"value": True}))
                return InboundResult(routed_to="stop")
            optout = session.get(m.Policy, "sms_opt_out:" + phone)
            if optout:
                if body.strip().upper() in {"START", "UNSTOP"}:
                    session.delete(optout)
                    session.flush()
                else:
                    return InboundResult(routed_to="stop")
            signup = request_signup(session, clock, ctx.gloo, phone, body, gate=gate)
            if signup:
                return InboundResult(routed_to=signup)
            from app.core.signup_responder import compose_signup_reply
            gate.send(
                body=compose_signup_reply(session, clock, ctx.gloo,
                    "Welcome to Texty! Text JOIN and your first and last name to sign up for volunteering. Reply STOP to stop.", ("JOIN", "first and last name", "STOP")),
                purpose="signup_reply",
                phone=phone,
            )
            return InboundResult(routed_to="signup_invitation")
        gate.send(
            body=templates.unknown_number(policies.church_name()),
            purpose="unknown_number",
            phone=phone,
        )
        return InboundResult(routed_to="unknown_number")

    # 2. Opt-out keywords beat everything.
    if volunteer.preferences.get("consent_pending") and body.strip().upper() not in {
        "STOP",
        "STOPALL",
        "UNSUBSCRIBE",
        "QUIT",
        "END",
    }:
        from app.core.signup import finish_signup

        return InboundResult(
            routed_to=finish_signup(session, clock, gate, volunteer, body, gloo=ctx.gloo if ctx else None)
        )
    keyword = handle_stop_start(session, clock, provider, volunteer, body)
    if keyword:
        return InboundResult(routed_to=keyword)

    # 3. The coordinator: approval replies first, everything else to the admin agent.
    if volunteer.is_coordinator:
        return _handle_coordinator(session, gate, volunteer, body, now, ctx)

    # 3b. A bare number right after a which-shift question is the answer to it.
    if ctx is not None and body.strip().isdigit() and len(body.strip()) <= 2:
        recent = session.scalar(
            select(m.Message).where(
                m.Message.volunteer_id == volunteer.id,
                m.Message.direction == "out",
                m.Message.purpose == "clarify_shift",
                m.Message.created_at >= now - timedelta(hours=CLARIFY_WINDOW_HOURS),
            )
        )
        if recent is not None:
            from app.agents import fill_agent

            outcome = fill_agent.handle_shift_choice(ctx, volunteer, int(body.strip()))
            return InboundResult(routed_to="fill_agent", notes=[outcome.action])

    # 4. Classify. The parser applies the keyword backstop itself.
    parsed: ParsedMessage = parser(body)
    result = InboundResult(routed_to="", parsed=parsed)

    # 5. Sensitive check on every volunteer message. Escalate to the pastor,
    #    block automated replies (the escalation row makes the send gate refuse),
    #    and keep routing the logistics without messaging them.
    if parsed.sensitive:
        result.escalation_id = _escalate_sensitive(
            session, gate, volunteer, body, parsed, now
        )

    # 6. Route by intent.
    if parsed.parse_error:
        result.routed_to = "escalated_unclear"
        result.escalation_id = result.escalation_id or _escalate(
            session,
            "system_error",
            "normal",
            f"Could not classify message from {volunteer.name}: {body!r}",
            volunteer,
            now,
        )
        return result

    intent = parsed.intent
    if intent != "confirm" and parsed.confidence < CONFIDENCE_FLOOR:
        intent = "unclear"

    if intent == "cancel":
        if ctx is not None:
            from app.agents import fill_agent

            outcome = fill_agent.handle_cancellation(
                ctx, volunteer, shift_hint=parsed.shift_hint, sensitive=parsed.sensitive
            )
            result.notes.append(outcome.action)
        elif not parsed.sensitive:
            gate.send(
                body=templates.cancellation_ack(volunteer.name),
                purpose="cancellation_ack",
                volunteer=volunteer,
            )
        result.routed_to = "fill_agent"
    elif intent in ("accept", "decline", "partial"):
        outreach = _record_outreach_response(session, volunteer, intent, now)
        result.notes.append(f"outreach_matched={outreach is not None}")
        if outreach is not None:
            if ctx is not None:
                from app.agents import fill_agent

                outcome = fill_agent.on_outreach_reply(ctx, volunteer, outreach, intent)
                result.notes.append(outcome.action)
            result.routed_to = "fill_agent"
        else:
            result.routed_to = _clarify_or_escalate(
                session, gate, volunteer, body, now, result
            )
    elif intent == "availability":
        if ctx is not None and not parsed.sensitive:
            from app.core.serving_requests import save_serving_request
            save_serving_request(session, clock, gate, ctx.gloo, volunteer, body, parsed, incoming_message.id)
            result.notes.append("serving_request_saved_for_review")
        result.routed_to = "planning"
    elif intent == "confirm":
        confirmed = _confirm_next_assignment(session, volunteer, now)
        result.notes.append(f"confirmed_assignment={confirmed}")
        result.routed_to = "confirmed" if confirmed else "unmatched_reply"
    else:  # question, other, unclear, low confidence
        result.routed_to = _clarify_or_escalate(
            session, gate, volunteer, body, now, result
        )

    return result


def _handle_coordinator(
    session, gate: SendGate, coordinator, body: str, now, ctx=None
) -> InboundResult:
    normalized = body.strip().upper().rstrip("!.")
    if normalized in APPROVAL_YES | APPROVAL_NO:
        query = select(m.Approval).where(m.Approval.status == "pending")
        if hasattr(gate.provider, "allows"):
            query = query.where(
                m.Approval.payload["transport"].as_string() == "mac_messages"
            )
        oldest = session.scalar(query.order_by(m.Approval.requested_at))
        if oldest is None:
            return InboundResult(routed_to="admin_agent", notes=["no pending approval"])

        notes = decide_approval(
            session,
            gate,
            oldest,
            approve=normalized in APPROVAL_YES,
            decided_by=coordinator.name,
            via="sms",
            now=now,
            ctx=ctx,
        )
        return InboundResult(routed_to="approval", approval_id=oldest.id, notes=notes)
    return InboundResult(routed_to="admin_agent")


def decide_approval(
    session,
    gate: SendGate,
    approval: m.Approval,
    *,
    approve: bool,
    decided_by: str,
    via: str,
    now,
    ctx=None,
) -> list[str]:
    """Resolve an approval — and its whole batch: one YES covers every pending
    approval for the same fill request (the coordinator was asked about them
    as a group). Used by both the SMS reply path and the approvals web page."""
    fill_request_id = approval.payload.get("fill_request_id")
    if fill_request_id is not None:
        batch = session.scalars(
            select(m.Approval).where(m.Approval.status == "pending")
        ).all()
        batch = [
            a for a in batch if a.payload.get("fill_request_id") == fill_request_id
        ]
        if hasattr(gate.provider, "allows"):
            batch = [a for a in batch if a.payload.get("transport") == "mac_messages"]
    else:
        batch = [approval]

    notes = []
    for item in batch:
        item.decided_at = now
        item.decided_by = decided_by
        item.via = via
        if approve:
            item.status = "approved"
            if item.kind == "signup":
                from app.core.signup import approve_signup

                approve_signup(session, gate.clock, item)
                notes.append(
                    f"approved signup #{item.id}; consent and qualifications remain unverified"
                )
            elif item.kind == "send_outreach":
                outcome = gate.send_approved(item)
                notes.append(f"approved #{item.id}, send={outcome.status.value}")
            else:
                notes.append(f"approved #{item.id}")
        else:
            item.status = "rejected"
            notes.append(f"rejected #{item.id}")

    if ctx is not None and fill_request_id is not None and approve:
        from app.agents import fill_agent

        fill_agent.on_outreach_approved(ctx, fill_request_id)
    return notes


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


def _record_outreach_response(session, volunteer, intent: str, now) -> m.Outreach | None:
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
        return None
    outreach.response = {"accept": "yes", "decline": "no", "partial": "partial"}[intent]
    outreach.responded_at = now
    return outreach


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
