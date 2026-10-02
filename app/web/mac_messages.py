"""Authenticated, allowlisted Messages ingress and durable outbound claims.

Run one backend worker for the SQLite demo. There is deliberately no lease
expiry/re-send: a crash around AppleScript delivery has an uncertain outcome.
"""

import hashlib
import secrets
import threading
import time
from datetime import timedelta
from functools import partial
from typing import Literal

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel, Field
from sqlalchemy import case, event, select, or_, update
from sqlalchemy.exc import IntegrityError

from app.agents.fill_agent import FillContext
from app.core.inbound import handle_inbound
from app.core.policies import PolicyStore, in_quiet_hours
from app.core.send_gate import has_open_sensitive_escalation, BLOCKING_ESCALATION_STATUSES
from app.db import models as m
from app.integrations.mac_models import MacDeliveryClaim, MacInboundReceipt
from app.llm.parser import parse_inbound
from app.sms.mac_provider import MacMessagesProvider
from app.integrations.test_sessions import permitted
from app.core.conversation import scope

claim_lock = threading.Lock()


def authorized(request: Request):
    s = request.app.state.settings
    if not s.mac_bridge_enabled or not isinstance(request.app.state.provider, MacMessagesProvider):
        raise HTTPException(503, "Mac connector is inactive")
    if not secrets.compare_digest(request.headers.get("Authorization", ""), "Bearer " + s.mac_bridge_token):
        raise HTTPException(401, "Invalid Mac connector credential")


router = APIRouter(prefix="/mac", dependencies=[Depends(authorized)])


class Incoming(BaseModel):
    guid: str = Field(min_length=1, max_length=128, pattern=r"^[A-Za-z0-9:._-]+$")
    phone: str = Field(pattern=r"^\+[1-9][0-9]{7,14}$")
    body: str = Field(min_length=1, max_length=1600)
    service: Literal["iMessage", "SMS"] = "iMessage"
    session_id: str = Field(default="", max_length=32)


class Acknowledgment(BaseModel):
    token: str = Field(min_length=32, max_length=64)
    outcome: Literal["submitted", "uncertain"]


@router.post("/inbound")
def inbound(data: Incoming, request: Request):
    state = request.app.state
    if (data.phone not in state.provider.phones or data.service not in state.provider.services
            or not data.body.strip()):
        raise HTTPException(403, "Message is outside the configured demo")
    selected = state.provider.test_sessions.get(data.phone)
    if not permitted(selected, data.session_id, data.body, state.mac_delivery_clock.now()):
        raise HTTPException(403, "Message needs a matching active test session")
    state.mac_last_poll = time.monotonic()
    fingerprint = hashlib.sha256((data.phone + "\0" + data.service + "\0" + data.session_id + "\0" + data.body).encode()).hexdigest()
    with state.session_factory() as session:
        session.info["mac_test_session"] = selected
        receipt = session.get(MacInboundReceipt, data.guid)
        if receipt:
            if receipt.fingerprint != fingerprint:
                raise HTTPException(409, "Message ID was reused with different content")
            return {**receipt.result, "duplicate": True}
        receipt = MacInboundReceipt(guid=data.guid, fingerprint=fingerprint, result={})
        session.add(receipt)
        try:
            session.flush()  # unique GUID lock before any agent action
        except IntegrityError:
            session.rollback()
            receipt = session.get(MacInboundReceipt, data.guid)
            if receipt is None or receipt.fingerprint != fingerprint:
                raise HTTPException(409, "Message ID conflict")
            return {**receipt.result, "duplicate": True}
        ctx = FillContext(session, state.clock, state.provider, state.gloo)
        def mark_origin(session, flush_context, instances):
            # Only objects created by this transaction, including signup review.
            # Other requests' simulator approvals cannot acquire a live origin.
            for approval in session.new:
                if isinstance(approval, m.Escalation):
                    approval.related_ids = {**approval.related_ids, "transport":"mac_messages", "phone":data.phone, "session_id":selected.id}
                if isinstance(approval, m.Approval) and approval.kind not in {"confirm_text", "confirm_record"}:
                    approval.payload = {**(approval.payload or {}), "transport": "mac_messages"}
        event.listen(session, "before_flush", mark_origin)
        try:
            result = handle_inbound(
                session, state.clock, state.provider, data.phone, data.body,
                partial(parse_inbound, state.gloo), ctx=ctx,
                allow_signup=state.settings.allow_text_signup,
            )
            session.flush()
        finally:
            event.remove(session, "before_flush", mark_origin)
        receipt.result = {"intent": result.routed_to, "notes": result.notes, "session_id": selected.id}
        session.commit()  # receipt + business changes + outbound rows atomically
        return {**receipt.result, "duplicate": False}


@router.get("/test-history")
def test_history(request: Request, phone: str, session_id: str, limit: int = 50):
    state = request.app.state
    selected = state.provider.test_sessions.get(phone)
    if not permitted(selected, session_id, "", state.mac_delivery_clock.now()):
        raise HTTPException(403, "Select an active test session for this phone")
    if not 1 <= limit <= 100:
        raise HTTPException(400, "History limit must be between 1 and 100")
    with state.session_factory() as session:
        rows = session.scalars(scope(select(m.Message), selected).where(
            m.Message.phone == phone, m.Message.created_at >= selected.starts_at,
            m.Message.created_at < selected.expires_at,
        ).order_by(m.Message.id.desc()).limit(limit)).all()
        return {"session_id": selected.id, "messages": [{"id": r.id, "direction": r.direction,
                "body": r.body, "status": r.status, "created_at": r.created_at.isoformat()}
                for r in reversed(rows)]}


@router.post("/outbound/pull")
def pull(request: Request):
    state = request.app.state
    state.mac_last_poll = time.monotonic()
    with claim_lock, state.session_factory() as session:
        policies = PolicyStore(session)
        now = state.mac_delivery_clock.now().astimezone(policies.church_tz())
        conditions = [(m.Message.phone == phone) & m.Message.provider_sid.startswith(selected.outbound_prefix) &
                      (m.Message.created_at >= selected.starts_at) & (m.Message.created_at < selected.expires_at)
                      for phone, selected in state.provider.test_sessions.items() if selected.active(now)]
        active_origins = or_(*conditions) if conditions else False
        # Mark only metadata for ineligible queue rows. Never load their bodies.
        session.execute(update(m.Message).where(m.Message.direction == "out", m.Message.status == "queued",
            m.Message.provider_sid.startswith("MAC"), ~active_origins if active_origins is not False else True)
            .values(status="blocked_test_session"))
        rows = session.scalars(select(m.Message).where(
            m.Message.direction == "out", m.Message.status == "queued",
            m.Message.provider_sid.startswith("MAC"),
            active_origins,
        ).order_by(case((m.Message.purpose.in_(["stop_confirm", "start_confirm"]), 0), else_=1), m.Message.id)
          .limit(50).with_for_update(skip_locked=True)).all()
        batch = []
        for row in rows:
            selected = state.provider.test_sessions.get(row.phone)
            if selected is None or not selected.active(now) or not row.provider_sid.startswith(selected.outbound_prefix):
                row.status = "blocked_test_session"
                continue
            from app.core import confirmations
            approval = confirmations.proof_for(session, row) if confirmations.enabled(session) else None
            if confirmations.enabled(session) and (approval is None or confirmations.delivery_problem(session, state.provider, approval, now, row)):
                row.status = "blocked_confirmation"
                continue
            volunteer = (session.get(m.Volunteer, row.volunteer_id) if row.volunteer_id else
                         session.scalar(select(m.Volunteer).where(m.Volunteer.phone == row.phone)))
            if row.phone not in state.provider.phones:
                row.status = "blocked_allowlist"
                continue
            opted_out = session.get(m.Policy, "sms_opt_out:" + row.phone)
            if row.purpose != "stop_confirm" and opted_out and opted_out.value.get("value"):
                row.status = "blocked_opt_out"
                continue
            phone_holds = session.scalars(select(m.Escalation.related_ids).where(
                m.Escalation.category == "sensitive",
                m.Escalation.status.in_(BLOCKING_ESCALATION_STATUSES),
            ))
            if any(hold.get("phone") == row.phone for hold in phone_holds):
                row.status = "blocked_sensitive"
                continue
            signup_reply = (volunteer and row.purpose == "signup_reply"
                            and volunteer.preferences.get("signup_source") == "sms"
                            and volunteer.preferences.get("consent_pending") is True)
            if volunteer and row.purpose != "stop_confirm" and not volunteer.sms_opt_in and not signup_reply:
                row.status = "blocked_opt_out"
                continue
            if volunteer and has_open_sensitive_escalation(session, volunteer.id):
                row.status = "blocked_sensitive"
                continue
            if row.purpose == "outreach":
                outreach = session.scalar(select(m.Outreach).where(m.Outreach.message_id == row.id))
                fill = session.get(m.FillRequest, outreach.fill_request_id) if outreach else None
                shift = session.get(m.Shift, fill.shift_id) if fill else None
                if fill and (fill.state not in (("in_progress", "escalated", "waiting_approval") if confirmations.enabled(session) else ("in_progress", "escalated")) or shift.event.starts_at <= now
                             or shift.event.status in ("cancelled", "completed")):
                    row.status = "superseded"
                    continue
            proof = session.get(m.Notification, f"reply-proof:{row.id}")
            incoming_id = proof.detail.get("reply_to_message_id") if proof else None
            incoming = session.execute(select(m.Message.direction, m.Message.phone, m.Message.created_at).where(m.Message.id == incoming_id)).first() if incoming_id else None
            direct_reply = bool(incoming and incoming.direction == "in" and incoming.phone == row.phone
                                and timedelta(0) <= now-incoming.created_at <= timedelta(minutes=10))
            # Check real delivery time; urgent requests use the tighter hard
            # envelope here because the existing Message row doesn't store urgency.
            start, end = policies.quiet_hours()
            if row.purpose == "outreach" and shift and shift.event.starts_at-now < timedelta(hours=24):
                start, end = policies.urgent_quiet_hours()
            notification = session.scalar(select(m.Notification).where(m.Notification.message_id == row.id))
            if notification and notification.detail.get("urgent"):
                start, end = policies.urgent_quiet_hours()
            test_reply = state.provider.allows_test_signup_reply(row.phone, row.purpose, now)
            if row.purpose not in {"stop_confirm", "start_confirm"} and in_quiet_hours(now, start, end) and not test_reply and not direct_reply:
                continue
            token = secrets.token_hex(32)
            session.add(MacDeliveryClaim(message_id=row.id, token=token))
            row.status = "dispatching"
            batch.append({"id": row.id, "token": token, "phone": row.phone, "body": row.body,
                          "session_id": selected.id,
                          **({"confirmation_required": True, "content_hash": approval.payload["content_hash"],
                              "approval_expires_at": approval.payload["expires_at"]} if approval else {})})
        session.commit()
        return {"messages": batch}


@router.post("/outbound/{message_id}/ack")
def ack(message_id: int, data: Acknowledgment, request: Request):
    with request.app.state.session_factory() as session:
        claim = session.get(MacDeliveryClaim, message_id)
        row = session.get(m.Message, message_id)
        if not claim or not row or not secrets.compare_digest(claim.token, data.token):
            raise HTTPException(409, "Invalid delivery claim")
        if row.status in {"submitted", "uncertain"}:
            if row.status != data.outcome:
                raise HTTPException(409, "Delivery already acknowledged differently")
            return {"status": row.status}
        row.status = data.outcome
        session.commit()
        return {"status": row.status}


class ClaimCheck(BaseModel):
    token: str = Field(min_length=32, max_length=64)
    content_hash: str = Field(min_length=64, max_length=64)


@router.post("/outbound/{message_id}/verify")
def verify_claim(message_id: int, data: ClaimCheck, request: Request):
    from app.core import confirmations
    from app.core.send_gate import SendGate
    state = request.app.state
    with claim_lock, state.session_factory() as session:
        row = session.get(m.Message, message_id)
        claim = session.get(MacDeliveryClaim, message_id)
        if not confirmations.enabled(session) or not row or row.status != "dispatching" or not claim or not secrets.compare_digest(claim.token, data.token):
            raise HTTPException(409, "Delivery claim is no longer valid")
        now = state.mac_delivery_clock.now()
        approval = confirmations.proof_for(session, row)
        error = (confirmations.delivery_problem(session, state.provider, approval, now, row)
                 if approval and approval.payload.get("content_hash") == data.content_hash else "Exact confirmation is missing")
        gate = SendGate(session, state.mac_delivery_clock, state.provider)
        if approval:
            gate.reply_to_message_id = approval.payload.get("reply_to_message_id")
        policies = gate.policies
        start, end = policies.urgent_quiet_hours() if approval and approval.payload.get("urgent") else policies.quiet_hours()
        if (not error and row.purpose != "stop_confirm" and in_quiet_hours(now.astimezone(policies.church_tz()), start, end) and
            not gate._immediate_reply(row.phone, row.purpose, now) and not state.provider.allows_test_signup_reply(row.phone, row.purpose, now)):
            error = "Sending hours changed"
        if error:
            row.status = "blocked_confirmation"
            session.commit()
            raise HTTPException(409, error)
        return {"verified": True, "phone": row.phone, "body": row.body, "content_hash": approval.payload["content_hash"],
                "approval_expires_at": approval.payload["expires_at"]}
