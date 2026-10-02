"""Texty JSON adapter over the existing scheduling core; Supabase admin auth.

The Texty simulator and its approval controls always use mock SMS. Twilio's
signed webhook remains separate and retains the original double send gate.
"""

import secrets
from functools import partial
from pathlib import Path

import httpx
from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import FileResponse
from sqlalchemy import select

from app.agents.fill_agent import FillContext
from app.core.inbound import decide_approval, handle_inbound
from app.core.send_gate import SendGate
from app.core.signup import PHONE
from app.db import models as m
from app.llm.parser import parse_inbound
from app.sms.mock_provider import MockSMSProvider
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
                403, "This backend is only reachable through the Texty dashboard."
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
    return {
        "name": "Texty",
        "connected": bool(
            s.supabase_url and s.supabase_publishable_key and allowed_emails(s)
        ),
        "provider": "gloo",
        "aiReady": bool(s.gloo_api_key),
        "liveSms": False,
        "allowTextSignup": s.allow_text_signup,
        "database": "postgres"
        if not s.database_url.startswith("sqlite")
        else "local SQLite",
        "simulatorOnly": True,
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


def profile(v, session):
    parts = v.name.split(" ", 1)
    prefs = v.preferences or {}
    latest = session.scalar(
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
    now = request.app.state.clock.now()
    roles = {r.id: r for r in session.scalars(select(m.Role)).all()}
    upcoming = session.scalars(
        select(m.Shift)
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
            "sensitive": bool(roles[s.role_id].required_qualifications),
        }
        for s in upcoming
    ]
    volunteers = session.scalars(select(m.Volunteer).order_by(m.Volunteer.name)).all()
    profiles = [profile(v, session) for v in volunteers]
    assignments = [
        {
            "id": str(a.id),
            "volunteer_id": str(a.volunteer_id),
            "shift_id": str(a.shift_id),
        }
        for a in session.scalars(
            select(m.Assignment).where(
                m.Assignment.status.in_(["approved", "confirmed", "proposed"])
            )
        ).all()
        if a.shift_id in shift_ids
    ]
    msgs = session.scalars(
        select(m.Message).order_by(m.Message.id.desc()).limit(200)
    ).all()
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
    for a in session.scalars(
        select(m.Approval).order_by(m.Approval.requested_at.desc())
    ).all():
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
                "status": a.status,
                "confidence": 1,
                "provider": "Gloo / scheduling core",
                "created_at": a.requested_at.isoformat(),
            }
        )
    escalations = [
        {
            "id": str(e.id),
            "category": e.category,
            "summary": e.summary,
            "severity": e.severity,
            "status": e.status,
        }
        for e in session.scalars(
            select(m.Escalation).where(m.Escalation.status == "open")
        ).all()
    ]
    return {
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


@router.post("/api/proposals/{proposal_id}/{decision}")
def review(
    request: Request,
    proposal_id: int,
    decision: str,
    user=Depends(admin),
    session=Depends(db),
):
    if decision not in {"approve", "reject"}:
        raise HTTPException(404)
    a = session.get(m.Approval, proposal_id)
    if a is None:
        raise HTTPException(404, "Approval not found.")
    if a.status != "pending":
        raise HTTPException(409, "This approval was already reviewed.")
    state = request.app.state
    provider = MockSMSProvider()
    gate = SendGate(session, state.clock, provider)
    ctx = FillContext(session, state.clock, provider, state.gloo)
    try:
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
    return {"reviewed": True, "notes": notes, "mock_sms_count": len(provider.sent)}


@router.get("/texty")
@router.get("/texty/{asset:path}")
def texty(asset: str = "index.html"):
    if asset not in {"index.html", "app.js", "domain.js", "style.css"}:
        raise HTTPException(404)
    # JS uses /api endpoints, so local assets intentionally live at root too.
    return FileResponse(STATIC / asset)


@router.get("/app.js")
@router.get("/domain.js")
@router.get("/style.css")
def root_asset(request: Request):
    return FileResponse(STATIC / request.url.path.lstrip("/"))
