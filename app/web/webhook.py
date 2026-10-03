"""Twilio inbound webhook (PLAN.md Phase 6).

POST /sms/inbound receives Twilio's form payload, validates the
X-Twilio-Signature header against the auth token, runs the shared
handle_inbound routing, and returns empty TwiML — replies go out through the
send gate via the REST API, never inline, so every message hits the same
policy checks and log.
"""

import logging
import re
from functools import partial

from fastapi import APIRouter, Form, Header, HTTPException, Request
from fastapi.responses import Response
from twilio.request_validator import RequestValidator
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from app.db import models as m

from app.agents.fill_agent import FillContext
from app.core.inbound import handle_inbound
from app.llm.parser import parse_inbound

logger = logging.getLogger("webhook")

router = APIRouter()

EMPTY_TWIML = '<?xml version="1.0" encoding="UTF-8"?><Response></Response>'


def _validate_signature(request: Request, form: dict, signature: str | None) -> None:
    settings = request.app.state.settings
    if settings.competition_confirmation_required and not settings.sms_is_live:
        raise HTTPException(503, "Twilio ingress is outside supported human confirmation mode")
    if settings.mac_bridge_enabled:
        raise HTTPException(503, "Twilio ingress is inactive in Mac mode")
    if not settings.twilio_auth_token:
        raise HTTPException(503, "Twilio is not configured")
    # Twilio signs the public URL it POSTed to, not the localhost URL uvicorn
    # sees behind ngrok, so reconstruct it from PUBLIC_BASE_URL.
    if settings.public_base_url:
        url = settings.public_base_url.rstrip("/") + request.url.path
        if request.url.query:
            url += "?" + request.url.query
    else:
        url = str(request.url)
    validator = RequestValidator(settings.twilio_auth_token)
    if not validator.validate(url, form, signature or ""):
        logger.warning("rejected inbound webhook with bad signature")
        raise HTTPException(403, "invalid Twilio signature")


@router.post("/sms/inbound")
async def sms_inbound(
    request: Request,
    From: str = Form(...),
    Body: str = Form(""),
    x_twilio_signature: str | None = Header(default=None),
) -> Response:
    form = dict((await request.form()).items())
    _validate_signature(request, form, x_twilio_signature)

    state = request.app.state
    if state.settings.sms_is_live:
        _validate_account(state.settings, form)
        if not re.fullmatch(r"\+[1-9][0-9]{7,14}", From) or len(Body) > 1600:
            raise HTTPException(422, "Invalid sender or text length")
        sid = form.get("MessageSid", "")
        if not sid.startswith("SM") or len(sid) != 34:
            raise HTTPException(422, "A valid MessageSid is required")
        # Twilio's HTTP request must not wait for an AI call. Persist then ack;
        # the independent cloud worker interprets it through Gloo.
        with state.session_factory() as session:
            key = "cloud-inbound:" + sid
            if session.get(m.Notification, key) is None:
                session.add(m.Notification(key=key, purpose="cloud_inbound", body=Body,
                    state="queued", due_at=state.clock.now(), created_at=state.clock.now(),
                    detail={"phone": From, "provider_sid": sid}))
                try:
                    session.commit()
                except IntegrityError:
                    session.rollback()  # The same carrier retry already exists.
        return Response(content=EMPTY_TWIML, media_type="application/xml")
    session = state.session_factory()
    try:
        ctx = FillContext(session, state.clock, state.provider, state.gloo)
        result = handle_inbound(
            session, state.clock, state.provider, From, Body,
            partial(parse_inbound, state.gloo), ctx=ctx,
            allow_signup=state.settings.allow_text_signup,
        )
        session.commit()
        logger.info("inbound from %s routed to %s", From[-4:], result.routed_to)
    except Exception:
        session.rollback()
        logger.exception("inbound webhook failed")
        raise
    finally:
        session.close()

    return Response(content=EMPTY_TWIML, media_type="application/xml")


def _validate_account(settings, form, *, outbound=False):
    if form.get("AccountSid") != settings.twilio_account_sid:
        raise HTTPException(403, "Wrong Twilio account")
    line = form.get("From" if outbound else "To")
    if line != settings.twilio_from_number:
        raise HTTPException(403, "Wrong texting line")


@router.post("/sms/status")
async def sms_status(request: Request, message_id: int,
                     x_twilio_signature: str | None = Header(default=None)):
    state = request.app.state
    if not state.settings.sms_is_live:
        raise HTTPException(503, "Cloud texting is not active")
    form = dict((await request.form()).items())
    _validate_signature(request, form, x_twilio_signature)
    _validate_account(state.settings, form, outbound=True)
    sid, status = form.get("MessageSid", ""), form.get("MessageStatus", "")
    from app.sms.cloud_delivery import PROGRESS, record_status
    if not sid.startswith("SM") or len(sid) != 34 or status not in PROGRESS:
        raise HTTPException(422, "Invalid delivery status")
    with state.session_factory() as session:
        receipt = session.scalar(select(m.Notification).where(m.Notification.key == f"cloud-sms:{message_id}").with_for_update())
        message = session.scalar(select(m.Message).where(m.Message.id == message_id).with_for_update())
        if not receipt or not message or receipt.state == "queued" or form.get("To") != message.phone:
            raise HTTPException(404, "No matching cloud submission")
        known = receipt.detail.get("provider_sid")
        if known and known != sid:
            raise HTTPException(403, "Message SID changed")
        record_status(session, message, receipt, sid, status, state.clock.now(), form.get("ErrorCode"))
        session.commit()
    return Response(status_code=204)
