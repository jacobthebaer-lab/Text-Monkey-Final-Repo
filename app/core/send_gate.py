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
from app.core import templates, offer_windows as offers
from app.core.policies import PolicyStore, in_quiet_hours, next_send_time
from app.core.message_style import outbound_style_problem, validate_outbound_style
from app.db import models as m
from app.sms.provider import SMSProvider
from app.sms.transport import transport_name, session_transport

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
    "booking_status",
    "manual",
}
# Purposes that count against the monthly ask budget.
ASK_PURPOSES = {"outreach", "availability_ask"}
UNSENT_STATUSES = ("blocked_confirmation", "blocked_opt_out", "blocked_style", "blocked_policy", "superseded")
VALID_PURPOSES = PRE_APPROVED_PURPOSES | ASK_PURPOSES

# Escalation states that still block automated contact.
BLOCKING_ESCALATION_STATUSES = ("open", "acknowledged")


class SendStatus(str, Enum):
    SENT = "sent"
    BLOCKED_POLICY = "blocked_policy"
    BLOCKED_STYLE = "blocked_style"
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
        select(m.Escalation.related_ids).where(
            m.Escalation.category == "sensitive",
            m.Escalation.status.in_(BLOCKING_ESCALATION_STATUSES),
        )
    )
    return any(e.get("volunteer_id") == volunteer_id for e in escalations)


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
        _confirmation: m.Approval | None = None,
        conversation: dict | None = None,
    ) -> SendOutcome:
        if volunteer is None and phone is None:
            raise ValueError("send() needs a volunteer or a phone number")
        if purpose not in VALID_PURPOSES:
            raise ValueError(f"unknown message purpose: {purpose!r}")  # fail closed
        if volunteer and purpose in {"coordinator_notify", "escalation_notify"} and volunteer.preferences.get("admin_text_owner") and volunteer.status != "active":
            return SendOutcome(SendStatus.BLOCKED_ELIGIBILITY, reason="admin text updates are paused")
        if not isinstance(body, str) or not 0 < len(body.strip()) <= 1600:
            raise ValueError("Text must contain 1-1600 characters")
        if problem := outbound_style_problem(body):
            return SendOutcome(SendStatus.BLOCKED_STYLE, reason=problem)
        now = self.clock.now()
        to_phone = phone or volunteer.phone
        selected = getattr(self.provider, 'test_sessions', {}).get(to_phone)
        if selected is not None:
            self.session.info['mac_test_session'] = selected
        from app.core import confirmations
        needs_confirmation = confirmations.enabled(self.session) or purpose == "manual"
        if needs_confirmation:
            self.session.info["confirmation_now"] = now
        if volunteer is None:
            volunteer = self.session.scalar(select(m.Volunteer).where(m.Volunteer.phone == to_phone))
        from app.core.consent_controls import acknowledgement_problem
        control_meta = (_confirmation.payload.get('conversation', {}) if _confirmation else conversation) or {}
        if not isinstance(control_meta, dict):
            control_meta = {}
        stop_ack = (purpose == 'stop_confirm' and kind == 'ai' and not acknowledgement_problem(
            self.session, purpose=purpose, volunteer=volunteer, phone=to_phone, body=body,
            key=control_meta.get('control_key')))
        opted_out = self.session.get(m.Policy, "sms_opt_out:" + to_phone)
        if opted_out and opted_out.value.get("value") and not stop_ack:
            return SendOutcome(SendStatus.BLOCKED_OPT_OUT, reason="phone opted out")
        if volunteer is None:
            volunteer = self.session.scalar(select(m.Volunteer).where(m.Volunteer.phone == to_phone))
        from app.core import outbound_conversation as conversation_policy
        if _confirmation is not None:
            conversation_meta = _confirmation.payload.get('conversation', {})
            conversation_error = None
        else:
            if purpose == "outreach" and fill_request_id and volunteer:
                # Scope comes from the application tool registry, never model
                # conversation JSON. Fresh source checks also run before delivery.
                fill = self.session.get(m.FillRequest, fill_request_id)
                outreach = self.session.scalar(select(m.Outreach).where(
                    m.Outreach.fill_request_id == fill.id,
                    m.Outreach.tranche == fill.current_tranche,
                    m.Outreach.volunteer_id == volunteer.id)) if fill else None
                conversation = {"outreach_id": outreach.id if outreach else None}
            conversation_meta, conversation_error = conversation_policy.metadata(self.session,
                purpose=purpose, volunteer=volunteer, phone=to_phone, now=now,
                supplied=conversation, reply_id=self.reply_to_message_id)
        conversation_error = conversation_error or conversation_policy.problem(self.session,
            purpose=purpose, volunteer=volunteer, phone=to_phone, body=body, now=now,
            meta=conversation_meta, approval=_confirmation)
        if conversation_error:
            conversation_policy.record_suppression(self.session, to_phone, purpose, body, now, conversation_error)
            return SendOutcome(SendStatus.BLOCKED_POLICY, reason=conversation_error)
        phone_escalations = self.session.scalars(select(m.Escalation.related_ids).where(
            m.Escalation.category == "sensitive",
            m.Escalation.status.in_(BLOCKING_ESCALATION_STATUSES),
        ))
        if any(e.get("phone") == to_phone for e in phone_escalations):
            return SendOutcome(SendStatus.BLOCKED_SENSITIVE, reason="phone needs human follow-up")
        if hasattr(self.provider, "allows") and not self.provider.allows(to_phone):
            return SendOutcome(SendStatus.BLOCKED_TRANSPORT, reason="outside configured demo numbers")

        if volunteer is not None:
            # Transactional replies are permitted only for a sender-initiated
            # signup awaiting consent, never general outreach to an opted-out user.
            signup_reply = (purpose == "signup_reply" and
                            volunteer.preferences.get("signup_source") == "sms" and
                            volunteer.preferences.get("consent_pending") is True)
            if cloud_demo_pending(self.provider, self.session, to_phone) and purpose == "signup_reply":
                signup_reply = True
            if not volunteer.sms_opt_in and not signup_reply and not stop_ack:
                return SendOutcome(SendStatus.BLOCKED_OPT_OUT, reason="volunteer opted out")
            if has_open_sensitive_escalation(self.session, volunteer.id):
                return SendOutcome(
                    SendStatus.BLOCKED_SENSITIVE,
                    reason="open sensitive escalation; only a human contacts them",
                )

        outreach = None
        offer_meta = None
        if purpose == "outreach" and fill_request_id and volunteer:
            fill = self.session.get(m.FillRequest, fill_request_id)
            slot = self.session.get(m.Shift, fill.shift_id)
            self.session.scalar(select(m.Event).where(m.Event.id == slot.event_id).with_for_update().execution_options(populate_existing=True))
            self.session.scalar(select(m.Shift).where(m.Shift.id == slot.id).with_for_update().execution_options(populate_existing=True))
            self.session.scalar(select(m.Role).where(m.Role.id == slot.role_id).with_for_update(read=True).execution_options(populate_existing=True))
            volunteer = self.session.scalar(select(m.Volunteer).where(m.Volunteer.id == volunteer.id)
                .with_for_update(key_share=True).execution_options(populate_existing=True))
            fill = self.session.scalar(select(m.FillRequest).where(m.FillRequest.id == fill.id)
                .with_for_update().execution_options(populate_existing=True))
            outreach = self.session.scalar(select(m.Outreach).where(m.Outreach.fill_request_id == fill.id,
                m.Outreach.volunteer_id == volunteer.id, m.Outreach.tranche == fill.current_tranche)
                .with_for_update().execution_options(populate_existing=True))
            now = offers.decision_time(self.session, self.clock)
            from app.core import eligibility
            occupied = self.session.scalar(select(m.Assignment.id).where(m.Assignment.shift_id == slot.id,
                m.Assignment.status.in_(eligibility.ACTIVE_ASSIGNMENT_STATUSES)))
            from app.core.algorithm_outreach import conflicting_offer
            other = conflicting_offer(self.session, outreach) if outreach else True
            if (not outreach or outreach.response not in offers.OPEN_RESPONSES or outreach.message_id or
                    fill.state not in offers.OPEN_FILLS or occupied or other or not volunteer.sms_opt_in or
                    offers.delivery_hold(self.session, volunteer_id=volunteer.id, shift_id=slot.id,
                                         exclude_outreach_id=outreach.id if outreach else None) or
                    not eligibility.check(self.session, volunteer, slot, tz=self.policies.get("church_timezone"))):
                return SendOutcome(SendStatus.BLOCKED_ELIGIBILITY, reason="offer is closed or no longer eligible")
            offer_meta = offers.metadata(self.session, outreach)
            if not offer_meta:
                offer_meta = offers.prepare(self.session, outreach, body, now)
            if not offer_meta:
                fill.state, fill.next_action_at = "escalated", None
                offers.close(self.session, outreach, "revoked", now)
                offers.task_once(self.session, fill, now, "Too little time remains for an automatic offer; coordinator review required.")
                return SendOutcome(SendStatus.BLOCKED_ELIGIBILITY, reason="too little time for an offer")
            body = offer_meta.body

        if problem := outbound_style_problem(body):
            return SendOutcome(SendStatus.BLOCKED_STYLE, reason=problem)
        cloud = transport_name(self.provider) == "google_voice"
        if cloud and self.provider.settings.google_voice_demo_mode:
            from app.integrations.google_voice_demo import demo_text_problem
            if error := demo_text_problem(self.session, self.provider, to_phone, body, purpose, now, reply_id=self.reply_to_message_id):
                return SendOutcome(SendStatus.BLOCKED_POLICY, reason=error)
        if cloud or purpose == "manual":
            from app.integrations import google_voice_policy
            if cloud and not google_voice_policy.google_voice_steps_allowed(self.provider.settings):
                return SendOutcome(SendStatus.BLOCKED_TRANSPORT, reason=google_voice_policy.POLICY_HOLD_MESSAGE)
            from app.core.cloud_composition import require_composition, reviewed_composition
            selected = getattr(self.provider, "test_sessions", {}).get(to_phone)
            if _confirmation is None:
                require_composition(self.session, self.clock, self.gloo, to_phone, body, selected)
                kind = "ai"
            elif not reviewed_composition(self.session, _confirmation, selected):
                return SendOutcome(SendStatus.BLOCKED_ELIGIBILITY, reason="Exact text has no Gloo composition proof")
        if needs_confirmation:
            if _confirmation is None:
                # Resolve any model wording before it is shown to a human.
                if self.gloo is not None and kind == "template" and purpose in {"clarify", "clarify_shift", "thanks", "cancellation_ack", "confirmation", "filled_thanks", "admin_reply"}:
                    from app.core.signup_responder import compose_signup_reply
                    body = compose_signup_reply(self.session, self.clock, self.gloo, body, (body,), volunteer=volunteer, phone=to_phone)
                    kind = "ai"
                if problem := outbound_style_problem(body):
                    return SendOutcome(SendStatus.BLOCKED_STYLE, reason=problem)
                approval = confirmations.stage_text(self, {"phone": to_phone, "volunteer_id": volunteer.id if volunteer else None,
                    "body": body, "purpose": purpose, "kind": kind, "role_id": role.id if role else None,
                    "fill_request_id": fill_request_id, "urgent": urgent,
                    "conversation": conversation_meta,
                    "transport": transport_name(self.provider)})
                if cloud or purpose == "manual":
                    from app.core.cloud_composition import record_review
                    record_review(self.session, approval, selected, now)
                return SendOutcome(SendStatus.HELD_FOR_APPROVAL, approval_id=approval.id, reason="Review exact recipient and text in the signed-in dashboard")
            if _confirmation.payload.get("message_id") is not None:
                return SendOutcome(SendStatus.BLOCKED_ELIGIBILITY, reason="Exact approval already consumed")
            if confirmations.delivery_problem(self.session, self.provider, _confirmation, now):
                return SendOutcome(SendStatus.BLOCKED_ELIGIBILITY, reason="Exact confirmation no longer valid")
            p = _confirmation.payload
            if body != p["body"] or to_phone != p["phone"] or purpose != p["purpose"] or fill_request_id != p.get("fill_request_id"):
                return SendOutcome(SendStatus.BLOCKED_ELIGIBILITY, reason="Approved content changed")

        if purpose == "outreach" and not _approved and not needs_confirmation:
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
                        "transport": transport_name(self.provider),
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
        from app.integrations.google_voice_quiet_test import deadline as quiet_test_deadline
        quiet_test = quiet_test_deadline(self.session, self.provider, to_phone, purpose, now, approval=_confirmation)
        if not stop_ack and in_quiet_hours(local_now, start, end) and not test_reply and not quiet_test and not self._immediate_reply(to_phone, purpose, now):
            return SendOutcome(
                SendStatus.HELD_QUIET_HOURS,
                retry_at=next_send_time(local_now, start, end),
                reason="inside quiet hours",
            )

        if purpose == "outreach" and volunteer is not None:
            recent = self.session.scalar(select(m.Message.id).where(m.Message.volunteer_id == volunteer.id,
                m.Message.direction == "out", m.Message.purpose.in_(ASK_PURPOSES),
                m.Message.status.not_in(UNSENT_STATUSES),
                m.Message.created_at > now-timedelta(hours=int(self.policies.get("outreach_cooldown_hours")))))
            if recent:
                return SendOutcome(SendStatus.BLOCKED_BUDGET, reason="outreach cooldown reached")

        if purpose in ASK_PURPOSES and volunteer is not None:
            if self._asks_this_month(volunteer.id, local_now) >= self.policies.ask_budget():
                return SendOutcome(
                    SendStatus.BLOCKED_BUDGET,
                    reason="monthly ask budget reached",
                )

        if not needs_confirmation and self.gloo is not None and kind == "template" and purpose in {"clarify", "clarify_shift", "thanks", "cancellation_ack", "confirmation", "filled_thanks", "admin_reply"}:
            from app.core.signup_responder import compose_signup_reply
            body = compose_signup_reply(self.session, self.clock, self.gloo, body, (body,),
                                        volunteer=volunteer, phone=to_phone)
            kind = "ai"
        if outreach:
            now = offers.decision_time(self.session, self.clock)
            if hasattr(self.provider, "allows"):
                offer_meta.state = "offer_queued"
            else:
                error = offers.dispatch(self.session, outreach, None, now, exact=_confirmation is not None)
                if error:
                    if _confirmation and "fresh exact review" in error:
                        _confirmation.status = "expired"
                        payload = {k:v for k,v in _confirmation.payload.items()
                                   if k not in {"message_id", "content_hash", "expires_at"}}
                        confirmations.stage_text(self, {**payload, "body": offer_meta.body})
                    return SendOutcome(SendStatus.BLOCKED_ELIGIBILITY, reason=error)
            body = offer_meta.body
        if len(body) > 1600:
            return SendOutcome(SendStatus.BLOCKED_ELIGIBILITY, reason="invitation and reply deadline exceed text limit")
        if problem := outbound_style_problem(body):
            return SendOutcome(SendStatus.BLOCKED_STYLE, reason=problem)
        if error := conversation_policy.problem(self.session, purpose=purpose, volunteer=volunteer,
                phone=to_phone, body=body, now=now, meta=conversation_meta, approval=_confirmation):
            conversation_policy.record_suppression(self.session, to_phone, purpose, body, now, error)
            return SendOutcome(SendStatus.BLOCKED_POLICY, reason=error)
        reservations = []
        from sqlalchemy.exc import IntegrityError
        try:
            with self.session.begin_nested():
                for key in conversation_meta.get('keys', []):
                    existing = self.session.get(m.Notification,key)
                    if existing is not None:
                        from app.integrations.google_voice_presend_review import original_reservation_allowed
                        if original_reservation_allowed(self.session,_confirmation,existing):
                            # Preserve the old reservation and claim; the sole
                            # newly reviewed successor gets its own dedupe key.
                            key = f'{key}:presend:{_confirmation.id}'
                    reservation = m.Notification(key=key, volunteer_id=volunteer.id if volunteer else None,
                        purpose='conversation_delivery', body='', state='reserved', due_at=now, created_at=now,
                        detail=conversation_meta)
                    self.session.add(reservation)
                    reservations.append(reservation)
                self.session.flush()
        except IntegrityError:
            return SendOutcome(SendStatus.BLOCKED_POLICY, reason='This conversation notification is already reserved')
        try:
            sid = self.provider.send(to_phone, body)
        except Exception:
            if not outreach:
                raise
            offer_meta.state = "offer_uncertain"
            fill.state, fill.next_action_at = "escalated", None
            offers.task_once(self.session, fill, now, "Offer delivery is uncertain; reconcile transport before retrying or advancing.")
            return SendOutcome(SendStatus.BLOCKED_TRANSPORT, reason="uncertain transport; no automatic retry")
        message = m.Message(
            direction="out",
            volunteer_id=volunteer.id if volunteer else None,
            phone=to_phone,
            body=body,
            kind=kind,
            purpose=purpose,
            provider_sid=sid,
            status="queued" if sid.startswith("MAC") or session_transport(self.provider) else "sent",
            created_at=now,
        )
        self.session.add(message)
        self.session.flush()
        if purpose in {'stop_confirm', 'start_confirm'}:
            source = self.session.get(m.Notification, conversation_meta['control_key'])
            source.message_id, source.state = message.id, 'sent'
        for reservation in reservations:
            reservation.message_id, reservation.state = message.id, 'queued' if sid.startswith('MAC') else 'sent'
        self.session.add(m.Notification(key=f'conversation-message:{message.id}', volunteer_id=message.volunteer_id,
            purpose='conversation_source', body='', state='recorded', due_at=now, created_at=now,
            message_id=message.id, detail=conversation_meta))
        self.session.flush()
        if outreach:
            offer_meta.message_id = message.id
        if self._immediate_reply(to_phone, purpose, now):
            self.session.add(m.Notification(key=f"reply-proof:{message.id}", volunteer_id=message.volunteer_id,
                purpose=purpose, body="", state="sent", due_at=now, created_at=now,
                message_id=message.id, detail={"reply_to_message_id": self.reply_to_message_id}))
            self.session.flush()
        return SendOutcome(SendStatus.SENT, message_id=message.id)

    def _immediate_reply(self, phone, purpose, now):
        if purpose not in {"signup_reply", "clarify", "clarify_shift", "thanks", "confirmation", "filled_thanks", "cancellation_ack"}:
            return False
        incoming = self.session.execute(select(m.Message.direction, m.Message.phone, m.Message.created_at).where(m.Message.id == self.reply_to_message_id)).first() if self.reply_to_message_id else None
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
                m.Message.status.not_in(UNSENT_STATUSES),
                m.Message.created_at >= month_start,
            )
        )


def handle_stop_start(
    session: Session, clock: Clock, provider: SMSProvider, volunteer: m.Volunteer, body: str
) -> str | None:
    """Process STOP/START keywords. Returns 'stop', 'start', or None.

    One recorded control acknowledgement may pass opt-out checks; repeat STOPs stay silent.
    """
    from app.core.consent_controls import control_action
    action = control_action(body)
    policies = PolicyStore(session)
    from app.core.confirmations import authorize_sender_fields

    if action == 'stop':
        authorize_sender_fields(session, volunteer, {"sms_opt_in", "preferences"})
        confirm = volunteer.sms_opt_in  # confirm once; repeat STOPs get silence
        volunteer.sms_opt_in = False
        from app.core.confirmations import suppress_phone
        key = 'sms_opt_out:' + volunteer.phone
        suppression = session.get(m.Policy, key)
        if suppression is None:
            session.add(m.Policy(key=key, value={'value': True}))
        else:
            suppression.value = {**suppression.value, 'value': True}
        suppress_phone(session, volunteer.phone)
        for ack in session.scalars(select(m.Notification).where(m.Notification.volunteer_id == volunteer.id,
                m.Notification.key.startswith('control:'), m.Notification.purpose == 'start_confirm',
                m.Notification.state == 'pending')):
            ack.state = 'expired'
        if volunteer.preferences.get("consent_pending"):
            volunteer.preferences = {**volunteer.preferences, "consent_pending": False}
        if confirm:
            _send_direct(session, clock, provider, volunteer, templates.stop_confirm(policies.church_name()), "stop_confirm")
        return "stop"

    if action == 'start':
        from app.core.consent_controls import prior_disclosed_consent
        if not prior_disclosed_consent(session, volunteer):
            key = f'consent_restart_review:{volunteer.id}'
            if session.get(m.Policy, key) is None:
                session.add(m.Policy(key=key, value={'state': 'held',
                    'reason': 'Previous delivered disclosure and actual affirmative consent reply are not verified',
                    'at': clock.now().isoformat()}))
            return 'consent_required'
        authorize_sender_fields(session, volunteer, {"sms_opt_in"})
        suppression = session.get(m.Policy, 'sms_opt_out:' + volunteer.phone)
        if suppression is not None:
            session.delete(suppression)
        for ack in session.scalars(select(m.Notification).where(m.Notification.volunteer_id == volunteer.id,
                m.Notification.key.startswith('control:'), m.Notification.purpose == 'stop_confirm',
                m.Notification.state == 'pending')):
            ack.state = 'expired'
        confirm = not volunteer.sms_opt_in
        volunteer.sms_opt_in = True
        if confirm:
            _send_direct(session, clock, provider, volunteer, templates.start_confirm(policies.church_name()), "start_confirm")
        return "start"

    return None


def _send_direct(session, clock, provider, volunteer, body, purpose) -> None:
    """Commit controls without model/transport latency; Gloo composes queued ack."""
    validate_outbound_style(body)
    now = clock.now()
    key = f'control:{purpose}:{volunteer.id}:{now.isoformat()}'
    if session.get(m.Notification, key) is None:
        session.add(m.Notification(key=key, volunteer_id=volunteer.id, body=body,
            purpose=purpose, state='pending', due_at=now, created_at=now))
    session.flush()


def cloud_demo_pending(provider, session, phone):
    if transport_name(provider) != "google_voice" or not provider.settings.google_voice_demo_mode:
        return False
    from app.integrations.google_voice_demo import RECIPIENT_KEY
    registration = session.get(m.Policy, RECIPIENT_KEY + phone)
    return bool(registration and registration.value.get("consent_state") == "awaiting_name")
