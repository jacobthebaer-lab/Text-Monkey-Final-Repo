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
from app.core.message_style import outbound_style_problem

claim_lock = threading.Lock()


def exact_review_required(session, row):
    from app.core import confirmations
    # A durable review receipt remains mandatory even when its approval is
    # missing or changed. Never downgrade an invalid proof to an ordinary send.
    return (confirmations.enabled(session) or
            session.get(m.Notification, f"confirmation:{row.id}") is not None)


def final_delivery_problem(session, state, row, now, approval=None):
    """Mutable transport and recipient guards apply to every native claim."""
    provider = state.provider
    selected = provider.test_sessions.get(row.phone)
    if row.phone not in provider.phones:
        return 'blocked_allowlist', 'Recipient is no longer in the configured allowlist'
    if (selected is None or not selected.active(now) or not row.provider_sid
            or not row.provider_sid.startswith(selected.outbound_prefix)):
        return 'blocked_test_session', 'Selected transport session expired or changed'
    # Ingress scopes intake dedupe to this exact session. Fresh delivery sessions
    # must reconstruct the same validated scope, never invent a new one.
    session.info['mac_test_session'] = selected
    volunteer = session.get(m.Volunteer, row.volunteer_id) if row.volunteer_id else session.scalar(
        select(m.Volunteer).where(m.Volunteer.phone == row.phone))
    if volunteer and volunteer.phone != row.phone:
        return 'blocked_eligibility', 'Volunteer phone changed before delivery'
    if row.purpose in {'stop_confirm', 'start_confirm'}:
        from app.core.outbound_conversation import queued_problem
        if error := queued_problem(session, row, now, approval):
            return 'blocked_policy', error
    opted_out = session.get(m.Policy, 'sms_opt_out:' + row.phone)
    signup = (volunteer and row.purpose == 'signup_reply'
              and (volunteer.preferences or {}).get('signup_source') == 'sms'
              and (volunteer.preferences or {}).get('consent_pending') is True)
    if row.purpose != 'stop_confirm' and ((opted_out and opted_out.value.get('value'))
            or (volunteer and not volunteer.sms_opt_in and not signup)):
        return 'blocked_opt_out', 'Recipient stopped texts or no longer consents'
    holds = session.scalars(select(m.Escalation.related_ids).where(
        m.Escalation.category == 'sensitive', m.Escalation.status.in_(BLOCKING_ESCALATION_STATUSES)))
    if any(hold.get('phone') == row.phone or (volunteer and hold.get('volunteer_id') == volunteer.id) for hold in holds):
        return 'blocked_sensitive', 'Recipient needs human follow-up'
    policies = PolicyStore(session)
    notifications = session.scalars(select(m.Notification).where(m.Notification.message_id == row.id)).all()
    urgent = bool(approval and approval.payload.get('urgent')) or any(n.detail.get('urgent') for n in notifications)
    start, end = policies.urgent_quiet_hours() if urgent else policies.quiet_hours()
    proof = session.get(m.Notification, f'reply-proof:{row.id}')
    incoming = session.get(m.Message, proof.detail.get('reply_to_message_id')) if proof else None
    direct_reply = bool(proof and proof.message_id == row.id and incoming and incoming.direction == 'in'
        and incoming.phone == row.phone and timedelta(0) <= now-incoming.created_at <= timedelta(minutes=10))
    test_reply = provider.allows_test_signup_reply(row.phone, row.purpose, now)
    if row.purpose != 'stop_confirm' and in_quiet_hours(now.astimezone(policies.church_tz()), start, end) and not direct_reply and not test_reply:
        return 'blocked_quiet_hours', 'Sending hours changed before native delivery'
    return None


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
        from app.core import profile_sync
        mirror = state.settings.profile_sync_enabled and data.phone in profile_sync.approved_phones(state.settings)
        before_profile = profile_sync.safe_snapshot(session, data.phone) if mirror else None
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
        if mirror:
            queued = profile_sync.capture(session, state.settings, phone=data.phone, guid=data.guid,
                        route=result.routed_to, before=before_profile, effective_at=state.mac_delivery_clock.now())
            receipt.result = {**receipt.result, "profile_sync": queued.state if queued else "unchanged"}
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
          .limit(50)).all()
        batch = []
        from app.core import offer_windows as offers
        for row in rows:
            outreach = session.scalar(select(m.Outreach).where(m.Outreach.message_id == row.id)) if row.purpose == "outreach" else None
            if outreach:
                outreach = offers.lock(session, outreach)
                now = offers.decision_time(session, state.mac_delivery_clock).astimezone(policies.church_tz())
            row = session.scalar(select(m.Message).where(m.Message.id == row.id).with_for_update(skip_locked=True)
                .execution_options(populate_existing=True))
            if row is None or row.status != "queued":
                continue
            if outbound_style_problem(row.body):
                row.status = "blocked_style"
                continue
            selected = state.provider.test_sessions.get(row.phone)
            if selected is None or not selected.active(now) or not row.provider_sid.startswith(selected.outbound_prefix):
                row.status = "blocked_test_session"
                continue
            session.info['mac_test_session'] = selected
            from app.core import confirmations
            exact = exact_review_required(session, row)
            approval = confirmations.proof_for(session, row) if exact else None
            if exact and (approval is None or confirmations.delivery_problem(session, state.provider, approval, now, row)):
                row.status = "blocked_confirmation"
                continue
            volunteer = (session.get(m.Volunteer, row.volunteer_id) if row.volunteer_id else
                         session.scalar(select(m.Volunteer).where(m.Volunteer.phone == row.phone)))
            from app.core import outbound_conversation
            if error := outbound_conversation.queued_problem(session, row, now, approval):
                row.status = 'blocked_policy'
                outbound_conversation.record_suppression(session, row.phone, row.purpose, row.body, now, error)
                continue
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
                if fill and (fill.state not in offers.OPEN_FILLS or shift.event.starts_at <= now
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
            from app.core.notifications import pre_event_delivery_problem
            if pre_event_delivery_problem(session, notification, now):
                row.status = "superseded"
                notification.state = "expired"
                continue
            if notification and notification.detail.get("urgent"):
                start, end = policies.urgent_quiet_hours()
            test_reply = state.provider.allows_test_signup_reply(row.phone, row.purpose, now)
            if row.purpose not in {"stop_confirm", "start_confirm"} and in_quiet_hours(now, start, end) and not test_reply and not direct_reply:
                continue
            if outreach:
                from app.core import eligibility
                if not eligibility.check(session, volunteer, shift, tz=policies.get("church_timezone")):
                    offers.close(session, outreach, "blocked", now)
                    row.status = "superseded"
                    fill.next_action_at = now
                    continue
                error = offers.dispatch(session, outreach, row, now, exact=approval is not None, claim=True)
                if error:
                    row.status = "blocked_confirmation" if approval else "superseded"
                    if approval and "fresh exact review" in error:
                        approval.status = "expired"
                        from app.core.send_gate import SendGate
                        payload = {k:v for k,v in approval.payload.items() if k not in {"message_id", "content_hash", "expires_at"}}
                        confirmations.stage_text(SendGate(session, state.mac_delivery_clock, state.provider),
                            {**payload, "body": offers.metadata(session, outreach).body})
                        outreach.message_id = None
                        offers.metadata(session, outreach).message_id = None
                        fill.state, fill.next_action_at = "waiting_approval", offers.cutoff(session, shift.event.starts_at)
                    continue
            if outbound_style_problem(row.body):
                row.status = "blocked_style"
                continue
            token = secrets.token_hex(32)
            session.add(MacDeliveryClaim(message_id=row.id, token=token))
            row.status = "dispatching"
            batch.append({"id": row.id, "token": token, "phone": row.phone, "body": row.body,
                          "session_id": selected.id,
                          "conversation_preflight_required": True,
                          **({"offer_preflight_required": True} if outreach else {}),
                          **({"confirmation_required": True, "content_hash": approval.payload["content_hash"],
                              "approval_expires_at": approval.payload["expires_at"]} if approval else {})})
        session.commit()
        return {"messages": batch}


@router.post("/outbound/{message_id}/ack")
def ack(message_id: int, data: Acknowledgment, request: Request):
    with request.app.state.session_factory() as session:
        from app.core import offer_windows as offers
        offers.begin_decision(session)
        claim = session.get(MacDeliveryClaim, message_id)
        row = session.get(m.Message, message_id)
        if not claim or not row or not secrets.compare_digest(claim.token, data.token):
            raise HTTPException(409, "Invalid delivery claim")
        if row.status in {"submitted", "uncertain"}:
            if row.status != data.outcome:
                raise HTTPException(409, "Delivery already acknowledged differently")
            return {"status": row.status}
        if row.status != "dispatching":
            raise HTTPException(409, "Delivery is no longer dispatching")
        if row.purpose == "outreach" and data.outcome == "submitted":
            from app.core import offer_windows as offers
            outreach = session.scalar(select(m.Outreach).where(m.Outreach.message_id == row.id))
            meta = offers.metadata(session, outreach) if outreach else None
            if not meta or meta.state != "offer_active":
                raise HTTPException(409, "Offer requires dispatch preflight before submission")
        row.status = data.outcome
        if row.purpose == "outreach":
            from app.core import offer_windows as offers
            outreach = session.scalar(select(m.Outreach).where(m.Outreach.message_id == row.id))
            if outreach and data.outcome == "uncertain":
                meta = offers.metadata(session, outreach)
                if meta:
                    meta.state = "offer_uncertain"
                fill = session.get(m.FillRequest, outreach.fill_request_id)
                fill.state, fill.next_action_at = "escalated", None
                offers.task_once(session, fill, offers.decision_time(session, request.app.state.mac_delivery_clock),
                    "Offer delivery is uncertain; reconcile this delivery claim before retrying or advancing.")
        session.commit()
        return {"status": row.status}


class ClaimCheck(BaseModel):
    token: str = Field(min_length=32, max_length=64)
    content_hash: str | None = Field(default=None, min_length=64, max_length=64)


@router.post("/outbound/{message_id}/verify")
def verify_claim(message_id: int, data: ClaimCheck, request: Request):
    from app.core import confirmations
    from app.core.send_gate import SendGate
    state = request.app.state
    with claim_lock, state.session_factory() as session:
        from app.core import offer_windows as offers
        offers.begin_decision(session)
        row = session.get(m.Message, message_id)
        claim = session.get(MacDeliveryClaim, message_id)
        if not row or row.status != "dispatching" or not claim or not secrets.compare_digest(claim.token, data.token):
            raise HTTPException(409, "Delivery claim is no longer valid")
        if problem := outbound_style_problem(row.body):
            row.status = "blocked_style"
            session.commit()
            raise HTTPException(409, problem)
        now = state.mac_delivery_clock.now()
        exact = exact_review_required(session, row)
        approval = confirmations.proof_for(session, row) if exact else None
        error = ((confirmations.delivery_problem(session, state.provider, approval, now, row)
                  if approval and approval.payload.get("content_hash") == data.content_hash else "Exact confirmation is missing")
                 if exact else None)
        if final_problem := final_delivery_problem(session, state, row, now, approval):
            row.status, reason = final_problem
            session.commit()
            raise HTTPException(409, reason)
        from app.core import outbound_conversation
        conversation_error = outbound_conversation.queued_problem(session, row, now, approval)
        if conversation_error:
            row.status = 'blocked_policy'
            outbound_conversation.record_suppression(session, row.phone, row.purpose, row.body, now, conversation_error)
            session.commit()
            raise HTTPException(409, conversation_error)
        gate = SendGate(session, state.mac_delivery_clock, state.provider)
        if approval:
            gate.reply_to_message_id = approval.payload.get("reply_to_message_id")
        policies = gate.policies
        start, end = policies.urgent_quiet_hours() if approval and approval.payload.get("urgent") else policies.quiet_hours()
        if (exact and not error and row.purpose != "stop_confirm" and in_quiet_hours(now.astimezone(policies.church_tz()), start, end) and
            not gate._immediate_reply(row.phone, row.purpose, now) and not state.provider.allows_test_signup_reply(row.phone, row.purpose, now)):
            error = "Sending hours changed"
        if not error and row.purpose == "outreach":
            from app.core import offer_windows as offers
            from app.core import eligibility
            outreach = session.scalar(select(m.Outreach).where(m.Outreach.message_id == row.id))
            if outreach:
                outreach = offers.lock(session, outreach)
                now = offers.decision_time(session, state.mac_delivery_clock)
                fill = session.get(m.FillRequest, outreach.fill_request_id)
                shift = session.get(m.Shift, fill.shift_id)
                volunteer = session.get(m.Volunteer, outreach.volunteer_id)
                meta = offers.metadata(session, outreach)
                opted_out = session.get(m.Policy, "sms_opt_out:" + row.phone)
                start, end = (policies.urgent_quiet_hours() if shift.event.starts_at-now < timedelta(hours=24)
                              else policies.quiet_hours())
                selected = state.provider.test_sessions.get(row.phone)
                if (selected is None or not selected.active(now) or not row.provider_sid.startswith(selected.outbound_prefix) or
                        row.phone != volunteer.phone or row.phone not in state.provider.phones or
                        in_quiet_hours(now.astimezone(policies.church_tz()), start, end) or
                        not volunteer.sms_opt_in or (opted_out and opted_out.value.get("value")) or
                        has_open_sensitive_escalation(session, volunteer.id) or
                        not eligibility.check(session, volunteer, shift, tz=policies.get("church_timezone"))):
                    error = "recipient is no longer eligible or consenting"
                    offers.close(session, outreach, "blocked", now)
                    fill.next_action_at = now
                elif meta and meta.state == "offer_active":
                    error = offers.problem(session, outreach, now)
                else:
                    error = offers.dispatch(session, outreach, row, now, exact=exact)
                if error and approval and "fresh exact review" in error:
                    approval.status = "expired"
                    payload = {k:v for k,v in approval.payload.items() if k not in {"message_id", "content_hash", "expires_at"}}
                    confirmations.stage_text(gate, {**payload, "body": meta.body})
                    outreach.message_id, meta.message_id = None, None
                    fill.state, fill.next_action_at = "waiting_approval", offers.cutoff(session, shift.event.starts_at)
            else:
                error = "offer metadata is missing"
        if problem := outbound_style_problem(row.body):
            row.status = "blocked_style"
            session.commit()
            raise HTTPException(409, problem)
        if error:
            row.status = "blocked_confirmation"
            session.commit()
            raise HTTPException(409, error)
        session.commit()
        return {"verified": True, "phone": row.phone, "body": row.body,
                **({"content_hash": approval.payload["content_hash"], "approval_expires_at": approval.payload["expires_at"]} if approval else {})}
