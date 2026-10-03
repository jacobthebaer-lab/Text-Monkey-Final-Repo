"""Committed cloud SMS outbox. Ambiguous submissions are never resent.

One deployed process owns the scheduler. Database locks/conditional claims
also prevent concurrent senders from taking the same reservation.
"""
import hashlib
import json
import logging
from datetime import timedelta

from sqlalchemy import select, update
from app.db import models as m
from app.core import confirmations, offer_windows as offers
from app.core.policies import PolicyStore, in_quiet_hours
from app.core.send_gate import has_open_sensitive_escalation

logger = logging.getLogger("cloud_sms")
PROGRESS = {"accepted": 0, "scheduled": 0, "queued": 0, "sending": 1, "sent": 2,
            "delivered": 3, "read": 4, "failed": 3, "undelivered": 3, "canceled": 3}
TERMINAL = {"delivered", "read", "failed", "undelivered", "canceled"}


def content_hash(message):
    return hashlib.sha256(json.dumps([message.phone, message.body, message.purpose],
                                     ensure_ascii=False).encode()).hexdigest()


def delivery_problem(session, state, message, receipt, now):
    if receipt.expires_at and now >= receipt.expires_at:
        return "expired"
    if receipt.detail.get("content_hash") != content_hash(message) or message.kind != "ai":
        return "blocked_confirmation"
    approval = confirmations.proof_for(session, message)
    if confirmations.enabled(session) and (not approval or confirmations.delivery_problem(
            session, state.provider, approval, now, message)):
        return "blocked_confirmation"
    v = session.get(m.Volunteer, message.volunteer_id) if message.volunteer_id else None
    stop = session.get(m.Policy, "sms_opt_out:" + message.phone)
    signup = v and message.purpose == "signup_reply" and v.preferences.get("signup_source") == "sms" and v.preferences.get("consent_pending") is True
    if v and v.phone != message.phone:
        return "blocked_opt_out"
    if message.purpose != "stop_confirm" and ((stop and stop.value.get("value")) or
            (v and not v.sms_opt_in and not signup)):
        return "blocked_opt_out"
    if v and v.status != "active":
        return "blocked_eligibility"
    if v and has_open_sensitive_escalation(session, v.id):
        return "blocked_sensitive"
    for hold in session.scalars(select(m.Escalation.related_ids).where(
            m.Escalation.category == "sensitive", m.Escalation.status.in_(("open", "acknowledged")))):
        if hold.get("phone") == message.phone:
            return "blocked_sensitive"
    for notification in session.scalars(select(m.Notification).where(m.Notification.message_id == message.id)):
        from app.core.notifications import pre_event_delivery_problem
        if pre_event_delivery_problem(session, notification, now):
            return "superseded"
    policies = PolicyStore(session)
    start, end = policies.urgent_quiet_hours() if receipt.detail.get("urgent") else policies.quiet_hours()
    proof = session.get(m.Notification, f"reply-proof:{message.id}")
    incoming = session.get(m.Message, proof.detail.get("reply_to_message_id")) if proof else None
    immediate = incoming and incoming.direction == "in" and incoming.phone == message.phone and timedelta(0) <= now - incoming.created_at <= timedelta(minutes=10)
    if message.purpose != "stop_confirm" and not immediate and in_quiet_hours(now.astimezone(policies.church_tz()), start, end):
        return "quiet"
    if message.purpose == "outreach":
        from app.core import eligibility
        outreach = session.scalar(select(m.Outreach).where(m.Outreach.message_id == message.id))
        fill = session.get(m.FillRequest, outreach.fill_request_id) if outreach else None
        shift = session.get(m.Shift, fill.shift_id) if fill else None
        if not shift or not v or not eligibility.check(session, v, shift, tz=policies.get("church_timezone")):
            return "superseded"
        # Deferred offers get their actual dispatch deadline here. A changed
        # reviewed body is held for a fresh review instead of silently rewritten.
        if offers.dispatch(session, outreach, message, now, exact=True):
            return "blocked_confirmation"
    return None


def record_status(session, message, receipt, sid, status, now, error_code=None):
    if status not in PROGRESS:
        return
    previous = receipt.detail.get("provider_status")
    if previous in TERMINAL or (previous in PROGRESS and PROGRESS[status] < PROGRESS[previous]):
        return
    message.provider_sid = sid
    message.status = "submitted" if status in {"accepted", "scheduled", "queued", "sending"} else status
    receipt.state = message.status
    if status in {"failed", "undelivered", "canceled"}:
        hold_offer(session, message, now)
    receipt.detail = {**receipt.detail, "provider_sid": sid, "provider_status": status,
                      "status_at": now.isoformat(), "error_code": error_code}


def hold_offer(session, message, now):
    if message.purpose != "outreach":
        return
    outreach = session.scalar(select(m.Outreach).where(m.Outreach.message_id == message.id))
    if outreach:
        meta = offers.metadata(session, outreach)
        if meta:
            meta.state = "offer_uncertain"
        fill = session.get(m.FillRequest, outreach.fill_request_id)
        fill.state, fill.next_action_at = "escalated", None
        offers.task_once(session, fill, now, "Cloud SMS delivery needs reconciliation before retrying or advancing.")


def dispatch_pending(state, limit=25):
    if getattr(state.provider, "transport", None) != "twilio" or state.settings.demo_mode:
        return
    with state.session_factory() as session:
        ids = session.scalars(select(m.Notification.message_id).where(
            m.Notification.purpose == "cloud_transport", m.Notification.state == "queued")
            .order_by(m.Notification.created_at).limit(limit)).all()
    for message_id in ids:
        now = state.clock.now()
        with state.session_factory() as session:
            offers.begin_decision(session)
            receipt = session.scalar(select(m.Notification).where(m.Notification.key == f"cloud-sms:{message_id}")
                                     .with_for_update(skip_locked=True))
            message = session.get(m.Message, message_id)
            if not receipt or receipt.state != "queued" or not message:
                continue
            if message.status != "queued":
                receipt.state = message.status
                session.commit()
                continue
            problem = delivery_problem(session, state, message, receipt, now)
            if problem == "quiet":
                session.commit()
                continue
            if problem:
                message.status = receipt.state = problem
                session.commit()
                continue
            claimed = session.execute(update(m.Message).where(m.Message.id == message_id, m.Message.status == "queued")
                                      .values(status="dispatching")).rowcount
            if not claimed:
                session.rollback()
                continue
            receipt.state = "dispatching"
            receipt.detail = {**receipt.detail, "attempted_at": now.isoformat()}
            phone, body = message.phone, message.body
            # Persist BEFORE calling the carrier. A crash here cannot create
            # a second automatic submission on restart.
            session.commit()
        try:
            result = state.provider.submit(message_id, phone, body)
        except Exception:
            # Even a timeout may have created an SMS; never blindly retry.
            with state.session_factory() as session:
                message = session.get(m.Message, message_id)
                receipt = session.get(m.Notification, f"cloud-sms:{message_id}")
                if receipt.state == "dispatching":
                    message.status = receipt.state = "uncertain"
                    hold_offer(session, message, state.clock.now())
                session.commit()
            logger.warning("Cloud SMS submission needs reconciliation: message %s", message_id)
            continue
        with state.session_factory() as session:
            message = session.scalar(select(m.Message).where(m.Message.id == message_id).with_for_update())
            receipt = session.scalar(select(m.Notification).where(m.Notification.key == f"cloud-sms:{message_id}").with_for_update())
            # A callback can arrive before the REST response; never regress it.
            record_status(session, message, receipt, result.sid, result.status or "queued", state.clock.now())
            session.commit()


def reconcile_interrupted(state):
    """A committed dispatch left by a restarted process is ambiguous, not retryable."""
    with state.session_factory() as session:
        for receipt in session.scalars(select(m.Notification).where(
                m.Notification.purpose == "cloud_transport", m.Notification.state == "dispatching")):
            message = session.get(m.Message, receipt.message_id)
            if message and message.status == "dispatching":
                message.status = receipt.state = "uncertain"
                hold_offer(session, message, state.clock.now())
        # Incoming processing performs no external send before commit. Its
        # uncommitted work rolled back on process exit, so this retry is safe.
        for receipt in session.scalars(select(m.Notification).where(
                m.Notification.purpose == "cloud_inbound", m.Notification.state == "processing")):
            receipt.state = "queued"
        session.commit()


def process_inbound(state, limit=5):
    from functools import partial
    from app.agents.fill_agent import FillContext
    from app.core.inbound import handle_inbound
    from app.llm.parser import parse_inbound
    with state.session_factory() as session:
        keys = session.scalars(select(m.Notification.key).where(
            m.Notification.purpose == "cloud_inbound", m.Notification.state == "queued",
            m.Notification.due_at <= state.clock.now()).order_by(m.Notification.created_at).limit(limit)).all()
    for key in keys:
        with state.session_factory() as session:
            claimed = session.execute(update(m.Notification).where(m.Notification.key == key,
                                      m.Notification.state == "queued").values(state="processing")).rowcount
            session.commit()
            if not claimed:
                continue
        try:
            with state.session_factory() as session:
                receipt = session.get(m.Notification, key)
                session.info["twilio_inbound_sid"] = receipt.detail["provider_sid"]
                result = handle_inbound(session, state.clock, state.provider, receipt.detail["phone"], receipt.body,
                    partial(parse_inbound, state.gloo), ctx=FillContext(session, state.clock, state.provider, state.gloo),
                    allow_signup=state.settings.allow_text_signup)
                receipt.state = "processed"
                receipt.detail = {**receipt.detail, "route": result.routed_to}
                session.commit()
        except Exception:
            # No provider call occurred: outbound reservations rolled back too.
            with state.session_factory() as session:
                receipt = session.get(m.Notification, key)
                attempts = receipt.detail.get("attempts", 0) + 1
                receipt.detail = {**receipt.detail, "attempts": attempts}
                receipt.state = "blocked" if attempts >= 3 else "queued"
                receipt.due_at = state.clock.now() + timedelta(minutes=2)
                if receipt.state == "blocked":
                    session.add(m.Escalation(category="operational", severity="normal", status="open",
                        summary="An incoming cloud text needs review after processing failed. Check Gloo and the worker before retrying.",
                        related_ids={"transport": "twilio", "phone": receipt.detail["phone"], "receipt_key": key},
                        created_at=state.clock.now()))
                session.commit()
            logger.warning("Cloud inbound processing held: receipt %s", key)


def tick(state):
    process_inbound(state)
    dispatch_pending(state)
