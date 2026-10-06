"""Superadmin controls for the private Google Voice connector."""

from contextlib import contextmanager
import json
import hashlib
import time
from uuid import uuid4

from fastapi import APIRouter, Depends, HTTPException, Request, Response
from fastapi.concurrency import run_in_threadpool
from sqlalchemy import func, select

from app.db import models as m
from app.integrations.google_voice_client import ConnectorUnavailable, connector_for, verified_health, verified_identity
from app.integrations.google_voice_runtime import (is_paused, set_paused, poll_inbound, retry_held_inbound,
    dispatch_outbound, demo_inbox_fresh, _tick_lock, _clock)
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
    from app.integrations.google_voice_demo import restore_demo_scope, registered_participants
    restore_demo_scope(state)
    from app.integrations.google_voice_demo_window import window_status
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
    if isinstance(state.provider, GoogleVoiceProvider) and not google_voice_policy.google_voice_steps_allowed(state.settings):
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
            connected = verified_health(health, settings, state.provider)
            identified = verified_identity(health, settings, state.provider)
            connection = {"connected": connected, "identity_verified": identified,
                "state": "ready" if connected else "baseline_pending" if identified else "reconnect_required"}
            if identified:
                connection.update(account_email=settings.google_voice_expected_email,
                                  number=settings.google_voice_expected_number)
        except (ConnectorUnavailable, ValueError):
            connection = {"connected": False, "state": "unreachable"}
    status = ("disabled" if not enabled else "paused" if paused else
              "live_disabled" if not settings.live_sms else
              "gloo_unavailable" if not settings.gloo_api_key else connection["state"])
    demo = google_voice_policy.google_voice_demo_allowed(settings)
    reviewed, pending = [], []
    if demo:
        from app.core import confirmations
        from app.core.cloud_composition import reviewed_composition
        with state.session_factory() as session:
            for approval in session.scalars(select(m.Approval).where(m.Approval.kind == "confirm_text",
                    m.Approval.status == "pending", m.Approval.payload["transport"].as_string() == "google_voice")
                    .order_by(m.Approval.id).limit(100)):
                selected = state.provider.test_sessions.get(approval.payload.get("phone"))
                if selected and selected.active(_clock(state).now()) and confirmations.valid(approval, _clock(state).now()) and reviewed_composition(session, approval, selected):
                    pending.append({"id": approval.id, "phone": approval.payload["phone"], "body": approval.payload["body"],
                        "purpose": approval.payload.get("purpose"),
                        "content_hash": approval.payload["content_hash"]})
            for row in session.scalars(select(m.Message).where(m.Message.direction == "out",
                    m.Message.status == "queued", m.Message.provider_sid.startswith("GV"))
                    .order_by(m.Message.id).limit(20)):
                approval = confirmations.proof_for(session, row)
                selected = state.provider.test_sessions.get(row.phone)
                if (approval and selected and selected.active(_clock(state).now()) and
                        not confirmations.delivery_problem(session, state.provider, approval, _clock(state).now(), row) and
                        reviewed_composition(session, approval, selected)):
                    reviewed.append({"id": row.id, "phone": row.phone, "body": row.body,
                        "body_hash": hashlib.sha256(row.body.encode()).hexdigest(), "status": row.status})
    window = window_status(state) if demo else {"active": False}
    from app.integrations.google_voice_signup import signup_status
    return {"authorized": True, "provider": "google_voice", "state": status,
            "demo_mode": demo, "demo_inbox_fresh": demo_inbox_fresh(state) if demo else False,
            "participants": registered_participants(state) if demo else [],
            "demo_window": window,
            "continuous_signup": signup_status(state),
            "reviewed_messages": reviewed, "pending_reviews": pending,
            "background_processing": window["active"] if demo else None, "delivery_verified": False,
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


@contextmanager
def demo_control_lock(state):
    acquired = False
    if state.settings.google_voice_demo_mode:
        acquired = _tick_lock.acquire(blocking=False)
        if not acquired:
            raise HTTPException(409, "Another demo step is in progress. Wait for its saved outcome before changing the connection or pause state.")
    try:
        yield
    finally:
        if acquired:
            _tick_lock.release()


@router.post("/session")
async def import_session(request: Request, _user=Depends(superadmin)):
    state = request.app.state
    with demo_control_lock(state):
        if not google_voice_policy.google_voice_steps_allowed(state.settings):
            raise HTTPException(503, google_voice_policy.POLICY_HOLD_MESSAGE)
        if not isinstance(state.provider, GoogleVoiceProvider):
            raise HTTPException(503, "Google Voice transport is not configured.")
        data = await small_json(request)
        try:
            cookies = validate_cookies(data.get("cookies"))
        except ValueError:
            raise HTTPException(400, "Provide a valid Google session cookie array.") from None
        # Reconnecting never silently resumes pending messages or inherits a fresh inbox.
        if state.settings.google_voice_demo_mode:
            from app.integrations.google_voice_demo_window import stop_window
            stop_window(state, "Session reconnect requires a new explicit demo window")
            from app.integrations.google_voice_signup import hold_signup
            hold_signup(state, "Cloud sender reconnect requires explicit signup re-enablement")
            state.google_voice_status = {}
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
    with demo_control_lock(state):
        if not data["paused"] and not google_voice_policy.google_voice_steps_allowed(state.settings):
            raise HTTPException(503, google_voice_policy.POLICY_HOLD_MESSAGE)
        if not data["paused"]:
            if not isinstance(state.provider, GoogleVoiceProvider) or not state.settings.gloo_api_key:
                raise HTTPException(409, "Configure Google Voice and Gloo before resuming.")
            try:
                health = await run_in_threadpool(connector_for(state).health)
                ready = verified_health(health, state.settings, state.provider)
            except (ConnectorUnavailable, ValueError):
                ready = False
            if not ready:
                raise HTTPException(409, "Reconnect and verify the expected Google Voice account before resuming.")
        if state.settings.google_voice_demo_mode and data["paused"]:
            from app.integrations.google_voice_demo_window import stop_window
            stop_window(state, "Paused by operator")
            from app.integrations.google_voice_signup import hold_signup
            hold_signup(state, "Cloud signup paused by operator")
        with state.session_factory() as session:
            set_paused(session, data["paused"])
            session.commit()
        return await run_in_threadpool(connection_status, state)


def require_demo(state):
    from app.integrations.google_voice_demo import restore_demo_scope
    restore_demo_scope(state)
    if not isinstance(state.provider, GoogleVoiceProvider) or not google_voice_policy.google_voice_demo_allowed(state.settings):
        raise HTTPException(409, "Bounded Google Voice demo is not configured.")


def demo_audit(state, actor, action, detail):
    with state.session_factory() as session:
        session.add(m.Notification(key="google-demo:" + uuid4().hex, purpose="human_review", body="",
            state="sent", due_at=_clock(state).now(), created_at=_clock(state).now(),
            detail={"action": action, "actor": actor, **detail}))
        session.commit()


def verify_profile_step(state, actor):
    if not _tick_lock.acquire(blocking=False):
        raise HTTPException(409, "Another demo step is in progress.")
    try:
        from app.integrations.google_voice_demo_window import stop_window
        stop_window(state, "Cloud sign-in verification requires a new explicit demo window")
        from app.integrations.google_voice_signup import hold_signup
        hold_signup(state, "Cloud sign-in verification requires explicit signup re-enablement")
        state.google_voice_status = {}
        with state.session_factory() as session:
            set_paused(session, True)
            session.commit()
        health = connector_for(state).verify_profile()
        if not verified_identity(health, state.settings, state.provider):
            raise HTTPException(409, "Cloud sign-in could not verify the dedicated sender. Outgoing work remains paused.")
        demo_audit(state, actor, "demo_profile_verified", {})
        result = connection_status(state)
        result["step_result"] = {"action": "verify_profile",
            "message": "Cloud sender identity verified. Sending remains paused. No inbox was checked or text submitted."}
        return result
    except (ConnectorUnavailable, ValueError):
        raise HTTPException(503, "Private cloud profile could not be verified. Close the manual sign-in window before verification. Sending remains paused.") from None
    finally:
        _tick_lock.release()


@router.post("/demo/verify-profile")
async def demo_verify_profile(request: Request, user=Depends(superadmin)):
    state = request.app.state
    require_demo(state)
    if await small_json(request):
        raise HTTPException(400, "Cloud sign-in verification takes no credentials or configuration.")
    return await run_in_threadpool(verify_profile_step, state, user["email"])


def intake_step(state, actor):
    if not _tick_lock.acquire(blocking=False):
        raise HTTPException(409, "Another demo step is in progress.")
    try:
        connector = connector_for(state)
        previous = connector.health()
        health = connector.intake()
        connected = verified_health(health, state.settings, state.provider)
        state.google_voice_status = {"connected": connected, "checked_monotonic": time.monotonic(),
            "last_checked_at": _clock(state).now().isoformat()}
        if not connected:
            raise HTTPException(409, "Demo account or inbox could not be verified. No outgoing submission was attempted.")
        # Apply current withdrawal before retrying older held Gloo work.
        poll_inbound(state, connector)
        retry_held_inbound(state)
        baseline = not previous.get("baseline_at")
        demo_audit(state, actor, "demo_intake", {"baseline_established": baseline})
        result = connection_status(state)
        result["step_result"] = {"action": "intake", "baseline_established": baseline,
            "message": "Baseline established. Prior history was skipped." if baseline else
                       "Bounded inbox check completed. Any composed reply still needs exact review and a separate send step."}
        return result
    except (ConnectorUnavailable, ValueError):
        raise HTTPException(503, "Private demo inbox unavailable. No outgoing submission was attempted.") from None
    finally:
        _tick_lock.release()


@router.post("/demo/intake")
async def demo_intake(request: Request, user=Depends(superadmin)):
    state = request.app.state
    require_demo(state)
    data = await small_json(request)
    if data:
        raise HTTPException(400, "Inbox check takes no configuration.")
    return await run_in_threadpool(intake_step, state, user["email"])


def dispatch_step(state, actor, message_id, body_hash):
    if not _tick_lock.acquire(blocking=False):
        raise HTTPException(409, "Another demo step is in progress.")
    try:
        if not demo_inbox_fresh(state):
            raise HTTPException(409, "Check the inbox once before sending. A verified demo check is valid for 90 seconds.")
        with state.session_factory() as session:
            row = session.get(m.Message, message_id)
            if not row or row.direction != "out" or not row.provider_sid.startswith("GV"):
                raise HTTPException(404, "Reviewed demo message not found.")
            if row.status != "queued":
                raise HTTPException(409, "This message has already been processed or held. It will not be retried.")
            if hashlib.sha256(row.body.encode()).hexdigest() != body_hash:
                raise HTTPException(409, "Reviewed text changed. Refresh and review the exact message again.")
        demo_audit(state, actor, "demo_single_dispatch_requested", {"message_id": message_id, "body_hash": body_hash})
        dispatch_outbound(state, connector_for(state), message_id=message_id, expected_body_hash=body_hash)
        with state.session_factory() as session:
            outcome = session.get(m.Message, message_id).status
        result = connection_status(state)
        result["step_result"] = {"action": "dispatch", "message_id": message_id, "status": outcome,
            "delivery_verified": False}
        return result
    finally:
        _tick_lock.release()


@router.post("/demo/dispatch")
async def demo_dispatch(request: Request, user=Depends(superadmin)):
    state = request.app.state
    require_demo(state)
    data = await small_json(request)
    if (set(data) != {"message_id", "body_hash"} or type(data["message_id"]) is not int or
            data["message_id"] < 1 or not isinstance(data["body_hash"], str) or
            len(data["body_hash"]) != 64 or any(c not in "0123456789abcdef" for c in data["body_hash"])):
        raise HTTPException(400, "Select one exact reviewed demo message.")
    return await run_in_threadpool(dispatch_step, state, user["email"], data["message_id"], data["body_hash"])


@router.post("/demo/reconcile")
async def demo_reconcile(request: Request, user=Depends(superadmin)):
    state = request.app.state
    require_demo(state)
    data = await small_json(request)
    if (set(data) != {"message_id", "body_hash"} or type(data["message_id"]) is not int or data["message_id"] < 1 or
            not isinstance(data["body_hash"], str) or len(data["body_hash"]) != 64 or
            any(character not in "0123456789abcdef" for character in data["body_hash"])):
        raise HTTPException(400, "Select one unchanged claimed text for observation.")
    from app.integrations.google_voice_reconciliation import reconcile_submission
    return await run_in_threadpool(reconcile_submission, state, user["email"], data["message_id"], data["body_hash"])


@router.post("/demo/recipients")
async def demo_register(request: Request, user=Depends(superadmin)):
    state = request.app.state
    require_demo(state)
    data = await small_json(request)
    from app.sms.google_voice_provider import VOICE_PHONE
    if (set(data) - {"phone", "name"} or
            not isinstance(data.get("phone"), str) or not VOICE_PHONE.fullmatch(data["phone"]) or
            data["phone"] == state.settings.google_voice_expected_number or
            not isinstance(data.get("name", "Demo participant"), str) or not 0 < len(data.get("name", "Demo participant").strip()) <= 80):
        raise HTTPException(400, "Enter the exact +1 mobile for one reviewed initial name invitation. Registration does not opt anyone in.")
    from app.integrations.google_voice_demo import register_participant
    with demo_control_lock(state):
        await run_in_threadpool(register_participant, state, user["email"], data["phone"], data.get("name", "Demo participant").strip())
        return await run_in_threadpool(connection_status, state)


@router.post("/signup/enable")
async def signup_enable(request: Request, user=Depends(superadmin)):
    state = request.app.state
    require_demo(state)
    data = await small_json(request)
    if set(data) != {"enabled"} or type(data["enabled"]) is not bool:
        raise HTTPException(400, "Choose enabled as true or false.")
    from app.integrations.google_voice_signup import set_enabled
    with demo_control_lock(state):
        await run_in_threadpool(set_enabled, state, user["email"], data["enabled"])
        return await run_in_threadpool(connection_status, state)


@router.post("/signup/recover-input")
async def signup_recover_input(request: Request, user=Depends(superadmin)):
    state = request.app.state
    require_demo(state)
    data = await small_json(request)
    if (set(data) != {"receipt_id"} or not isinstance(data["receipt_id"], str) or
            not 1 <= len(data["receipt_id"]) <= 256):
        raise HTTPException(400, "Select one original stored signup receipt.")
    from app.integrations.google_voice_signup_recovery import recover_signup_input
    return await run_in_threadpool(recover_signup_input, state, user["email"], data["receipt_id"])


@router.post('/demo/quiet-test')
async def quiet_test_grant(request: Request, user=Depends(superadmin)):
    state = request.app.state
    require_demo(state)
    data = await small_json(request)
    if (set(data) != {'signup_phone', 'admin_phone', 'confirmed'} or data.get('confirmed') is not True or
            not all(isinstance(data.get(key), str) for key in ('signup_phone', 'admin_phone'))):
        raise HTTPException(400, 'Confirm the exact two authorized tester sessions for tonight’s one-time exception.')
    from app.integrations.google_voice_quiet_test import grant
    with demo_control_lock(state):
        with state.session_factory() as session:
            result = grant(state, session, user['email'], data['signup_phone'], data['admin_phone'], _clock(state).now())
            session.commit()
            return result


def compose_demo_text(state, actor, phone, instruction):
    from app.core import confirmations
    from app.core.cloud_composition import record_composition
    from app.core.send_gate import SendGate, has_open_sensitive_escalation, BLOCKING_ESCALATION_STATUSES
    from app.core.message_style import validate_outbound_style
    from app.core.policies import PolicyStore, in_quiet_hours
    from app.llm.parser import keyword_sensitive
    from app.llm.gloo_client import GlooUnavailableError
    from app.llm.agent_loop import RunLogger
    now = _clock(state).now()
    if not state.settings.gloo_api_key or keyword_sensitive(instruction):
        raise HTTPException(409, "Gloo must be configured and sensitive details stay in internal review.")
    with state.session_factory() as session:
        selected = state.provider.test_sessions.get(phone)
        from app.integrations.google_voice_demo import RECIPIENT_KEY
        registration = session.get(m.Policy, RECIPIENT_KEY + phone)
        first_invitation = bool(registration and registration.value.get("consent_state") == "awaiting_name")
        if first_invitation:
            if registration.value.get("invitation"):
                result = connection_status(state)
                result["step_result"] = {"action": "compose", "message": "This participant already has an initial invitation. Review its saved outcome; it will not be reissued."}
                return result
            if not selected or not selected.active(now) or session.get(m.Policy, "sms_opt_out:" + phone):
                raise HTTPException(409, "This pending signup session is expired or suppressed.")
            session.info["mac_test_session"] = selected
            session.info["conversation_origin"] = "google_voice"
            from app.core.signup_copy import WELCOME
            from app.core.signup_responder import compose_signup_reply
            from app.core.signup_delivery import intake_context
            body = compose_signup_reply(session, _clock(state), state.gloo, WELCOME + " Text STOP to stop.",
                ("FIRST and LAST name", "Text STOP to stop."), phone=phone, signup_conversation=True,
                require_gloo=True, exact_copy=True)
            registration.value = {**registration.value, "invitation": {"body": body,
                "body_hash": hashlib.sha256(body.encode()).hexdigest()}}
            session.info["mac_test_session"] = selected
            session.info["conversation_origin"] = "google_voice"
            gate = SendGate(session, _clock(state), state.provider)
            gate.gloo = state.gloo
            outcome = gate.send(body=body, purpose="signup_reply", phone=phone, kind="ai",
                conversation=intake_context(session, phone, "name", ["name"]))
            if not outcome.approval_id:
                raise HTTPException(409, outcome.reason or "Initial invitation was held.")
            approval = session.get(m.Approval, outcome.approval_id)
            registration.value = {**registration.value, "invitation": {**registration.value["invitation"],
                "approval_id": approval.id, "content_hash": approval.payload["content_hash"]}}
            session.commit()
            result = connection_status(state)
            result["step_result"] = {"action": "compose", "message": "Gloo composed the initial name invitation. Review its exact text and recipient. A first-and-last-name reply after submission is their opt-in."}
            return result
        volunteer = session.scalar(select(m.Volunteer).where(m.Volunteer.phone == phone))
        opted_out = session.get(m.Policy, "sms_opt_out:" + phone)
        if (not selected or not selected.active(now) or not volunteer or not volunteer.sms_opt_in or
                volunteer.status != "active" or (opted_out and opted_out.value.get("value")) or
                has_open_sensitive_escalation(session, volunteer.id) or
                any(row.get("phone") == phone for row in session.scalars(select(m.Escalation.related_ids).where(
                    m.Escalation.category == "sensitive", m.Escalation.status.in_(BLOCKING_ESCALATION_STATUSES))))):
            raise HTTPException(409, "An active consenting demo participant without a texting hold is required.")
        policies = PolicyStore(session)
        if in_quiet_hours(now.astimezone(policies.church_tz()), *policies.quiet_hours()):
            raise HTTPException(409, "Demo composition is held during quiet hours.")
        log = RunLogger(session, _clock(state), agent="cloud_demo_opener", trigger="Operator-requested demo composition", model=state.settings.parser_model)
        try:
            response = state.gloo.create_response(model=state.settings.parser_model,
                instructions="Compose one concise, friendly SMS for this consenting church demo participant using the operator's request. Return only plain text, at most 1600 characters. Never use em dashes or their presentation forms. Do not invent names, facts, consent, dates or promises. Do not add a mandatory YES response or footer. Do not repeat STOP or HELP instructions after the initial invitation. This text must await exact human review.",
                input=json.dumps({"operator_request": instruction}, ensure_ascii=False))
            log.add_usage(getattr(response, "usage", None))
            body = getattr(response, "output_text", None)
            if not isinstance(body, str) or not 0 < len(body.strip()) <= 1600 or keyword_sensitive(body):
                raise GlooUnavailableError("Invalid Gloo demo output")
            validate_outbound_style(body)
            record_composition(session, phone, body, selected)
            log.close("composed_for_review")
            session.info["mac_test_session"] = selected
            session.info["conversation_origin"] = "google_voice"
            gate = SendGate(session, _clock(state), state.provider)
            gate.gloo = state.gloo
            outcome = gate.send(body=body, purpose="manual", kind="ai", volunteer=volunteer)
        except (GlooUnavailableError, ValueError):
            raise HTTPException(503, "Gloo could not compose a valid demo text. Nothing was sent or queued.") from None
        if outcome.approval_id is None:
            raise HTTPException(409, outcome.reason or "Demo texting rules held the draft.")
        session.commit()
    result = connection_status(state)
    result["step_result"] = {"action": "compose", "message": "Gloo composed a draft. Review its exact text and recipient before approval."}
    return result


@router.post("/demo/compose")
async def demo_compose(request: Request, user=Depends(superadmin)):
    state = request.app.state
    require_demo(state)
    data = await small_json(request)
    from app.sms.google_voice_provider import VOICE_PHONE
    if (set(data) != {"phone", "instruction"} or not isinstance(data["phone"], str) or
            not VOICE_PHONE.fullmatch(data["phone"]) or not isinstance(data["instruction"], str) or
            not 0 < len(data["instruction"].strip()) <= 500):
        raise HTTPException(400, "Select a registered participant and describe the demo text in 1–500 characters.")
    with demo_control_lock(state):
        return await run_in_threadpool(compose_demo_text, state, user["email"], data["phone"], data["instruction"])


@router.post("/demo/window")
async def demo_window(request: Request, user=Depends(superadmin)):
    state = request.app.state
    require_demo(state)
    data = await small_json(request)
    if (set(data) != {"minutes", "submission_budget"} or type(data["minutes"]) is not int or
            not 1 <= data["minutes"] <= 30 or type(data["submission_budget"]) is not int or
            not 1 <= data["submission_budget"] <= 1000):
        raise HTTPException(400, "Choose a demo window of 1–30 minutes and a visible submission budget of 1–1000.")
    from app.integrations.google_voice_demo_window import start_window
    with demo_control_lock(state):
        try:
            await run_in_threadpool(start_window, state, user["email"], data["minutes"], data["submission_budget"])
        except ConnectorUnavailable:
            raise HTTPException(503, "Demo connector could not be verified. No demo window started.") from None
        return await run_in_threadpool(connection_status, state)


@router.post("/demo/window/stop")
async def demo_window_stop(request: Request, user=Depends(superadmin)):
    state = request.app.state
    require_demo(state)
    if await small_json(request):
        raise HTTPException(400, "Stop window takes no configuration.")
    from app.integrations.google_voice_demo_window import stop_window
    with demo_control_lock(state):
        await run_in_threadpool(stop_window, state, "Stopped by operator")
        return await run_in_threadpool(connection_status, state)
