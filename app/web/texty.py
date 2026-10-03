"""Text Monkey JSON adapter over the existing scheduling core; Supabase admin auth.

The Text Monkey simulator and its approval controls always use mock SMS. Twilio's
signed webhook remains separate and retains the original double send gate.
"""

import secrets
import time
import hashlib
from uuid import UUID
from functools import partial
from pathlib import Path

import httpx
from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import FileResponse
from sqlalchemy import select, func, or_
from sqlalchemy.orm import selectinload
from sqlalchemy.exc import IntegrityError

from app.agents.fill_agent import FillContext, escalation_deadline
from app.core.inbound import decide_approval, handle_inbound
from app.core.send_gate import SendGate
from app.core.notifications import staffing_snapshots
from app.core.policies import PolicyStore
from app.core.signup import PHONE
from app.db import models as m
from app.llm.parser import parse_inbound
from app.llm.gloo_client import GlooUnavailableError
from app.sms.mock_provider import MockSMSProvider
from app.sms.mac_provider import MacMessagesProvider
from app.web.routes import db

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
            403, "This verified account does not have coordinator access."
        )
    return user


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
        if response.status_code != 200:
            raise HTTPException(401, "Session expired. Sign in again.")
        return check_user(response.json(), s)
    except httpx.HTTPError:
        raise HTTPException(503, "Supabase sign-in is temporarily unavailable.")


@router.get("/api/config")
def config(request: Request):
    s = request.app.state.settings
    mac_configured = isinstance(request.app.state.provider, MacMessagesProvider)
    mac_connected = (
        mac_configured
        and time.monotonic() - getattr(request.app.state, "mac_last_poll", 0) < 180
    )
    return {
        "name": "Text Monkey",
        "humanConfirmationRequired": s.competition_confirmation_required,
        "adminReplyAvailable": True,
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
        "automationEnabled": s.automation_enabled and not s.demo_mode,
        "database": "postgres"
        if not s.database_url.startswith("sqlite")
        else "local SQLite",
        "simulatorOnly": not mac_configured,
    }


@router.post("/api/login")
async def login(request: Request):
    bridge(request)
    s = request.app.state.settings
    if not s.supabase_url or not s.supabase_publishable_key:
        raise HTTPException(503, "Connect the new Supabase project first.")
    data = await request.json()
    if data.get("email", "").lower() not in allowed_emails(s):
        raise HTTPException(401, "Unable to sign in with this account.")
    try:
        async with httpx.AsyncClient(timeout=10) as client:
            response = await client.post(
                s.supabase_url + "/auth/v1/token?grant_type=password",
                headers={"apikey": s.supabase_publishable_key},
                json={"email": data.get("email"), "password": data.get("password")},
            )
        if response.status_code != 200:
            raise HTTPException(
                401, "Unable to sign in. Check your email and password."
            )
        result = response.json()
        check_user(result.get("user", {}), s)
        return {
            "access_token": result["access_token"],
            "email": result["user"]["email"],
        }
    except httpx.HTTPError:
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
        if response.status_code >= 400:
            raise HTTPException(
                400,
                "Unable to complete this request. Check your details or try signing in.",
            )
        return response.json() if response.content else {}
    except httpx.HTTPError:
        raise HTTPException(503, "Supabase sign-in is temporarily unavailable.")


@router.post("/api/register")
async def register(request: Request):
    bridge(request)
    settings = request.app.state.settings
    data = await request.json()
    email = str(data.get("email", "")).strip().lower()
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
    data = await request.json()
    email = str(data.get("email", "")).strip().lower()
    if email in allowed_emails(settings):
        await auth_request(settings, "recover", {"email": email})
    return {
        "message": "If this email has administrator access, a password-reset link is on its way."
    }


@router.post("/api/reset-password")
async def reset_password(request: Request, user=Depends(admin)):
    data = await request.json()
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


def profile(v, session, provider=None, availability_by_volunteer=None):
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
    return {
        "id": str(v.id),
        "phone": v.phone,
        "first_name": parts[0],
        "last_name": parts[1] if len(parts) > 1 else "",
        "ministry": prefs.get("preferred_ministry", "Not set"),
        "status": "active" if v.status == "active" else "paused",
        "consent": v.sms_opt_in,
        "qualified": any(q.status == "verified" for q in quals),
        "background_check_until": background.expires_on.isoformat()
        if background and background.expires_on
        else None,
        "availability": prefs.get("availability_note")
        or (latest.raw_reply if latest else None)
        or "Not provided",
        "onboarding_stage": prefs.get("onboarding_stage", "not_started" if prefs.get("signup_source") == "sms" else "complete"),
        "can_start_text_setup": bool(isinstance(provider, MacMessagesProvider)
                                     and provider.allows(v.phone) and v.sms_opt_in and v.status == "active"
                                     and prefs.get("onboarding_stage") not in {"interests", "availability"}),
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


@router.get("/api/state")
def state(request: Request, user=Depends(admin), session=Depends(db)):
    state = request.app.state
    now = request.app.state.clock.now()
    roles = {r.id: r for r in session.scalars(select(m.Role)).all()}
    upcoming = session.scalars(
        select(m.Shift)
        .options(selectinload(m.Shift.event))
        .join(m.Event)
        .where(m.Event.starts_at >= now)
        .order_by(m.Event.starts_at)
        .limit(160)
    ).all()
    shift_ids = {s.id for s in upcoming}
    shifts = [
        {
            "id": str(s.id),
            "title": s.event.title,
            "role": roles[s.role_id].name,
            "ministry": roles[s.role_id].ministry,
            "starts_at": s.event.starts_at.isoformat(),
            "ends_at": s.event.ends_at.isoformat(),
            "required": 1,
            "event_id": str(s.event_id),
            "fill_policy": roles[s.role_id].fill_policy,
            "sensitive": bool(roles[s.role_id].required_qualifications),
        }
        for s in upcoming
    ]
    volunteers = session.scalars(select(m.Volunteer).options(selectinload(m.Volunteer.qualifications)).order_by(m.Volunteer.name)).all()
    latest_ids = select(func.max(m.Availability.id)).where(
        m.Availability.volunteer_id.in_([v.id for v in volunteers])).group_by(m.Availability.volunteer_id)
    availability = {a.volunteer_id: a for a in session.scalars(select(m.Availability).where(m.Availability.id.in_(latest_ids)))}
    profiles = [profile(v, session, request.app.state.provider, availability) for v in volunteers]
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
    message_query = select(m.Message)
    provider = request.app.state.provider
    if isinstance(provider, MacMessagesProvider):
        from sqlalchemy import or_
        delivery_now = request.app.state.mac_delivery_clock.now()
        conditions = []
        for phone, selected in provider.test_sessions.items():
            if selected.active(delivery_now):
                conditions.append((m.Message.phone == phone) & (
                    (m.Message.purpose == "test:"+selected.id) |
                    m.Message.provider_sid.startswith(selected.outbound_prefix)) &
                    (m.Message.created_at >= selected.starts_at) &
                    (m.Message.created_at < selected.expires_at))
        message_query = message_query.where(or_(*conditions) if conditions else False)
    msgs = session.scalars(message_query.order_by(m.Message.id.desc()).limit(200)).all()
    messages = [
        {
            "id": str(v.id),
            "phone": v.phone,
            "body": v.body,
            "direction": "inbound" if v.direction == "in" else "outbound",
            "status": "simulated"
            if v.provider_sid and v.provider_sid.startswith("MOCK")
            else v.status,
            "created_at": v.created_at.isoformat(),
        }
        for v in reversed(msgs)
    ]
    proposals = []
    by_id = {v.id: v for v in volunteers}
    proposal_query = select(m.Approval)
    if isinstance(state.provider, MacMessagesProvider):
        now = state.mac_delivery_clock.now()
        active = [(m.Approval.payload["phone"].as_string() == phone) &
                  (m.Approval.payload["session_id"].as_string() == selected.id) &
                  (m.Approval.requested_at >= selected.starts_at) & (m.Approval.requested_at < selected.expires_at)
                  for phone, selected in state.provider.test_sessions.items() if selected.active(now)]
        # Select provenance before loading JSON bodies; preserve explicitly simulated proposals.
        proposal_query = proposal_query.where(or_(m.Approval.payload["transport"].as_string() == "mock_or_twilio",
            (m.Approval.payload["transport"].as_string() == "mac_messages") & or_(*active) if active else False))
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
    if isinstance(state.provider, MacMessagesProvider):
        active_care = [(m.Escalation.related_ids["phone"].as_string() == phone) &
                       (m.Escalation.related_ids["session_id"].as_string() == selected.id) &
                       (m.Escalation.created_at >= selected.starts_at) & (m.Escalation.created_at < selected.expires_at)
                       for phone, selected in state.provider.test_sessions.items() if selected.active(now)]
        escalation_query = escalation_query.where(or_(m.Escalation.related_ids["transport"].as_string() == "mock_or_twilio",
            (m.Escalation.related_ids["transport"].as_string() == "mac_messages") & or_(*active_care) if active_care else False))
    escalations = [
        {
            "id": str(e.id),
            "category": e.category,
            "summary": e.summary,
            "severity": e.severity,
            "status": e.status,
        }
        for e in session.scalars(escalation_query).all()
    ]
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
    return {
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
    return profile(v, session)


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
    return profile(v, session)


@router.post("/api/volunteers/{volunteer_id}/text-setup")
def start_text_setup(request: Request, volunteer_id: int, user=Depends(admin), session=Depends(db)):
    """An authenticated coordinator starts setup; volunteers still only text."""
    state = request.app.state
    if not isinstance(state.provider, MacMessagesProvider):
        raise HTTPException(503, "Live texting is paused. Enable the test connection first.")
    if not state.settings.gloo_signup_replies or not PolicyStore(session).get("full_text_onboarding"):
        raise HTTPException(503, "Gloo text setup is not enabled.")
    volunteer = session.scalar(select(m.Volunteer).where(m.Volunteer.id == volunteer_id).with_for_update())
    if volunteer is None:
        raise HTTPException(404, "Volunteer not found.")
    if not state.provider.allows(volunteer.phone):
        raise HTTPException(403, "This volunteer is outside the enabled test phones.")
    selected = state.provider.test_sessions.get(volunteer.phone)
    if selected is None or not selected.active(state.mac_delivery_clock.now()):
        raise HTTPException(409, "Start an active test session for this volunteer before text setup.")
    if not volunteer.sms_opt_in or volunteer.status != "active":
        raise HTTPException(409, "The volunteer must first opt in by text and be active.")
    if volunteer.preferences.get("onboarding_stage") in {"interests", "availability"}:
        raise HTTPException(409, "Text setup is already in progress. Their next reply continues it.")
    from app.core.onboarding import start
    from app.web.admin_setup import owner
    session.info["mac_test_session"] = selected
    try:
        outcome = start(session, state.clock, SendGate(session, state.clock, state.provider), volunteer, state.gloo,
                        copy_owner=owner(user) if user.get("id") else None)
    except GlooUnavailableError:
        raise HTTPException(503, "Gloo could not compose the setup text. Nothing was sent; try again.")
    if not outcome.sent and not outcome.approval_id:
        raise HTTPException(409, outcome.reason or "Setup text is held by the texting rules. Try during sending hours.")
    session.flush()
    return {"delivery": "awaiting_confirmation" if outcome.approval_id else "queued_for_mac", "approval_id": outcome.approval_id, "message_id": outcome.message_id,
            "volunteer": profile(volunteer, session, state.provider)}


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
    if not volunteer.sms_opt_in or volunteer.status != "active":
        raise HTTPException(409, "The recipient must be active and have text consent.")
    provider = state.provider if isinstance(state.provider, MacMessagesProvider) else MockSMSProvider()
    if isinstance(provider, MacMessagesProvider):
        if not provider.allows(volunteer.phone):
            raise HTTPException(403, "This recipient is outside the enabled test phones.")
        selected = provider.test_sessions.get(volunteer.phone)
        if selected is None or not selected.active(state.mac_delivery_clock.now()):
            raise HTTPException(409, "An active approved test session is required before drafting this text.")
        session.info["mac_test_session"] = selected
    body_hash = hashlib.sha256(data["body"].encode()).hexdigest()
    key = f"admin-compose:{volunteer.id}:{request_id}" if request_id else None
    previous = session.get(m.Notification, key) if key else None
    if previous:
        if previous.detail.get("body_hash") != body_hash:
            raise HTTPException(409, "This text request already contains different words.")
        result = previous.detail.get("result")
        if result is None:
            raise HTTPException(409, "This text is being queued. Retry the same request shortly.")
        return result
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
    try:
        # No model rewrite: the typed body and roster phone are the exact review content.
        outcome = gate.send(body=data["body"], purpose="admin_reply", volunteer=volunteer)
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
        result = {"delivery": "queued_for_mac" if isinstance(provider, MacMessagesProvider) else "simulated",
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
    if state.settings.competition_confirmation_required and isinstance(state.provider, MacMessagesProvider):
        active_review = [(m.Approval.payload["phone"].as_string() == phone) &
                         (m.Approval.payload["session_id"].as_string() == selected.id) &
                         (m.Approval.requested_at >= selected.starts_at) & (m.Approval.requested_at < selected.expires_at)
                         for phone, selected in state.provider.test_sessions.items() if selected.active(state.mac_delivery_clock.now())]
        approval_query = approval_query.where(or_(m.Approval.payload["transport"].as_string() == "mock_or_twilio",
            (m.Approval.payload["transport"].as_string() == "mac_messages") & or_(*active_review) if active_review else False))
    a = session.scalar(approval_query)
    if a is None:
        raise HTTPException(404, "Approval not found.")
    if a.status != "pending":
        raise HTTPException(409, "This approval was already reviewed.")
    state = request.app.state
    # Only approvals originating in real Mac ingress may queue Mac replies.
    # Simulator/legacy approvals always retain simulated delivery.
    provider = (
        state.provider
        if isinstance(state.provider, MacMessagesProvider)
        and a.payload.get("transport") == "mac_messages"
        else MockSMSProvider()
    )
    gate = SendGate(session, state.clock, provider)
    ctx = FillContext(session, state.clock, provider, state.gloo)
    from app.core import confirmations
    try:
        if confirmations.enabled(session):
            data = await request.json()
            expected = data.get("content_hash") if isinstance(data, dict) else None
            if not isinstance(expected, str) or not expected:
                raise ValueError("Review the exact displayed action before approving or rejecting")
            notes = confirmations.decide(session, gate, a, approve=decision == "approve",
                actor=user["email"], expected=expected, now=state.clock.now(), ctx=ctx)
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
    return {
        "reviewed": True,
        "notes": notes,
        "mock_sms_count": len(provider.sent)
        if isinstance(provider, MockSMSProvider)
        else 0,
        "delivery": "queued_for_mac"
        if isinstance(provider, MacMessagesProvider)
        else "simulated",
    }


# Same public kit paths are used by the Cloudflare dashboard and legacy admin pages.
from app.web.brand import router as brand_router

router.include_router(brand_router)


PUBLIC_ASSETS = frozenset({
    "index.html", "app.js", "domain.js", "setup.js", "setup-domain.js", "style.css",
    "accessibility.js", "admin-readiness.js", "onboarding-copy-nav.js",
    "onboarding-copy.js", "onboarding-copy.html", "onboarding-copy.css",
    "onboarding-copy-defaults.json",
})


@router.get("/texty")
@router.get("/texty/{asset:path}")
def texty(asset: str = "index.html"):
    if asset not in PUBLIC_ASSETS:
        raise HTTPException(404)
    # JS uses /api endpoints, so local assets intentionally live at root too.
    return FileResponse(STATIC / asset)


@router.get("/app.js")
@router.get("/domain.js")
@router.get("/setup.js")
@router.get("/setup-domain.js")
@router.get("/style.css")
@router.get("/accessibility.js")
@router.get("/admin-readiness.js")
@router.get("/onboarding-copy-nav.js")
@router.get("/onboarding-copy.js")
@router.get("/onboarding-copy.html")
@router.get("/onboarding-copy.css")
@router.get("/onboarding-copy-defaults.json")
def root_asset(request: Request):
    return texty(request.url.path.lstrip("/"))
