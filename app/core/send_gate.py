"""The send gate: every outbound SMS passes through here (PLAN.md section 7).

Nothing else in the codebase may call an SMS provider. The model only ever
gets a request_send_text tool that lands here. Checks, in order:

1. Opt-out (STOP/START keywords handled by handle_stop_start below)
2. Sensitive block: nobody with an open sensitive escalation gets automated texts
3. Purpose policy: pre-approved templates send; outreach for needs_approval
   roles is held behind an approvals row; unknown purposes fail closed
4. Quiet hours (urgent same-day fills still respect 06:30-21:30)
5. Monthly ask budget (confirmations/reminders don't count)
6. Send via the provider and log to messages
"""

from dataclasses import dataclass
from datetime import datetime, timedelta
from enum import Enum

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.clock import Clock
from app.core import templates
from app.core.policies import PolicyStore, in_quiet_hours, next_send_time
from app.db import models as m
from app.sms.provider import SMSProvider

# Purposes that may go out without human approval (templates or agent-written
# text within an already-approved flow).
PRE_APPROVED_PURPOSES = {
    "reminder",
    "confirmation",
    "filled_thanks",
    "availability_ask",
    "cancellation_ack",
    "clarify",
    "clarify_shift",
    "thanks",
    "coordinator_notify",
    "escalation_notify",
    "unknown_number",
    "stop_confirm",
    "start_confirm",
    "admin_reply",
    "signup_reply",
}
# Purposes that count against the monthly ask budget.
ASK_PURPOSES = {"outreach", "availability_ask"}
VALID_PURPOSES = PRE_APPROVED_PURPOSES | ASK_PURPOSES

# Escalation states that still block automated contact.
BLOCKING_ESCALATION_STATUSES = ("open", "acknowledged")


class SendStatus(str, Enum):
    SENT = "sent"
    BLOCKED_TRANSPORT = "blocked_transport"
    BLOCKED_ELIGIBILITY = "blocked_eligibility"
    BLOCKED_OPT_OUT = "blocked_opt_out"
    BLOCKED_SENSITIVE = "blocked_sensitive"
    BLOCKED_BUDGET = "blocked_budget"
    HELD_QUIET_HOURS = "held_quiet_hours"
    HELD_FOR_APPROVAL = "held_for_approval"


@dataclass
class SendOutcome:
    status: SendStatus
    message_id: int | None = None
    approval_id: int | None = None
    retry_at: datetime | None = None
    reason: str = ""

    @property
    def sent(self) -> bool:
        return self.status is SendStatus.SENT


def has_open_sensitive_escalation(session: Session, volunteer_id: int) -> bool:
    escalations = session.scalars(
        select(m.Escalation).where(
            m.Escalation.category == "sensitive",
            m.Escalation.status.in_(BLOCKING_ESCALATION_STATUSES),
        )
    )
    return any(e.related_ids.get("volunteer_id") == volunteer_id for e in escalations)


class SendGate:
    def __init__(self, session: Session, clock: Clock, provider: SMSProvider, reply_to_message_id: int | None = None) -> None:
        self.session = session
        self.clock = clock
        self.provider = provider
        self.policies = PolicyStore(session)
        self.reply_to_message_id = reply_to_message_id
        self.gloo = None

    def send(
        self,
        *,
        body: str,
        purpose: str,
        volunteer: m.Volunteer | None = None,
        phone: str | None = None,
        kind: str = "template",
        role: m.Role | None = None,
        fill_request_id: int | None = None,
        urgent: bool = False,
        _approved: bool = False,
    ) -> SendOutcome:
        if volunteer is None and phone is None:
            raise ValueError("send() needs a volunteer or a phone number")
        if purpose not in VALID_PURPOSES:
            raise ValueError(f"unknown message purpose: {purpose!r}")  # fail closed
        now = self.clock.now()
        to_phone = phone or volunteer.phone
        opted_out = self.session.get(m.Policy, "sms_opt_out:" + to_phone)
        if opted_out and opted_out.value.get("value"):
            return SendOutcome(SendStatus.BLOCKED_OPT_OUT, reason="phone opted out")
        if volunteer is None:
            volunteer = self.session.scalar(select(m.Volunteer).where(m.Volunteer.phone == to_phone))
        phone_escalations = self.session.scalars(select(m.Escalation).where(
            m.Escalation.category == "sensitive",
            m.Escalation.status.in_(BLOCKING_ESCALATION_STATUSES),
        ))
        if any(e.related_ids.get("phone") == to_phone for e in phone_escalations):
            return SendOutcome(SendStatus.BLOCKED_SENSITIVE, reason="phone needs human follow-up")
        if hasattr(self.provider, "allows") and not self.provider.allows(to_phone):
            return SendOutcome(SendStatus.BLOCKED_TRANSPORT, reason="outside configured demo numbers")

        if volunteer is not None:
            # Transactional replies are permitted only for a sender-initiated
            # signup awaiting consent, never general outreach to an opted-out user.
            signup_reply = (purpose == "signup_reply" and
                            volunteer.preferences.get("signup_source") == "sms" and
                            volunteer.preferences.get("consent_pending") is True)
            if not volunteer.sms_opt_in and not signup_reply:
                return SendOutcome(SendStatus.BLOCKED_OPT_OUT, reason="volunteer opted out")
            if has_open_sensitive_escalation(self.session, volunteer.id):
                return SendOutcome(
                    SendStatus.BLOCKED_SENSITIVE,
                    reason="open sensitive escalation; only a human contacts them",
                )

        if purpose == "outreach" and not _approved:
            if role is None:
                raise ValueError("outreach requires the role for policy checks")
            if role.fill_policy == "needs_approval":
                approval = m.Approval(
                    kind="send_outreach",
                    payload={
                        "volunteer_id": volunteer.id if volunteer else None,
                        "phone": to_phone,
                        "body": body,
                        "purpose": purpose,
                        "kind": kind,
                        "role_id": role.id,
                        "fill_request_id": fill_request_id,
                        "urgent": urgent,
                        "transport": "mac_messages" if hasattr(self.provider, "allows") else "mock_or_twilio",
                    },
                    status="pending",
                    requested_at=now,
                )
                self.session.add(approval)
                self.session.flush()
                return SendOutcome(
                    SendStatus.HELD_FOR_APPROVAL,
                    approval_id=approval.id,
                    reason=f"{role.name} outreach needs coordinator approval",
                )

        start, end = (
            self.policies.urgent_quiet_hours() if urgent else self.policies.quiet_hours()
        )
        local_now = now.astimezone(self.policies.church_tz())
        test_reply = (hasattr(self.provider, "allows_test_signup_reply")
                      and self.provider.allows_test_signup_reply(to_phone, purpose, now))
        if in_quiet_hours(local_now, start, end) and not test_reply and not self._immediate_reply(to_phone, purpose, now):
            return SendOutcome(
                SendStatus.HELD_QUIET_HOURS,
                retry_at=next_send_time(local_now, start, end),
                reason="inside quiet hours",
            )

        if purpose == "outreach" and volunteer is not None:
            recent = self.session.scalar(select(m.Message.id).where(m.Message.volunteer_id == volunteer.id,
                m.Message.direction == "out", m.Message.purpose.in_(ASK_PURPOSES),
                m.Message.created_at > now-timedelta(hours=int(self.policies.get("outreach_cooldown_hours")))))
            if recent:
                return SendOutcome(SendStatus.BLOCKED_BUDGET, reason="outreach cooldown reached")

        if purpose in ASK_PURPOSES and volunteer is not None:
            if self._asks_this_month(volunteer.id, local_now) >= self.policies.ask_budget():
                return SendOutcome(
                    SendStatus.BLOCKED_BUDGET,
                    reason="monthly ask budget reached",
                )

        if self.gloo is not None and kind == "template" and purpose in {"clarify", "clarify_shift", "thanks", "cancellation_ack", "confirmation", "filled_thanks", "admin_reply"}:
            from app.core.signup_responder import compose_signup_reply
            body = compose_signup_reply(self.session, self.clock, self.gloo, body, (body,))
            kind = "ai"
        sid = self.provider.send(to_phone, body)
        message = m.Message(
            direction="out",
            volunteer_id=volunteer.id if volunteer else None,
            phone=to_phone,
            body=body,
            kind=kind,
            purpose=purpose,
            provider_sid=sid,
            status="queued" if sid.startswith("MAC") else "sent",
            created_at=now,
        )
        self.session.add(message)
        self.session.flush()
        if self._immediate_reply(to_phone, purpose, now):
            self.session.add(m.Notification(key=f"reply-proof:{message.id}", volunteer_id=message.volunteer_id,
                purpose=purpose, body="", state="sent", due_at=now, created_at=now,
                message_id=message.id, detail={"reply_to_message_id": self.reply_to_message_id}))
            self.session.flush()
        return SendOutcome(SendStatus.SENT, message_id=message.id)

    def _immediate_reply(self, phone, purpose, now):
        if purpose not in {"signup_reply", "clarify", "clarify_shift", "thanks", "confirmation", "filled_thanks", "cancellation_ack"}:
            return False
        incoming = self.session.get(m.Message, self.reply_to_message_id) if self.reply_to_message_id else None
        return bool(incoming and incoming.direction == "in" and incoming.phone == phone
                    and timedelta(0) <= now-incoming.created_at <= timedelta(minutes=10))

    def send_approved(self, approval: m.Approval) -> SendOutcome:
        """Send a message the coordinator approved. Safety checks still apply;
        only the approval hold itself is bypassed."""
        if approval.kind != "send_outreach" or approval.status != "approved":
            raise ValueError("send_approved needs an approved send_outreach approval")
        payload = approval.payload
        volunteer = (
            self.session.scalar(select(m.Volunteer).where(m.Volunteer.id == payload["volunteer_id"])
                                .with_for_update(key_share=True).execution_options(populate_existing=True))
            if payload.get("volunteer_id")
            else None
        )
        fill = self.session.get(m.FillRequest, payload.get("fill_request_id")) if payload.get("fill_request_id") else None
        outreach = None
        if fill and volunteer:
            outreach = self.session.scalar(select(m.Outreach).where(
                m.Outreach.fill_request_id == fill.id, m.Outreach.volunteer_id == volunteer.id,
                m.Outreach.tranche == fill.current_tranche, m.Outreach.message_id.is_(None)))
        if fill:
            from app.core import eligibility
            shift = self.session.get(m.Shift, fill.shift_id)
            if (fill.state != "waiting_approval" or shift.event.starts_at <= self.clock.now()
                    or volunteer is None or not eligibility.check(self.session, volunteer, shift,
                                                                  tz=self.policies.get("church_timezone"))):
                if outreach:
                    outreach.response = "blocked"
                return SendOutcome(SendStatus.BLOCKED_ELIGIBILITY, reason="approved offer is closed or no longer eligible")
        result = self.send(
            body=payload["body"],
            purpose=payload["purpose"],
            volunteer=volunteer,
            phone=payload.get("phone"),
            kind=payload.get("kind", "ai"),
            fill_request_id=payload.get("fill_request_id"),
            urgent=payload.get("urgent", False),
            _approved=True,
        )
        if outreach:
            if result.sent:
                outreach.message_id = result.message_id
            elif result.status != SendStatus.HELD_QUIET_HOURS:
                outreach.response = "blocked"
        return result

    def _asks_this_month(self, volunteer_id: int, local_now: datetime) -> int:
        month_start = local_now.replace(day=1, hour=0, minute=0, second=0, microsecond=0)
        return self.session.scalar(
            select(func.count())
            .select_from(m.Message)
            .where(
                m.Message.volunteer_id == volunteer_id,
                m.Message.direction == "out",
                m.Message.purpose.in_(ASK_PURPOSES),
                m.Message.created_at >= month_start,
            )
        )


def handle_stop_start(
    session: Session, clock: Clock, provider: SMSProvider, volunteer: m.Volunteer, body: str
) -> str | None:
    """Process STOP/START keywords. Returns 'stop', 'start', or None.

    The STOP confirmation is the one message that bypasses the opt-out check
    (carriers require a single confirmation; after that, never text again).
    """
    keyword = body.strip().upper()
    policies = PolicyStore(session)

    if keyword in ("STOP", "STOPALL", "UNSUBSCRIBE", "QUIT", "END"):
        confirm = volunteer.sms_opt_in  # confirm once; repeat STOPs get silence
        volunteer.sms_opt_in = False
        if volunteer.preferences.get("consent_pending"):
            volunteer.preferences = {**volunteer.preferences, "consent_pending": False}
        if confirm:
            _send_direct(session, clock, provider, volunteer, templates.stop_confirm(policies.church_name()), "stop_confirm")
        return "stop"

    if keyword in ("START", "UNSTOP"):
        volunteer.sms_opt_in = True
        _send_direct(session, clock, provider, volunteer, templates.start_confirm(policies.church_name()), "start_confirm")
        return "start"

    return None


def _send_direct(session, clock, provider, volunteer, body, purpose) -> None:
    """Opt-out keyword confirmations only — everything else uses SendGate.send."""
    sid = provider.send(volunteer.phone, body)
    session.add(
        m.Message(
            direction="out",
            volunteer_id=volunteer.id,
            phone=volunteer.phone,
            body=body,
            kind="template",
            purpose=purpose,
            provider_sid=sid,
            status="queued" if sid.startswith("MAC") else "sent",
            created_at=clock.now(),
        )
    )
    session.flush()
