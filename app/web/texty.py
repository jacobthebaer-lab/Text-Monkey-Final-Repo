"""Text Monkey JSON adapter over the existing scheduling core; Supabase admin auth.

The Text Monkey simulator and its approval controls always use mock SMS. Twilio's
signed webhook remains separate and retains the original double send gate.
"""

import secrets
import json
import os
import time
import hashlib
from uuid import UUID
from functools import partial
from pathlib import Path

import httpx
from fastapi import APIRouter, Depends, HTTPException, Query, Request, Response
from fastapi.responses import FileResponse
from sqlalchemy import select, func, or_
from sqlalchemy.orm import selectinload
from sqlalchemy.exc import IntegrityError

from app.agents.fill_agent import FillContext, escalation_deadline
from app.core.inbound import decide_approval, handle_inbound
from app.core.send_gate import SendGate, SendStatus
from app.core.notifications import staffing_snapshots
from app.core.policies import PolicyStore
from app.core.signup import PHONE
from app.db import models as m
from app.llm.parser import parse_inbound
from app.llm.gloo_client import GlooUnavailableError
from app.sms.mock_provider import MockSMSProvider
from app.sms.mac_provider import MacMessagesProvider
from app.sms.transport import transport_name, session_transport, queue_result
from app.core.conversation import inbound_scope
from app.web.routes import db
from app.web import fictional_history

router = APIRouter()
STATIC = Path(__file__).resolve().parents[2] / "web" / "texty" / "public"


def allowed_emails(settings):
    return {
        e.strip().lower()
        for e in settings.admin_email_allowlist.split(",")
        if e.strip()
    }


def check_user(user, settings):
    if user.get("email", "").lower() not in allowed_emails(settings) or not user.get(
        "email_confirmed_at"
    ):
        raise HTTPException(
            403, "This verified account does not have coordinator access.",
            headers={"X-Texty-Auth-Invalid": "1"}
        )
    return user


async def auth_payload(request):
    """Bound untrusted account requests before decoding or contacting Supabase."""
    chunks, size = [], 0
    async for chunk in request.stream():
        size += len(chunk)
        if size > 32768:
            raise HTTPException(413, "Account request is too large.")
        chunks.append(chunk)
    try:
        data = json.loads(b"".join(chunks))
        if not isinstance(data, dict):
            raise ValueError()
        return data
    except (ValueError, UnicodeDecodeError, RecursionError):
        raise HTTPException(422, "Submit a valid account request.") from None


def auth_email(data):
    email = data.get("email")
    if not isinstance(email, str) or not 1 <= len(email.strip()) <= 320:
        raise HTTPException(422, "Enter your administrator email address.")
    return email.strip().lower()


def bridge(request):
    expected = request.app.state.settings.backend_bridge_key
    # Local access still requires a Supabase bearer token. Non-local access
    # additionally requires the Cloudflare-held bridge secret.
    if request.client and request.client.host not in {"127.0.0.1", "::1", "testclient"}:
        if not expected or not secrets.compare_digest(
            request.headers.get("X-Texty-Bridge", ""), expected
        ):
            raise HTTPException(
                403, "This backend is only reachable through the Text Monkey dashboard."
            )


async def admin(request: Request):
    bridge(request)
    s = request.app.state.settings
    if not s.supabase_url or not s.supabase_publishable_key or not allowed_emails(s):
        raise HTTPException(503, "Supabase coordinator login is not configured yet.")
    auth = request.headers.get("Authorization", "")
    if not auth.startswith("Bearer "):
        raise HTTPException(401, "Sign in to continue.")
    try:
        async with httpx.AsyncClient(timeout=10) as client:
            response = await client.get(
                s.supabase_url + "/auth/v1/user",
                headers={"apikey": s.supabase_publishable_key, "Authorization": auth},
            )
        if response.status_code in (401, 403):
            raise HTTPException(401, "Session expired. Sign in again.")
        if response.status_code == 429:
            raise HTTPException(429, "Sign-in is rate limited. Please wait and try again.")
        if response.status_code != 200:
            raise HTTPException(503, "Supabase sign-in is temporarily unavailable.")
        user = response.json()
        if not isinstance(user, dict):
            raise ValueError()
        return check_user(user, s)
    except (httpx.HTTPError, ValueError, AttributeError):
        raise HTTPException(503, "Supabase sign-in is temporarily unavailable.")


@router.get("/api/config")
def config(request: Request):
    s = request.app.state.settings
    from app.integrations import google_voice_policy
    voice_held = s.sms_provider == "google_voice" and not google_voice_policy.google_voice_steps_allowed(s)
    mac_configured = isinstance(request.app.state.provider, MacMessagesProvider)
    mac_connected = (
        mac_configured
        and time.monotonic() - getattr(request.app.state, "mac_last_poll", 0) < 180
    )
    return {
        "name": "Text Monkey",
        "humanConfirmationRequired": s.competition_confirmation_required,
        "adminReplyAvailable": True,
        "acceptanceEventAvailable": bool(os.environ.get("TEXT_MONKEY_ACCEPTANCE_SCOPE_FILE")),
        "productMode": "competition" if s.competition_confirmation_required else "automatic",
        "connected": bool(
            s.supabase_url and s.supabase_publishable_key and allowed_emails(s)
        ),
        "provider": "gloo",
        "aiReady": bool(s.gloo_api_key),
        "liveSms": False,
        "messagingTransport": "mac_messages" if mac_configured else s.sms_provider,
        "macBridgeConfigured": mac_configured,
        "macBridgeConnected": mac_connected,
        "allowTextSignup": s.allow_text_signup,
        "automationEnabled": s.automation_enabled and not s.demo_mode and not voice_held,
        "providerPolicyHold": voice_held,
        "database": "postgres"
        if not s.database_url.startswith("sqlite")
        else "local SQLite",
        "simulatorOnly": not session_transport(request.app.state.provider),
        "cloudTextingAvailable": s.sms_provider == "google_voice",
    }


@router.get("/api/auth/me")
def identity(request: Request, response: Response, user=Depends(admin)):
    response.headers["Cache-Control"] = "no-store"
    allowed = {e.strip().lower() for e in request.app.state.settings.superadmin_email_allowlist.split(",") if e.strip()}
    return {"email": user["email"], "superadmin": user["email"].lower() in allowed}


@router.post("/api/login")
async def login(request: Request, response_headers: Response):
    response_headers.headers["Cache-Control"] = "no-store"
    bridge(request)
    s = request.app.state.settings
    if not s.supabase_url or not s.supabase_publishable_key:
        raise HTTPException(503, "Connect the new Supabase project first.")
    data = await auth_payload(request)
    email = auth_email(data)
    password = data.get("password")
    if not isinstance(password, str) or not 1 <= len(password) <= 4096:
        raise HTTPException(422, "Enter your password.")
    if email not in allowed_emails(s):
        raise HTTPException(401, "Unable to sign in with this account.")
    try:
        async with httpx.AsyncClient(timeout=10) as client:
            response = await client.post(
                s.supabase_url + "/auth/v1/token?grant_type=password",
                headers={"apikey": s.supabase_publishable_key},
                json={"email": email, "password": password},
            )
        if response.status_code == 429:
            raise HTTPException(429, "Sign-in is rate limited. Please wait and try again.")
        if response.status_code >= 500:
            raise HTTPException(503, "Supabase sign-in is temporarily unavailable.")
        if response.status_code != 200:
            raise HTTPException(
                401, "Unable to sign in. Check your email and password."
            )
        result = response.json()
        from app.web.admin_session import token_payload
        return token_payload(result, s)
    except (httpx.HTTPError, ValueError):
        raise HTTPException(503, "Supabase sign-in is temporarily unavailable.")


async def auth_request(settings, path, data, *, method="POST", token=None):
    if not settings.supabase_url or not settings.supabase_publishable_key:
        raise HTTPException(503, "Connect the new Supabase project first.")
    headers = {"apikey": settings.supabase_publishable_key}
    if token:
        headers["Authorization"] = "Bearer " + token
    try:
        async with httpx.AsyncClient(timeout=15) as client:
            response = await client.request(
                method,
                settings.supabase_url + "/auth/v1/" + path,
                headers=headers,
                json=data,
                params={"redirect_to": settings.admin_site_url},
            )
        if response.status_code == 429:
            raise HTTPException(
                429, "Too many attempts. Please wait before trying again."
            )
        if response.status_code >= 500:
            raise HTTPException(503, "Supabase sign-in is temporarily unavailable.")
        if response.status_code >= 400:
            raise HTTPException(
                400,
                "Unable to complete this request. Check your details or try signing in.",
            )
        result = response.json() if response.content else {}
        if not isinstance(result, dict):
            raise ValueError()
        return result
    except (httpx.HTTPError, ValueError, RecursionError):
        raise HTTPException(503, "Supabase sign-in is temporarily unavailable.") from None


@router.post("/api/register")
async def register(request: Request):
    bridge(request)
    settings = request.app.state.settings
    data = await auth_payload(request)
    email = auth_email(data)
    password = data.get("password")
    if email not in allowed_emails(settings):
        raise HTTPException(
            403,
            "This email needs an administrator invitation. Volunteers sign up by text.",
        )
    if not isinstance(password, str) or not 12 <= len(password) <= 128:
        raise HTTPException(422, "Use a password with 12–128 characters.")
    signup = {"email": email, "password": password}
    if "church_details" in data:
        from app.web.admin_setup import validate_details
        try:
            details = validate_details(data["church_details"], complete=True)
        except ValueError as error:
            raise HTTPException(422, str(error))
        signup["data"] = {"church_setup": details}
    await auth_request(settings, "signup", signup)
    return {
        "message": "Check your email to confirm your administrator account, then sign in."
    }


@router.post("/api/recover")
async def recover(request: Request):
    bridge(request)
    settings = request.app.state.settings
    data = await auth_payload(request)
    email = auth_email(data)
    if email in allowed_emails(settings):
        await auth_request(settings, "recover", {"email": email})
    return {
        "message": "If this email has administrator access, a password-reset link is on its way."
    }


@router.post("/api/reset-password")
async def reset_password(request: Request, user=Depends(admin)):
    data = await auth_payload(request)
    password = data.get("password")
    if not isinstance(password, str) or not 12 <= len(password) <= 128:
        raise HTTPException(422, "Use a password with 12–128 characters.")
    await auth_request(
        request.app.state.settings,
        "user",
        {"password": password},
        method="PUT",
        token=request.headers["Authorization"].removeprefix("Bearer "),
    )
    return {"message": "Password updated. Sign in with your new password."}


@router.post("/api/logout")
async def logout(request: Request, user=Depends(admin)):
    await auth_request(
        request.app.state.settings,
        "logout",
        {},
        token=request.headers["Authorization"].removeprefix("Bearer "),
    )
    return {"message": "Signed out."}


def text_setup_block(state, session, volunteer, *, enabled=None, welcome_receipts=None):
    """Share non-mutating welcome preflight between roster and authenticated action."""
    provider = state.provider
    if fictional_history.candidate(volunteer):
        return (409, "fictional_profile", "Texting is paused for this profile.")
    if not session_transport(provider):
        return (503, "connection_paused", "Live texting is paused. Ask the connection owner to restore the approved Messages connection.")
    if transport_name(provider) == "google_voice":
        from app.integrations import google_voice_policy
        if not google_voice_policy.google_voice_steps_allowed(state.settings):
            return (503, "provider_policy_hold", google_voice_policy.POLICY_HOLD_MESSAGE)
    if enabled is None:
        enabled = state.settings.gloo_signup_replies and PolicyStore(session).get("full_text_onboarding")
    if not enabled:
        return (503, "setup_disabled", "Gloo text setup is disabled. Ask an administrator to enable text onboarding.")
    if not provider.allows(volunteer.phone):
        from app.integrations.mac_roster import policy as roster_policy, eligible as roster_eligible
        if transport_name(provider) == "mac_messages" and roster_policy(session) and roster_eligible(session,volunteer):
            return (409, "enrollment_pending", "Connecting this volunteer. Keep the Messages connector online; their welcome message will be available after enrollment.")
        return (403, "outside_approved_scope", "This volunteer is outside the approved texting recipients. Ask the connection owner to review their texting authorization.")
    selected = provider.test_sessions.get(volunteer.phone)
    if selected is None:
        return (409, "session_missing", "This volunteer has no approved texting session. Ask the connection owner to set up an authorized session.")
    if not selected.active(state.mac_delivery_clock.now()):
        return (409, "session_inactive", "This volunteer's approved texting session is not active. Ask the connection owner to review its start and expiry.")
    if not volunteer.sms_opt_in or volunteer.status != "active":
        return (409, "consent_required", "Text consent and an active volunteer profile are required before sending a welcome text.")
    from app.core.volunteer_welcome import previous, terminal_no_send, legacy_terminal_attempt
    if previous(session,volunteer,receipts=welcome_receipts):
        return (409, "welcome_prepared", "A welcome text is already prepared. Check this volunteer's history or pending review; another welcome will not be created.")
    stage=(volunteer.preferences or {}).get("onboarding_stage")
    terminal=terminal_no_send(session,volunteer,session.get(m.Notification,"volunteer-welcome:"+str(volunteer.id)))
    if (stage in {"welcome_name", "interests", "availability"} and not terminal
            and not (stage in {'welcome_name','interests'} and legacy_terminal_attempt(session,volunteer))):
        return (409, "setup_in_progress", "Text setup is already in progress. Their next reply continues it. Check text history below.")
    return None


def profile(v, session, state, availability_by_volunteer=None, *, setup_enabled=None, welcome_receipts=None):
    parts = v.name.split(" ", 1)
    prefs = v.preferences or {}
    latest = availability_by_volunteer.get(v.id) if availability_by_volunteer is not None else session.scalar(
        select(m.Availability)
        .where(m.Availability.volunteer_id == v.id)
        .order_by(m.Availability.id.desc())
    )
    quals = v.qualifications
    background = next(
        (q for q in quals if q.type == "background_check" and q.status == "verified"),
        None,
    )
    setup_block = text_setup_block(state, session, v, enabled=setup_enabled, welcome_receipts=welcome_receipts)
    from app.integrations.mac_roster import eligible as real_eligible
    return {
        "id": str(v.id),
        "phone": v.phone,
        "first_name": parts[0],
        "last_name": parts[1] if len(parts) > 1 else "",
        "ministry": prefs.get("preferred_ministry", "Not set"),
        "status": "active" if v.status == "active" else "paused",
        "consent": v.sms_opt_in,
        "fictional": fictional_history.candidate(v),
        "qualified": any(q.status == "verified" for q in quals),
        "background_check_until": background.expires_on.isoformat()
        if background and background.expires_on
        else None,
        "availability": prefs.get("availability_note")
        or (latest.raw_reply if latest else None)
        or "Not provided",
        "onboarding_stage": prefs.get("onboarding_stage", "not_started" if prefs.get("signup_source") == "sms" else "complete"),
        "can_start_text_setup": setup_block is None,
        "welcome_eligible": setup_block is None and real_eligible(session,v),
        "text_setup_block_code": setup_block[1] if setup_block else None,
        "text_setup_block_reason": setup_block[2] if setup_block else None,
        "interested_roles": prefs.get("interested_roles", []),
        "max_per_month": prefs.get("max_per_month", 3),
        "preferred_services": prefs.get("preferred_services", []),
        "qualifications": [
            {
                "type": q.type,
                "status": q.status,
                "expires_on": q.expires_on.isoformat() if q.expires_on else None,
            }
            for q in quals
        ],
    }


def cancellation_review_context(session, review, provider, now):
    """Project safe current context only after checking the recorded sender/session."""
    from app.core.cancellation_scope import bookings, snapshot, review_source
    ids = review.related_ids
    digest = ids.get('source_body_hash')
    if not isinstance(digest, str) or len(digest) != 64 or ids.get('transport') != transport_name(provider):
        return None
    selected = provider.test_sessions.get(ids.get('phone')) if session_transport(provider) else None
    source = review_source(session, ids.get('volunteer_id'), ids.get('message_id'), provider, selected, now, digest)
    if source is None or source.phone != ids.get('phone') or ids.get('session_id') != (selected.id if selected else None):
        return None
    volunteer = session.get(m.Volunteer, ids['volunteer_id'])
    hold = session.get(m.Notification, ids.get('scope_key'))
    if (not hold or hold.volunteer_id != volunteer.id or hold.purpose != 'cancellation_scope'
            or hold.state != 'pending' or not isinstance(ids.get('bookings'), list)
            or ids['bookings'] != hold.detail.get('bookings')):
        return None
    current = bookings(session, volunteer, now)
    changed = ids['bookings'] != snapshot(current)
    return {'volunteer_id':str(volunteer.id), 'recipient_name':volunteer.name, 'scope_changed':changed,
            'bookings':[{'assignment_id':str(a.id), 'shift_id':str(a.shift_id), 'role':a.shift.role.name,
                         'event_title':a.shift.event.title, 'starts_at':a.shift.starts_at.isoformat(),
                         'status':a.status} for a in current],
            'next_step':('Bookings have changed. Review the current schedule before resolving this cancellation internally.' if changed
                         else 'Review this volunteer’s current roles and dates in Shifts. Identify the intended booking before making any change.'),
            'delivery':'internal_only'}


def scoped_message_query(state):
    """Apply the existing native-session privacy boundary before fetching bodies."""
    query = select(m.Message)
    provider = state.provider
    if session_transport(provider):
        conditions = []
        for phone, selected in provider.test_sessions.items():
            if selected.active(state.mac_delivery_clock.now()):
                conditions.append((m.Message.phone == phone) & (
                    inbound_scope(selected) |
                    m.Message.provider_sid.startswith(selected.outbound_prefix)) &
                    (m.Message.created_at >= selected.starts_at) & selected.window(m.Message.created_at))
        query = query.where(or_(*conditions) if conditions else False)
    return query


def history_message(message, *, fictional=False):
    return {"id": str(message.id), "phone": message.phone, "body": message.body,
            "direction": "inbound" if message.direction == "in" else "outbound",
            "status": "simulated" if fictional or (message.provider_sid and message.provider_sid.startswith("MOCK")) else message.status,
            "fictional": fictional, "created_at": message.created_at.isoformat()}


@router.get("/api/volunteers/{volunteer_id}/history")
def volunteer_history(request: Request, response: Response, volunteer_id: int,
                      limit: int = Query(100, ge=1, le=200),
                      before_id: int | None = Query(None, ge=1),
                      user=Depends(admin), session=Depends(db)):
    response.headers["Cache-Control"] = "no-store"
    person = session.get(m.Volunteer, volunteer_id)
    if person is None:
        raise HTTPException(404, "Volunteer not found.")
    fictional = fictional_history.candidate(person)
    query = fictional_history.history_query(session, person) if fictional else scoped_message_query(request.app.state)
    query = query.where(m.Message.volunteer_id == person.id, m.Message.phone == person.phone)
    if before_id is not None:
        query = query.where(m.Message.id < before_id)
    rows = session.scalars(query.order_by(m.Message.id.desc()).limit(limit+1)).all()
    more = len(rows) > limit
    rows = rows[:limit]
    return {"volunteer_id": str(person.id), "fictional": fictional,
            "messages": [history_message(row, fictional=fictional) for row in reversed(rows)],
            "next_before_id": rows[-1].id if more else None}


@router.get("/api/state")
def state(request: Request, user=Depends(admin), session=Depends(db)):
    state = request.app.state
    now = request.app.state.clock.now()
    roles = {r.id: r for r in session.scalars(select(m.Role)).all()}
    upcoming = session.scalars(
        select(m.Shift)
        .options(selectinload(m.Shift.event))
        .join(m.Event)
        .where(m.Shift.starts_at >= now, ~m.Shift.coverage_children.any())
        .order_by(m.Shift.starts_at)
        .limit(160)
    ).all()
    shift_ids = {s.id for s in upcoming}
    shifts = [
        {
            "id": str(s.id),
            "title": s.event.title,
            "role": roles[s.role_id].name,
            "ministry": roles[s.role_id].ministry,
            "starts_at": s.starts_at.isoformat(),
            "ends_at": s.ends_at.isoformat(),
            "required": 1,
            "event_id": str(s.event_id),
            "fill_policy": roles[s.role_id].fill_policy,
            "sensitive": bool(roles[s.role_id].required_qualifications),
            "required_qualifications": roles[s.role_id].required_qualifications or [],
        }
        for s in upcoming
    ]
    volunteers = session.scalars(select(m.Volunteer).options(selectinload(m.Volunteer.qualifications)).order_by(m.Volunteer.name)).all()
    latest_ids = select(func.max(m.Availability.id)).where(
        m.Availability.volunteer_id.in_([v.id for v in volunteers])).group_by(m.Availability.volunteer_id)
    availability = {a.volunteer_id: a for a in session.scalars(select(m.Availability).where(m.Availability.id.in_(latest_ids)))}
    setup_enabled = state.settings.gloo_signup_replies and PolicyStore(session).get("full_text_onboarding")
    welcome_receipts={n.volunteer_id:n for n in session.scalars(select(m.Notification).where(m.Notification.purpose=='volunteer_welcome'))}
    profiles = [profile(v, session, state, availability, setup_enabled=setup_enabled, welcome_receipts=welcome_receipts) for v in volunteers]
    assignments = [
        {
            "id": str(a.id),
            "volunteer_id": str(a.volunteer_id),
            "shift_id": str(a.shift_id),
            "status": a.status,
        }
        for a in session.scalars(
            select(m.Assignment).where(
                m.Assignment.status.in_(["approved", "confirmed"])
            )
        ).all()
        if a.shift_id in shift_ids
    ]
    message_query = scoped_message_query(state)
    provider = request.app.state.provider
    msgs = session.scalars(message_query.order_by(m.Message.id.desc()).limit(200)).all()
    messages = [history_message(v) for v in reversed(msgs)]
    proposals = []
    by_id = {v.id: v for v in volunteers}
    proposal_query = select(m.Approval)
    if session_transport(state.provider):
        now = state.mac_delivery_clock.now()
        active = [(m.Approval.payload["phone"].as_string() == phone) &
                  (m.Approval.payload["session_id"].as_string() == selected.id) &
                  (m.Approval.requested_at >= selected.starts_at) & selected.window(m.Approval.requested_at)
                  for phone, selected in state.provider.test_sessions.items() if selected.active(now)]
        # Select provenance before loading JSON bodies; preserve explicitly simulated proposals.
        from app.web.signup_preferences import review_scope
        proposal_query = proposal_query.where(or_(m.Approval.payload["transport"].as_string() == "mock_or_twilio",
            (m.Approval.payload["transport"].as_string() == transport_name(state.provider)) & or_(*active) if active else False,
            review_scope(state.provider)))
    for a in session.scalars(proposal_query.order_by(m.Approval.requested_at.desc())).all():
        v = by_id.get(a.payload.get("volunteer_id"))
        phone = a.payload.get("phone") or (v.phone if v else "")
        proposals.append(
            {
                "id": str(a.id),
                "phone": phone,
                "intent": a.kind,
                "summary": f"Review signup: {a.payload.get('first_name', '')} {a.payload.get('last_name', '')}"
                if a.kind == "signup"
                else f"Approve {a.kind.replace('_', ' ')}",
                "reply": a.payload.get("body", ""),
                "confirmation_required": a.kind in {"confirm_text", "confirm_record"},
                "content_hash": a.payload.get("content_hash"),
                "reason": a.payload.get("reason"),
                "expires_at": a.payload.get("expires_at"),
                "record_change": {"record": a.payload.get("record"), "before": a.payload.get("before"), "after": a.payload.get("after")} if a.kind == "confirm_record" else None,
                "status": a.status,
                "confidence": 1,
                "provider": "Gloo / scheduling core",
                "created_at": a.requested_at.isoformat(),
            }
        )
    escalation_query = select(m.Escalation).where(m.Escalation.status == "open")
    if session_transport(state.provider):
        active_care = [(m.Escalation.related_ids["phone"].as_string() == phone) &
                       (m.Escalation.related_ids["session_id"].as_string() == selected.id) &
                       (m.Escalation.created_at >= selected.starts_at) & selected.window(m.Escalation.created_at)
                       for phone, selected in state.provider.test_sessions.items() if selected.active(now)]
        escalation_query = escalation_query.where(or_(m.Escalation.related_ids["transport"].as_string() == "mock_or_twilio",
            (m.Escalation.related_ids["transport"].as_string() == transport_name(state.provider)) & or_(*active_care) if active_care else False))
    escalations = []
    for e in session.scalars(escalation_query).all():
        item = {'id':str(e.id), 'category':e.category, 'summary':e.summary,
                'severity':e.severity, 'status':e.status}
        if e.category == 'cancellation_scope':
            context = cancellation_review_context(session, e, state.provider, state.mac_delivery_clock.now())
            if context is None:
                continue
            item['summary'] = 'Cancellation needs a specific current role and day.'
            item['internal_review'] = context
        escalations.append(item)
    events = {s.event.id: s.event for s in upcoming}
    fills = []
    for f in session.scalars(select(m.FillRequest).where(m.FillRequest.shift_id.in_(shift_ids)).order_by(m.FillRequest.created_at.desc())):
        asks = session.scalars(select(m.Outreach).where(m.Outreach.fill_request_id == f.id)).all()
        fills.append({"id": str(f.id), "shift_id": str(f.shift_id), "state": f.state,
                      "batch": f.current_tranche, "next_action_at": f.next_action_at.isoformat() if f.next_action_at else None,
                      "asked": len([o for o in asks if o.message_id]),
                      "declined": len([o for o in asks if o.response == "no"]),
                      "accepted": len([o for o in asks if o.response == "yes"]),
                      "created_at": f.created_at.isoformat(),
                      "escalate_at": escalation_deadline(f, session.get(m.Shift, f.shift_id).event, session).isoformat()})
    policies = PolicyStore(session)
    signup_drafts=[]
    if transport_name(state.provider)=='mac_messages':
        from app.web.signup_preferences import scoped
        from app.core.signup_preference_review import card
        for person in volunteers:
            if not person.preferences.get('onboarding_availability_draft'):continue
            try:
                scoped(session,state,person)
                signup_drafts.append(card(session,person,state.mac_delivery_clock.now()))
            except ValueError:
                continue
    return {
        "signup_preference_drafts":signup_drafts,
        "staffing": staffing_snapshots(session, events.values()),
        "fills": fills,
        "timing": {"quiet_hours": policies.get("quiet_hours"), "urgent_quiet_hours": policies.get("urgent_quiet_hours"),
                   "monthly_ask_limit": policies.ask_budget(), "outreach_cooldown_hours": policies.get("outreach_cooldown_hours"),
                   "signup_enabled": policies.get("full_text_onboarding"), "coordinator_debounce_minutes": 5,
                   "coordinator_minimum_gap_minutes": 15, "pre_event_update_hours": 3},
        "notifications": [{"event_id": str(n.event_id) if n.event_id else None, "state": n.state,
                           "due_at": n.due_at.isoformat(), "purpose": n.purpose} for n in session.scalars(
                               select(m.Notification).where(m.Notification.state.in_(("pending", "blocked"))).limit(100))],
        "volunteers": profiles,
        "shifts": shifts,
        "assignments": assignments,
        "messages": messages,
        "proposals": proposals,
        "escalations": escalations,
    }


def validated(data):
    first, last = data.get("first_name", ""), data.get("last_name", "")
    phone = data.get("phone", "")
    if not PHONE.fullmatch(phone) or not all(
        isinstance(n, str) and 0 < len(n.strip()) <= 80 for n in (first, last)
    ):
        raise HTTPException(
            400, "Enter a first name, last name, and international phone number."
        )
    return first.strip() + " " + last.strip(), phone


@router.post("/api/signup-invitations")
async def invite_signup(request: Request, user=Depends(admin), session=Depends(db)):
    """Start only the explicitly authorized current Mac demo conversation."""
    state = request.app.state
    try:
        data = await request.json()
    except ValueError:
        raise HTTPException(400, "Enter a name, international phone, and request ID.")
    if (not isinstance(data, dict) or set(data) != {"phone", "name", "request_id"}
            or not isinstance(data.get("phone"), str) or not PHONE.fullmatch(data["phone"])
            or not isinstance(data.get("name"), str) or not 0 < len(data["name"].strip()) <= 160):
        raise HTTPException(400, "Enter a name and exact international phone number.")
    try:
        request_id = str(UUID(data["request_id"]))
    except (ValueError, TypeError, AttributeError):
        raise HTTPException(400, "A valid invitation request ID is required for safe retries.")
    phone, name = data["phone"], data["name"].strip()
    if not isinstance(state.provider, MacMessagesProvider):
        raise HTTPException(503, "Start signup requires the enabled Mac Messages connection.")
    if not state.provider.allows(phone):
        raise HTTPException(403, "This recipient is outside the enabled phone numbers.")
    selected = state.provider.test_sessions.get(phone)
    if selected is None or not selected.active(state.mac_delivery_clock.now()):
        raise HTTPException(409, "An active texting session is required before inviting this recipient.")
    session.info["mac_test_session"] = selected
    session.info["conversation_origin"] = transport_name(state.provider)
    from app.core.signup_copy import compose_welcome, exact_enabled, mac_demo_invitation_enabled
    # Lock the pre-existing operator authorization; this route cannot expand it.
    authorization = session.scalar(select(m.Policy).where(
        m.Policy.key == "mac_demo_invitation:" + phone).with_for_update())
    if not authorization or not exact_enabled(session, phone) or not mac_demo_invitation_enabled(session, phone):
        raise HTTPException(403, "This recipient needs current, specific signup invitation authorization.")
    if not state.settings.gloo_signup_replies or not PolicyStore(session).get("full_text_onboarding"):
        raise HTTPException(503, "Gloo text signup is not enabled.")
    key = "signup-invitation:" + request_id
    binding = {"phone": phone, "name": name, "session_id": selected.id}
    previous = session.get(m.Notification, key)
    if previous:
        if any(previous.detail.get(field) != value for field, value in binding.items()):
            raise HTTPException(409, "This invitation request already belongs to a different recipient or session.")
        if previous.detail.get("result"):
            result=previous.detail["result"]
            approval=session.get(m.Approval,result.get('approval_id')) if result.get('approval_id') else None
            message_id=(approval.payload.get('message_id') if approval else None) or result.get('message_id')
            message=session.get(m.Message,message_id) if message_id else None
            if message and message.status=='blocked_native_route':
                return {**result,'delivery':'held_native_route',
                    'reason':'This invitation is held because the selected Messages route is unavailable. Automatic retry is unavailable for this invitation. Ask the connection owner to review it.'}
            return previous.detail["result"]
        raise HTTPException(409, "This invitation is being queued. Retry the same request shortly.")
    if session.scalar(select(m.Volunteer.id).where(m.Volunteer.phone == phone)):
        raise HTTPException(409, "This phone already has a volunteer profile. Use its text setup action.")
    now = state.clock.now()
    receipt = m.Notification(key=key, purpose="signup_invitation", state="reserved",
        due_at=now, created_at=now, detail={**binding, "operator": user.get("email")})
    try:
        session.add(receipt)
        session.flush()
    except IntegrityError:
        session.rollback()
        raise HTTPException(409, "This invitation is being queued. Retry the same request shortly.")
    from app.core.signup_delivery import intake_context, intake_block
    conversation = intake_context(session, phone, "name", ["name"])
    gate = SendGate(session, state.clock, state.provider)
    gate.gloo = state.gloo
    blocked = intake_block(session, state.clock, gate, phone=phone, volunteer=None, conversation=conversation)
    if blocked:
        raise HTTPException(409, blocked.reason)
    try:
        body = compose_welcome(session, state.clock, state.gloo, phone)
        outcome = gate.send(body=body, phone=phone, purpose="signup_reply", kind="ai", conversation=conversation)
    except GlooUnavailableError:
        raise HTTPException(503, "Gloo could not compose the signup invitation. Nothing was sent; retry this request.")
    if not outcome.sent and not outcome.approval_id:
        raise HTTPException(409, outcome.reason or "The invitation is held by the texting rules.")
    result = {"delivery": "awaiting_confirmation" if outcome.approval_id else queue_result(state.provider),
              "approval_id": outcome.approval_id, "message_id": outcome.message_id, "phone": phone, "body": body}
    receipt.state = "awaiting_review" if outcome.approval_id else "queued"
    receipt.message_id = outcome.message_id
    receipt.detail = {**receipt.detail, "result": result}
    session.flush()
    return result


@router.post("/api/volunteers")
async def create_volunteer(request: Request, user=Depends(admin), session=Depends(db)):
    data = await request.json()
    name, phone = validated(data)
    if session.scalar(select(m.Volunteer).where(m.Volunteer.phone == phone)):
        raise HTTPException(409, "This phone already has a volunteer profile.")
    v = m.Volunteer(
        name=name,
        phone=phone,
        sms_opt_in=data.get("consent") is True,
        status="active",
        is_coordinator=False,
        is_pastor=False,
        preferences={"preferred_ministry": str(data.get("ministry", "Welcome"))[:80]},
        created_at=request.app.state.clock.now(),
    )
    session.add(v)
    session.flush()
    from app.core.algorithm_outreach import profile as enrollment_profile
    enrollment_profile(session, v, request.app.state.clock.now())
    return profile(v, session, request.app.state)


@router.post("/api/volunteers/{volunteer_id}")
async def update_volunteer(
    request: Request, volunteer_id: int, user=Depends(admin), session=Depends(db)
):
    data = await request.json()
    name, phone = validated(data)
    v = session.get(m.Volunteer, volunteer_id)
    if v is None:
        raise HTTPException(404, "Volunteer not found.")
    if v.phone != phone:
        raise HTTPException(400, "Phone changes require a separate identity review.")
    v.name = name
    v.sms_opt_in = data.get("consent") is True
    v.status = "active" if data.get("status") == "active" else "inactive"
    v.preferences = {
        **(v.preferences or {}),
        "preferred_ministry": str(data.get("ministry", "Welcome"))[:80],
        "availability_note": str(data.get("availability", ""))[:500],
    }
    # No blanket qualification flag. Specific credentials are verified in the
    # existing qualification page, with type, expiry, and coordinator evidence.
    session.flush()
    return profile(v, session, request.app.state)


@router.post("/api/volunteers/{volunteer_id}/text-setup")
def start_text_setup(request: Request, volunteer_id: int, user=Depends(admin), session=Depends(db)):
    """An authenticated coordinator starts setup; volunteers still only text."""
    state = request.app.state
    from app.core.offer_windows import begin_decision
    from app.core.volunteer_welcome import prepare
    begin_decision(session)
    volunteer = session.scalar(select(m.Volunteer).where(m.Volunteer.id == volunteer_id).with_for_update())
    if volunteer is None:
        raise HTTPException(404, "Volunteer not found.")
    result=prepare(session,state,volunteer,user,text_setup_block)
    return {**result,"volunteer":profile(volunteer,session,state)}


@router.post('/api/welcome-batches')
async def welcome_batch(request:Request,user=Depends(admin)):
    try:
        data=await request.json()
        if not isinstance(data,dict) or not isinstance(data.get('request_id'),str): raise ValueError()
        request_id=str(UUID(data['request_id']))
        ids=data['volunteer_ids']
        if (set(data)!={'request_id','volunteer_ids'} or not isinstance(ids,list) or not 1<=len(ids)<=1000
                or any(type(value) is not int or value<1 for value in ids) or len(set(ids))!=len(ids)):
            raise ValueError()
    except (ValueError,TypeError,KeyError):
        raise HTTPException(400,'Choose up to 1,000 distinct volunteers and provide a valid welcome request ID.')
    from starlette.concurrency import run_in_threadpool
    from app.core.volunteer_welcome import batch_step
    return await run_in_threadpool(batch_step,request.app.state,user,request_id,sorted(ids),text_setup_block)


@router.post("/api/reply")
async def compose_admin_reply(request: Request, user=Depends(admin), session=Depends(db)):
    """Coordinator text: normal queued delivery, or optional exact competition review."""
    from app.core import confirmations
    state = request.app.state
    if state.settings.competition_confirmation_required != confirmations.enabled(session):
        raise HTTPException(409, "Texting mode changed. Reload before composing a text.")
    try:
        data = await request.json()
    except ValueError:
        raise HTTPException(400, "Choose a roster recipient and enter a text.")
    if (not isinstance(data, dict) or not {"volunteer_id", "body"} <= set(data)
            or set(data) - {"volunteer_id", "body", "request_id"}
            or type(data.get("volunteer_id")) is not int or data["volunteer_id"] < 1
            or not isinstance(data.get("body"), str) or not 0 < len(data["body"].strip())
            or len(data["body"]) > 1600):
        raise HTTPException(400, "Choose a roster recipient and enter a text of 1–1,600 characters.")
    request_id = None
    if "request_id" in data:
        try:
            request_id = str(UUID(data["request_id"]))
        except (ValueError, TypeError, AttributeError):
            raise HTTPException(400, "Provide a valid text request ID.")
    if not confirmations.enabled(session) and request_id is None:
        raise HTTPException(400, "A text request ID is required for safe retries.")
    volunteer = session.scalar(select(m.Volunteer).where(m.Volunteer.id == data["volunteer_id"]).with_for_update())
    if volunteer is None:
        raise HTTPException(404, "Volunteer not found.")
    if fictional_history.candidate(volunteer):
        raise HTTPException(409, "Fictional profiles cannot receive texts.")
    if not volunteer.sms_opt_in or volunteer.status != "active":
        raise HTTPException(409, "The recipient must be active and have text consent.")
    provider = state.provider if session_transport(state.provider) else MockSMSProvider()
    selected = None
    if session_transport(provider):
        if not provider.allows(volunteer.phone):
            raise HTTPException(403, "This recipient is outside the enabled phone numbers.")
        selected = provider.test_sessions.get(volunteer.phone)
        if selected is None or not selected.active(state.mac_delivery_clock.now()):
            raise HTTPException(409, "An active approved texting session is required before drafting this text.")
        session.info["mac_test_session"] = selected
        session.info["conversation_origin"] = transport_name(provider)
    if transport_name(provider) == "google_voice":
        from app.integrations import google_voice_policy
        if not google_voice_policy.google_voice_steps_allowed(state.settings):
            raise HTTPException(409, google_voice_policy.POLICY_HOLD_MESSAGE)
    from app.core.cloud_composition import require_composition, reviewed_composition
    from app.core.message_style import outbound_style_problem
    from app.core.send_gate import has_open_sensitive_escalation, BLOCKING_ESCALATION_STATUSES
    from app.core.policies import in_quiet_hours
    from app.llm.parser import keyword_sensitive
    body = data["body"]
    if error := outbound_style_problem(body):
        raise HTTPException(409, error)
    opted_out = session.get(m.Policy, "sms_opt_out:" + volunteer.phone)
    care = session.scalars(select(m.Escalation.related_ids).where(
        m.Escalation.category == "sensitive", m.Escalation.status.in_(BLOCKING_ESCALATION_STATUSES)))
    if (opted_out and opted_out.value.get("value") or has_open_sensitive_escalation(session, volunteer.id)
            or any(item.get("phone") == volunteer.phone for item in care)):
        raise HTTPException(409, "This recipient is held by the texting rules.")
    if keyword_sensitive(body):
        raise HTTPException(409, "Recognized sensitive details require internal human review.")
    # A closed care flag still cannot authorize exporting its recorded private text.
    from app.core.privacy import safe_message_history
    source_rows = session.scalars(select(m.Message).where(m.Message.phone == volunteer.phone,
        m.Message.body == body)).all()
    if len(safe_message_history(session, source_rows)) != len(source_rows):
        raise HTTPException(409, "Recognized sensitive details require internal human review.")
    policies = PolicyStore(session)
    if in_quiet_hours(state.clock.now().astimezone(policies.church_tz()), *policies.quiet_hours()):
        raise HTTPException(409, "Text preparation is held during quiet hours.")
    body_hash = hashlib.sha256(data["body"].encode()).hexdigest()
    key = f"admin-compose:{volunteer.id}:{request_id}" if request_id else None
    previous = session.get(m.Notification, key) if key else None
    if previous:
        if previous.detail.get("body_hash") != body_hash:
            raise HTTPException(409, "This text request already contains different words.")
        result = previous.detail.get("result")
        if result is None:
            raise HTTPException(409, "This text is being queued. Retry the same request shortly.")
        approval = session.get(m.Approval, result.get("approval_id"))
        if (approval is None or result.get("body") != data["body"] or result.get("phone") != volunteer.phone
                or not reviewed_composition(session, approval, selected)):
            raise HTTPException(409, "This prior draft needs new exact Gloo preparation. Use a new request ID.")
        if approval.status == "approved":
            message = session.get(m.Message, approval.payload.get("message_id"))
            if message is None or message.body != body or message.phone != volunteer.phone:
                raise HTTPException(409, "The prior exact reply has no verified delivery receipt.")
            return {"delivery": queue_result(provider) if message.status == "queued" else message.status,
                    "message_id": message.id, "phone": message.phone, "body": message.body}
        if approval.status != "pending" or not confirmations.valid(approval, state.clock.now()):
            raise HTTPException(409, "This prior review was rejected or expired. Use a new request ID.")
        return result
    # Exact-mode retries without request IDs may reuse only a current prepared review.
    if key is None:
        pending = session.scalars(select(m.Approval).where(m.Approval.kind == "confirm_text",
            m.Approval.status == "pending", m.Approval.payload["phone"].as_string() == volunteer.phone,
            m.Approval.payload["purpose"].as_string() == "manual",
            m.Approval.payload["body"].as_string() == body))
        for approval in pending:
            if confirmations.valid(approval, state.clock.now()) and reviewed_composition(session, approval, selected):
                return {"delivery": "awaiting_confirmation", "approval_id": approval.id,
                        "content_hash": approval.payload["content_hash"], "phone": volunteer.phone, "body": body}
    reservation = None
    if key:
        reservation = m.Notification(key=key, volunteer_id=volunteer.id, purpose="admin_reply",
                                     body="", state="recorded", due_at=state.clock.now(),
                                     created_at=state.clock.now(), detail={"body_hash": body_hash})
        try:
            # Claim the unique retry ID before creating an outbound message.
            session.add(reservation)
            session.flush()
        except IntegrityError:
            session.rollback()
            raise HTTPException(409, "This text request was already queued. Retry the same request.")
    gate = SendGate(session, state.clock, provider)
    gate.gloo = state.gloo
    try:
        # Verify literal Gloo output before staging the same body and review hash.
        require_composition(session, state.clock, state.gloo, volunteer.phone, body, selected)
        outcome = gate.send(body=body, purpose="manual", volunteer=volunteer, kind="ai")
    except GlooUnavailableError:
        raise HTTPException(503, "Gloo could not prepare the exact reply. Nothing was queued.") from None
    except ValueError as error:
        raise HTTPException(409, str(error))
    if outcome.approval_id is None and not outcome.sent:
        raise HTTPException(409, outcome.reason or "The texting rules blocked this draft.")
    if outcome.approval_id:
        approval = session.get(m.Approval, outcome.approval_id)
        result = {"delivery": "awaiting_confirmation", "approval_id": approval.id,
                  "content_hash": approval.payload["content_hash"], "phone": volunteer.phone,
                  "body": approval.payload["body"]}
    else:
        result = {"delivery": queue_result(provider),
                  "message_id": outcome.message_id, "phone": volunteer.phone, "body": data["body"]}
    if reservation is not None:
        reservation.message_id = outcome.message_id
        reservation.detail = {"body_hash": body_hash, "result": result}
    return result


@router.post("/api/simulate")
async def simulate(request: Request, user=Depends(admin), session=Depends(db)):
    data = await request.json()
    phone, body = data.get("phone", ""), data.get("body", "")
    if (
        not PHONE.fullmatch(phone)
        or not isinstance(body, str)
        or not 0 < len(body.strip()) <= 1600
    ):
        raise HTTPException(
            400, "Use an international phone number and a text under 1,600 characters."
        )
    provider = MockSMSProvider()
    state = request.app.state
    ctx = FillContext(session, state.clock, provider, state.gloo)
    result = handle_inbound(
        session,
        state.clock,
        provider,
        phone,
        body,
        partial(parse_inbound, state.gloo),
        ctx=ctx,
        allow_signup=state.settings.allow_text_signup,
    )
    return {
        "decision": {
            "provider": "gloo unavailable"
            if result.parsed and result.parsed.parse_error
            else "gloo",
            "intent": result.routed_to,
            "sensitive": result.parsed.sensitive
            if result.parsed
            else result.routed_to == "escalated_sensitive",
        },
        "notes": result.notes,
        "mock_sms_count": len(provider.sent),
    }


@router.post("/api/automation/tick")
def automation_tick(request: Request, user=Depends(admin), session=Depends(db)):
    from app.jobs import process_due_fill_requests
    provider = MockSMSProvider()
    outcomes = process_due_fill_requests(FillContext(session, request.app.state.clock, provider, request.app.state.gloo))
    return {"outcomes": [o.action for o in outcomes], "mock_sms_count": len(provider.sent), "delivery": "simulated"}


@router.post("/api/proposals/{proposal_id}/{decision}")
async def review(
    request: Request,
    proposal_id: int,
    decision: str,
    user=Depends(admin),
    session=Depends(db),
):
    if decision not in {"approve", "reject"}:
        raise HTTPException(404)
    approval_query = select(m.Approval).where(m.Approval.id == proposal_id)
    state = request.app.state
    if state.settings.competition_confirmation_required and session_transport(state.provider):
        active_review = [(m.Approval.payload["phone"].as_string() == phone) &
                         (m.Approval.payload["session_id"].as_string() == selected.id) &
                         (m.Approval.requested_at >= selected.starts_at) & selected.window(m.Approval.requested_at)
                         for phone, selected in state.provider.test_sessions.items() if selected.active(state.mac_delivery_clock.now())]
        from app.web.signup_preferences import review_scope
        approval_query = approval_query.where(or_(m.Approval.payload["transport"].as_string() == "mock_or_twilio",
            (m.Approval.payload["transport"].as_string() == transport_name(state.provider)) & or_(*active_review) if active_review else False,
            review_scope(state.provider)))
    a = session.scalar(approval_query)
    if a is None:
        raise HTTPException(404, "Approval not found.")
    if a.status != "pending":
        raise HTTPException(409, "This approval was already reviewed.")
    state = request.app.state
    # Only approvals from this connected transport may queue real replies.
    # Simulator/legacy approvals always retain simulated delivery.
    provider = (
        state.provider
        if session_transport(state.provider)
        and a.payload.get("transport") == transport_name(state.provider)
        else MockSMSProvider()
    )
    gate = SendGate(session, state.clock, provider)
    ctx = FillContext(session, state.clock, provider, state.gloo)
    from app.core import confirmations
    try:
        if confirmations.enabled(session) or a.kind in {'confirm_record', 'confirm_text'}:
            signup_review=a.payload.get('workflow_signup_preferences')
            review_before=None
            if signup_review:
                from app.web.signup_preferences import scoped
                from app.core import profile_sync
                person=session.get(m.Volunteer,signup_review['source']['volunteer_id'])
                scoped(session,state,person)
                review_before=profile_sync.safe_snapshot(session,person.phone)
            data = await request.json()
            expected = data.get("content_hash") if isinstance(data, dict) else None
            if not isinstance(expected, str) or not expected:
                raise ValueError("Review the exact displayed action before approving or rejecting")
            notes = confirmations.decide(session, gate, a, approve=decision == "approve",
                actor=user["email"], expected=expected, now=state.mac_delivery_clock.now() if signup_review else state.clock.now(), ctx=ctx)
            if signup_review and decision=='approve':
                profile_sync.capture(session,state.settings,phone=person.phone,
                    guid=signup_review['source']['receipt_guid'],route='onboarding_complete',
                    before=review_before,effective_at=state.mac_delivery_clock.now())
        else:
            notes = decide_approval(
                session,
                gate,
                a,
                approve=decision == "approve",
                decided_by=user["email"],
                via="web",
                now=state.clock.now(),
                ctx=ctx,
            )
    except ValueError as e:
        raise HTTPException(409, str(e))
    # A reviewed action can still be suppressed by the send gate. Transport
    # configuration alone is never a queue receipt or delivery proof.
    reported = next((str(note).removeprefix("Exact message: ").split(";", 1)[0].strip()
        for note in notes if str(note).startswith("Exact message: ")), None)
    known = {status.value for status in SendStatus}
    reported = reported if reported in known else None
    message = session.get(m.Message, a.payload.get("message_id")) if a.kind == "confirm_text" and a.payload.get("message_id") else None
    if reported and reported != SendStatus.SENT.value:
        delivery, message = reported, None
    elif message:
        delivery = "simulated" if isinstance(provider, MockSMSProvider) else (
            queue_result(provider) if message.status == "queued" else message.status)
    elif isinstance(provider, MockSMSProvider) and provider.sent:
        delivery = "simulated"
    else:
        delivery = "rejected" if decision == "reject" else "not_queued"
    return {
        "reviewed": True,
        "notes": notes,
        "mock_sms_count": len(provider.sent)
        if isinstance(provider, MockSMSProvider)
        else 0,
        "delivery": delivery,
        "message_id": message.id if message else None,
        "message_status": message.status if message else None,
    }


# Same public kit paths are used by the Cloudflare dashboard and legacy admin pages.
from app.web.brand import router as brand_router

router.include_router(brand_router)


PUBLIC_ASSETS = frozenset({
    "index.html", "app.js", "domain.js", "setup.js", "setup-domain.js", "style.css", "church-presentation.js",
    "accessibility.js", "admin-readiness.js", "admin-notifications.js", "planning-workflows.js", "signup-preferences.js", "planning-center-review.js", "onboarding-copy-nav.js",
    "onboarding-copy.js", "onboarding-copy.html", "onboarding-copy.css",
    "onboarding-copy-defaults.json", "cloud-texting.js", "google-calendar.js", "acceptance-workflow.js", "coordinator-workflows.js", "coordinator-session.js", "split-coverage.js", "volunteer-history.js", "bulk-welcome.js",
})


@router.get("/texty")
@router.get("/texty/{asset:path}")
def texty(asset: str = "index.html"):
    if asset not in PUBLIC_ASSETS:
        raise HTTPException(404)
    # JS uses /api endpoints, so local assets intentionally live at root too.
    return FileResponse(STATIC / asset)


@router.get("/google-calendar.js")
@router.get("/cloud-texting.js")
@router.get("/app.js")
@router.get("/church-presentation.js")
@router.get("/domain.js")
@router.get("/setup.js")
@router.get("/setup-domain.js")
@router.get("/style.css")
@router.get("/accessibility.js")
@router.get("/admin-readiness.js")
@router.get("/admin-notifications.js")
@router.get("/planning-workflows.js")
@router.get("/signup-preferences.js")
@router.get("/coordinator-session.js")
@router.get("/coordinator-workflows.js")
@router.get("/split-coverage.js")
@router.get("/volunteer-history.js")
@router.get("/bulk-welcome.js")
@router.get("/acceptance-workflow.js")
@router.get("/planning-center-review.js")
@router.get("/onboarding-copy-nav.js")
@router.get("/onboarding-copy.js")
@router.get("/onboarding-copy.html")
@router.get("/onboarding-copy.css")
@router.get("/onboarding-copy-defaults.json")
def root_asset(request: Request):
    return texty(request.url.path.lstrip("/"))
