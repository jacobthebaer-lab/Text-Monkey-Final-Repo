"""Authenticated, allowlisted iMessage ingress and durable outbound claims.

Run one backend worker for the SQLite demo. There is deliberately no lease
expiry/re-send: a crash around AppleScript delivery has an uncertain outcome.
"""

import hashlib
import secrets
import threading
import time
from functools import partial
from typing import Literal

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel, Field
from sqlalchemy import case, event, select
from sqlalchemy.exc import IntegrityError

from app.agents.fill_agent import FillContext
from app.core.inbound import handle_inbound
from app.core.policies import PolicyStore, in_quiet_hours
from app.core.send_gate import has_open_sensitive_escalation, BLOCKING_ESCALATION_STATUSES
from app.db import models as m
from app.integrations.mac_models import MacDeliveryClaim, MacInboundReceipt
from app.llm.parser import parse_inbound
from app.sms.mac_provider import MacMessagesProvider

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
    service: Literal["iMessage"] = "iMessage"


class Acknowledgment(BaseModel):
    token: str = Field(min_length=32, max_length=64)
    outcome: Literal["submitted", "uncertain"]


@router.post("/inbound")
def inbound(data: Incoming, request: Request):
    state = request.app.state
    if data.phone not in state.provider.phones or not data.body.strip():
        raise HTTPException(403, "Message is outside the configured demo")
    state.mac_last_poll = time.monotonic()
    fingerprint = hashlib.sha256((data.phone + "\0" + data.service + "\0" + data.body).encode()).hexdigest()
    with state.session_factory() as session:
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
                if isinstance(approval, m.Approval):
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
        receipt.result = {"intent": result.routed_to, "notes": result.notes}
        session.commit()  # receipt + business changes + outbound rows atomically
        return {**receipt.result, "duplicate": False}


@router.post("/outbound/pull")
def pull(request: Request):
    state = request.app.state
    state.mac_last_poll = time.monotonic()
    with claim_lock, state.session_factory() as session:
        rows = session.scalars(select(m.Message).where(
            m.Message.direction == "out", m.Message.status == "queued",
            m.Message.provider_sid.startswith("MAC"),
        ).order_by(case((m.Message.purpose.in_(["stop_confirm", "start_confirm"]), 0), else_=1), m.Message.id)
          .limit(50).with_for_update(skip_locked=True)).all()
        batch = []
        policies = PolicyStore(session)
        now = state.mac_delivery_clock.now().astimezone(policies.church_tz())
        for row in rows:
            volunteer = (session.get(m.Volunteer, row.volunteer_id) if row.volunteer_id else
                         session.scalar(select(m.Volunteer).where(m.Volunteer.phone == row.phone)))
            if row.phone not in state.provider.phones:
                row.status = "blocked_allowlist"
                continue
            opted_out = session.get(m.Policy, "sms_opt_out:" + row.phone)
            if row.purpose != "stop_confirm" and opted_out and opted_out.value.get("value"):
                row.status = "blocked_opt_out"
                continue
            phone_holds = session.scalars(select(m.Escalation).where(
                m.Escalation.category == "sensitive",
                m.Escalation.status.in_(BLOCKING_ESCALATION_STATUSES),
            ))
            if any(hold.related_ids.get("phone") == row.phone for hold in phone_holds):
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
            # Check real delivery time; urgent requests use the tighter hard
            # envelope here because the existing Message row doesn't store urgency.
            start, end = policies.quiet_hours()
            test_reply = state.provider.allows_test_signup_reply(row.phone, row.purpose, now)
            if row.purpose not in {"stop_confirm", "start_confirm"} and in_quiet_hours(now, start, end) and not test_reply:
                continue
            token = secrets.token_hex(32)
            session.add(MacDeliveryClaim(message_id=row.id, token=token))
            row.status = "dispatching"
            batch.append({"id": row.id, "token": token, "phone": row.phone, "body": row.body})
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
