"""Twilio inbound webhook (PLAN.md Phase 6).

POST /sms/inbound receives Twilio's form payload, validates the
X-Twilio-Signature header against the auth token, runs the shared
handle_inbound routing, and returns empty TwiML — replies go out through the
send gate via the REST API, never inline, so every message hits the same
policy checks and log.
"""

import logging
from functools import partial

from fastapi import APIRouter, Form, Header, HTTPException, Request
from fastapi.responses import Response
from twilio.request_validator import RequestValidator

from app.agents.fill_agent import FillContext
from app.core.inbound import handle_inbound
from app.llm.parser import parse_inbound

logger = logging.getLogger("webhook")

router = APIRouter()

EMPTY_TWIML = '<?xml version="1.0" encoding="UTF-8"?><Response></Response>'


def _validate_signature(request: Request, form: dict, signature: str | None) -> None:
    settings = request.app.state.settings
    if not settings.twilio_auth_token:
        raise HTTPException(503, "Twilio is not configured")
    # Twilio signs the public URL it POSTed to, not the localhost URL uvicorn
    # sees behind ngrok, so reconstruct it from PUBLIC_BASE_URL.
    if settings.public_base_url:
        url = settings.public_base_url.rstrip("/") + request.url.path
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
    session = state.session_factory()
    try:
        ctx = FillContext(session, state.clock, state.provider, state.gloo)
        result = handle_inbound(
            session, state.clock, state.provider, From, Body,
            partial(parse_inbound, state.gloo), ctx=ctx,
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
