"""Gloo interprets signup; names and explicit consent are collected by text."""

from pathlib import Path
import json
import re
from datetime import timedelta
from sqlalchemy import select
from app.config import get_settings
from app.db import models as m
from app.llm.agent_loop import RunLogger
from app.llm.parser import _extract_json, keyword_sensitive
from app.llm.gloo_client import GlooUnavailableError

PROMPT = Path(__file__).resolve().parents[2] / "prompts" / "signup.md"
PHONE = re.compile(r"^\+[1-9]\d{7,14}$")
CONTROLS = {"STOP", "STOPALL", "START", "HELP", "UNSTOP", "UNSUBSCRIBE", "END", "QUIT"}


def request_signup(session, clock, gloo, phone, body, gate=None):
    if not PHONE.fullmatch(phone) or body.strip().upper() in CONTROLS:
        return None
    sensitive_rows = session.scalars(select(m.Escalation).where(
        m.Escalation.category == "sensitive", m.Escalation.status.in_(("open", "acknowledged"))
    ))
    if any(e.related_ids.get("phone") == phone for e in sensitive_rows):
        return "escalated_sensitive"
    existing = session.scalar(select(m.Volunteer).where(m.Volunteer.phone == phone))
    if existing:
        return (
            "signup_consent_pending"
            if existing.preferences.get("consent_pending")
            else "signup_complete"
        )
    # A short conversation lets JOIN followed by a name complete signup.
    messages = session.scalars(
        select(m.Message)
        .where(
            m.Message.phone == phone,
            m.Message.created_at >= clock.now() - timedelta(hours=24),
        )
        .order_by(m.Message.id.desc())
        .limit(8)
    ).all()
    conversation = [
        {"direction": msg.direction, "body": msg.body} for msg in reversed(messages)
    ]
    if not conversation or conversation[-1]["body"] != body:
        conversation.append({"direction": "in", "body": body})
    logger = RunLogger(
        session,
        clock,
        agent="signup",
        trigger="SMS signup",
        model=getattr(gloo, "settings", get_settings()).parser_model,
    )
    try:
        response = gloo.create_response(
            model=getattr(gloo, "settings", get_settings()).parser_model,
            instructions=PROMPT.read_text(),
            input=json.dumps(conversation),
        )
        logger.add_usage(getattr(response, "usage", None))
        data = _extract_json(getattr(response, "output_text", "") or "") or {}
    except GlooUnavailableError:
        session.add(
            m.Escalation(
                category="system_error",
                severity="normal",
                summary="Gloo could not process text signup.",
                related_ids={"phone": phone},
                status="open",
                created_at=clock.now(),
            )
        )
        logger.close("gloo_unavailable")
        return "escalated_signup"
    if data.get("sensitive") is True or keyword_sensitive(body):
        session.add(
            m.Escalation(
                category="sensitive",
                severity="normal",
                summary="Unknown sender needs a human review; see incoming message.",
                related_ids={"phone": phone},
                status="open",
                created_at=clock.now(),
            )
        )
        logger.close("sensitive")
        return "escalated_sensitive"
    if data.get("signup") is not True:
        logger.close("not_signup")
        return None
    first, last = data.get("first_name"), data.get("last_name")
    if not all(isinstance(n, str) and 0 < len(n.strip()) <= 80 for n in (first, last)):
        if gate:
            gate.send(
                body="Welcome to Texty! What is your first and last name? Reply STOP to stop.",
                purpose="signup_reply",
                phone=phone,
            )
        logger.close("name_needed")
        return "signup_name_needed"
    volunteer = m.Volunteer(
        name=f"{first.strip()} {last.strip()}",
        phone=phone,
        sms_opt_in=False,
        status="inactive",
        is_coordinator=False,
        is_pastor=False,
        preferences={"signup_source": "sms", "consent_pending": True},
        created_at=clock.now(),
    )
    session.add(volunteer)
    session.flush()
    if gate:
        gate.send(
            body=f"Thanks, {first.strip()}! Reply YES to receive volunteer scheduling texts from Texty. Message frequency varies; message/data rates may apply. Reply STOP to stop or HELP for help.",
            purpose="signup_reply",
            volunteer=volunteer,
        )
    logger.close("consent_pending")
    return "signup_consent_pending"


def finish_signup(session, clock, gate, volunteer, body):
    """Only a pending signup can consume a consent reply; no role grants."""
    if not volunteer.preferences.get("consent_pending"):
        return None
    word = body.strip().upper()
    if keyword_sensitive(body):
        session.add(
            m.Escalation(
                category="sensitive",
                severity="normal",
                summary="Signup sender needs a human review.",
                related_ids={"volunteer_id": volunteer.id},
                status="open",
                created_at=clock.now(),
            )
        )
        return "escalated_sensitive"
    if word in {"YES", "Y", "START", "UNSTOP"}:
        from app.core.send_gate import has_open_sensitive_escalation

        if has_open_sensitive_escalation(session, volunteer.id):
            return "escalated_sensitive"
        volunteer.sms_opt_in = True
        volunteer.status = "active"
        volunteer.preferences = {
            **volunteer.preferences,
            "consent_pending": False,
            "consent_at": clock.now().isoformat(),
            "consent_source": "sms_reply",
        }
        gate.send(
            body=f"You’re signed up, {volunteer.name.split()[0]}! Text when you’re available or what you’d like to help with. We’ll confirm a shift before adding you. Reply STOP to stop or HELP for help.",
            purpose="signup_reply",
            volunteer=volunteer,
        )
        return "signup_complete"
    if word in {"NO", "N"}:
        volunteer.preferences = {**volunteer.preferences, "consent_pending": False}
        return "signup_declined"
    if word == "HELP":
        gate.send(
            body="Texty coordinates volunteer shifts by text. Reply YES to complete signup or STOP to stop. Contact your ministry coordinator for other help.",
            purpose="signup_reply",
            volunteer=volunteer,
        )
        return "signup_help"
    # Avoid messaging someone who stopped the signup.
    stopped = session.scalar(
        select(m.Message.id).where(
            m.Message.phone == volunteer.phone,
            m.Message.direction == "in",
            m.Message.body.in_(["STOP", "STOPALL", "UNSUBSCRIBE", "QUIT", "END"]),
        )
    )
    if not stopped:
        gate.send(
            body="Reply YES to receive volunteer scheduling texts and finish signing up, or STOP to stop.",
            purpose="signup_reply",
            volunteer=volunteer,
        )
    return "signup_consent_pending"


def approve_signup(session, clock, approval):
    """Compatibility for signup approvals created before text-only onboarding."""
    data = approval.payload
    if session.scalar(select(m.Volunteer).where(m.Volunteer.phone == data["phone"])):
        raise ValueError("A volunteer already uses this phone.")
    volunteer = m.Volunteer(
        name=f"{data['first_name']} {data['last_name']}",
        phone=data["phone"],
        sms_opt_in=False,
        status="inactive",
        is_coordinator=False,
        is_pastor=False,
        preferences={"signup_source": "sms", "consent_pending": True},
        created_at=clock.now(),
    )
    session.add(volunteer)
    session.flush()
    return volunteer
