"""Superadmin controls for the private Google Voice connector."""

import json

from fastapi import APIRouter, Depends, HTTPException, Request, Response
from fastapi.concurrency import run_in_threadpool
from sqlalchemy import func, select

from app.db import models as m
from app.integrations.google_voice_client import ConnectorUnavailable, connector_for, verified_health
from app.integrations.google_voice_runtime import is_paused, set_paused
from app.integrations.google_voice_models import GoogleVoiceInboundReceipt
from app.sms.google_voice_provider import GoogleVoiceProvider
from app.web.texty import admin
from app.integrations import google_voice_policy

router = APIRouter(prefix="/api/cloud-texting")


async def superadmin(request: Request, response: Response, user=Depends(admin)):
    allowed = {email.strip().lower() for email in
               request.app.state.settings.superadmin_email_allowlist.split(",") if email.strip()}
    if not user.get("email_confirmed_at") or user.get("email", "").lower() not in allowed:
        raise HTTPException(403, "This verified account does not have superadmin access.")
    response.headers["Cache-Control"] = "no-store"
    return user


def connection_status(state):
    settings = state.settings
    enabled = isinstance(state.provider, GoogleVoiceProvider) and settings.google_voice_enabled
    with state.session_factory() as session:
        paused = is_paused(session)
        counts = dict(session.execute(select(m.Message.status, func.count()).where(
            m.Message.direction == "out", m.Message.provider_sid.startswith("GV"))
            .group_by(m.Message.status)).all())
        held = dict(session.execute(select(GoogleVoiceInboundReceipt.result["state"].as_string(), func.count())
            .where(GoogleVoiceInboundReceipt.result["state"].as_string().in_(("held_gloo", "held_expired_session")))
            .group_by(GoogleVoiceInboundReceipt.result["state"].as_string())).all()) if isinstance(state.provider, GoogleVoiceProvider) else {}
    if isinstance(state.provider, GoogleVoiceProvider) and not google_voice_policy.google_voice_automation_allowed():
        return {"authorized": True, "provider": "google_voice", "state": "policy_hold",
                "reason_code": google_voice_policy.POLICY_HOLD_CODE,
                "policy_message": google_voice_policy.POLICY_HOLD_MESSAGE,
                "connection": {"connected": False, "state": "policy_hold"},
                "paused": True, "enabled": False, "live_enabled": False,
                "held_inbound": {key: held.get(key, 0) for key in ("held_gloo", "held_expired_session")},
                "gloo_ready": bool(settings.gloo_api_key), "test_recipients": 0,
                "queue": {key: counts.get(key, 0) for key in
                          ("queued", "dispatching", "submitted", "uncertain", "rejected")}}
    connection = {"connected": False, "state": "disabled"}
    if enabled:
        try:
            health = connector_for(state).health()
            connected = verified_health(health, settings)
            connection = {"connected": connected, "state": "ready" if connected else "reconnect_required"}
            if connected:
                connection.update(account_email=settings.google_voice_expected_email,
                                  number=settings.google_voice_expected_number)
        except (ConnectorUnavailable, ValueError):
            connection = {"connected": False, "state": "unreachable"}
    status = ("disabled" if not enabled else "paused" if paused else
              "live_disabled" if not settings.live_sms else
              "gloo_unavailable" if not settings.gloo_api_key else connection["state"])
    return {"authorized": True, "provider": "google_voice", "state": status,
            "connection": connection, "paused": paused, "enabled": enabled,
            "live_enabled": settings.live_sms,
            "held_inbound": {key: held.get(key, 0) for key in ("held_gloo", "held_expired_session")},
            "gloo_ready": bool(settings.gloo_api_key),
            "test_recipients": len(getattr(state.provider, "phones", [])) if enabled else 0,
            "queue": {key: counts.get(key, 0) for key in
                      ("queued", "dispatching", "submitted", "uncertain", "rejected")}}


@router.get("")
def status(request: Request, _user=Depends(superadmin)):
    return connection_status(request.app.state)


async def small_json(request):
    # FastAPI/Pydantic validation errors can echo invalid input. Parse privately
    # so malformed credentials never appear in an API error response.
    data = await request.body()
    if len(data) > 64 * 1024:
        raise HTTPException(400, "The submitted configuration is too large.")
    try:
        result = json.loads(data)
    except (ValueError, TypeError):
        raise HTTPException(400, "Submit valid JSON configuration.") from None
    if not isinstance(result, dict):
        raise HTTPException(400, "Submit a configuration object.")
    return result


def validate_cookies(value):
    if not isinstance(value, list) or not 1 <= len(value) <= 100:
        raise ValueError("Provide a Google session cookie array")
    required = {"SID", "HSID", "SSID", "APISID", "SAPISID"}
    names = set()
    allowed_keys = {"name", "value", "domain", "path", "expires", "httpOnly", "secure", "sameSite", "partitionKey"}
    for cookie in value:
        if (not isinstance(cookie, dict) or set(cookie) - allowed_keys or
                not all(isinstance(cookie.get(key), str) for key in ("name", "value", "domain", "path")) or
                not 1 <= len(cookie["name"]) <= 256 or not 1 <= len(cookie["value"]) <= 8192 or
                cookie["domain"].lstrip(".") not in {"google.com", "accounts.google.com", "voice.google.com"} or
                cookie["path"] != "/" or cookie.get("sameSite", "Lax") not in {"Lax", "Strict", "None"} or
                any(key in cookie and not isinstance(cookie[key], bool) for key in ("secure", "httpOnly")) or
                ("expires" in cookie and (isinstance(cookie["expires"], bool) or not isinstance(cookie["expires"], (int, float)))) or
                ("partitionKey" in cookie and not isinstance(cookie["partitionKey"], str))):
            raise ValueError("Invalid Google session cookie format")
        names.add(cookie["name"])
    if not required <= names:
        raise ValueError("Google session is missing required authentication cookies")
    return value


@router.post("/session")
async def import_session(request: Request, _user=Depends(superadmin)):
    state = request.app.state
    if not google_voice_policy.google_voice_automation_allowed():
        raise HTTPException(503, google_voice_policy.POLICY_HOLD_MESSAGE)
    if not isinstance(state.provider, GoogleVoiceProvider):
        raise HTTPException(503, "Google Voice transport is not configured.")
    data = await small_json(request)
    try:
        cookies = validate_cookies(data.get("cookies"))
    except ValueError:
        raise HTTPException(400, "Provide a valid Google session cookie array.") from None
    # Reconnecting never silently resumes pending messages.
    with state.session_factory() as session:
        set_paused(session, True)
        session.commit()
    try:
        await run_in_threadpool(connector_for(state).import_session, cookies)
    except (ConnectorUnavailable, ValueError):
        raise HTTPException(503, "Google Voice session could not be verified. Sending remains paused.") from None
    return await run_in_threadpool(connection_status, state)


@router.post("/pause")
async def pause(request: Request, _user=Depends(superadmin)):
    data = await small_json(request)
    if set(data) != {"paused"} or type(data["paused"]) is not bool:
        raise HTTPException(400, "Provide paused as true or false.")
    state = request.app.state
    if not data["paused"] and not google_voice_policy.google_voice_automation_allowed():
        raise HTTPException(503, google_voice_policy.POLICY_HOLD_MESSAGE)
    if not data["paused"]:
        if not isinstance(state.provider, GoogleVoiceProvider) or not state.settings.gloo_api_key:
            raise HTTPException(409, "Configure Google Voice and Gloo before resuming.")
        try:
            health = await run_in_threadpool(connector_for(state).health)
            ready = verified_health(health, state.settings)
        except (ConnectorUnavailable, ValueError):
            ready = False
        if not ready:
            raise HTTPException(409, "Reconnect and verify the expected Google Voice account before resuming.")
    with state.session_factory() as session:
        set_paused(session, data["paused"])
        session.commit()
    return await run_in_threadpool(connection_status, state)
