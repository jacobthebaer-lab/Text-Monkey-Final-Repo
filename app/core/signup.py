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
from app.core.signup_responder import compose_signup_reply
from app.core.care import escalate_sensitive

PROMPT = Path(__file__).resolve().parents[2] / "prompts" / "signup.md"
PHONE = re.compile(r"^\+[1-9]\d{7,14}$")
CONTROLS = {"STOP", "STOPALL", "START", "HELP", "UNSTOP", "UNSUBSCRIBE", "END", "QUIT"}


def request_signup(session, clock, gloo, phone, body, gate=None):
    if not PHONE.fullmatch(phone) or body.strip().upper() in CONTROLS:
        return None
    sensitive_rows = session.scalars(select(m.Escalation.related_ids).where(
        m.Escalation.category == "sensitive", m.Escalation.status.in_(("open", "acknowledged"))
    ))
    if any(e.get("phone") == phone for e in sensitive_rows):
        return "escalated_sensitive"
    existing = session.scalar(select(m.Volunteer).where(m.Volunteer.phone == phone))
    if existing:
        return (
            "signup_consent_pending"
            if existing.preferences.get("consent_pending")
            else "signup_complete"
        )
    # A short conversation lets JOIN followed by a name complete signup.
    from app.core.conversation import scope
    messages = session.scalars(
        scope(select(m.Message), session.info.get("mac_test_session"))
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
    if keyword_sensitive(body):
        escalate_sensitive(session, gate, None, body, clock.now(), phone=phone)
        logger.close("sensitive")
        return "escalated_sensitive"
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
        escalate_sensitive(session, gate, None, body, clock.now(), phone=phone, severity=data.get("severity"))
        logger.close("sensitive")
        return "escalated_sensitive"
    if data.get("signup") is not True:
        logger.close("not_signup")
        return None
    first, last = data.get("first_name"), data.get("last_name")
    if not all(isinstance(n, str) and 0 < len(n.strip()) <= 80 for n in (first, last)):
        if gate:
            gate.send(
                body=compose_signup_reply(session, clock, gloo,
                    "Welcome to Text Monkey! What is your first and last name? Reply STOP to stop or HELP for help.", ("first and last name", "STOP", "HELP"), phone=phone, signup_conversation=True),
                purpose="signup_reply",
                phone=phone,
            )
        logger.close("name_needed")
        return "signup_name_needed"
    from app.core.confirmations import enabled
    own_inputs = [msg["body"] for msg in conversation if msg["direction"] == "in"]
    explicit_join = any(re.match(r"^\s*(?:join\b|sign me up\b|i want to volunteer\b)", text, re.I) for text in own_inputs)
    explicit_name = re.fullmatch(r"\s*(?:(?:my name is|i am|i'm)\s+)?" + re.escape(first.strip()) + r"\s+" + re.escape(last.strip()) + r"[.!]?\s*", body, re.I)
    declined = re.search(r"\b(?:don't|do not|not|never)\s+(?:sign|join|volunteer)", body, re.I)
    if enabled(session) and (declined or not (explicit_join or explicit_name) or not all(re.search(r"\b" + re.escape(n.strip()) + r"\b", " ".join(own_inputs), re.I) for n in (first, last))):
        session.add(m.Escalation(category="unclear", severity="normal", summary="Signup identity needs human clarification; no roster record created.", related_ids={"phone": phone}, status="open", created_at=clock.now()))
        logger.close("human_review")
        return "signup_identity_review"
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
    from app.core.confirmations import authorize_sender_fields
    authorize_sender_fields(session, volunteer, {"name", "phone", "status", "sms_opt_in", "preferences", "is_coordinator", "is_pastor"})
    session.add(volunteer)
    session.flush()
    if gate:
        gate.send(
            body=compose_signup_reply(session, clock, gloo,
                f"Thanks, {first.strip()}! Reply YES to receive volunteer scheduling texts from Text Monkey. Message frequency varies; message/data rates may apply. Reply STOP to stop or HELP for help.",
                ("Reply YES", "Message frequency varies", "message/data rates may apply", "STOP", "HELP"), volunteer=volunteer, signup_conversation=True),
            purpose="signup_reply",
            volunteer=volunteer,
        )
    logger.close("consent_pending")
    return "signup_consent_pending"


def finish_signup(session, clock, gate, volunteer, body, gloo=None):
    """Only a pending signup can consume a consent reply; no role grants."""
    if not volunteer.preferences.get("consent_pending"):
        return None
    word = body.strip().upper()
    if keyword_sensitive(body):
        escalate_sensitive(session, gate, volunteer, body, clock.now())
        return "escalated_sensitive"
    if word in {"YES", "Y", "START", "UNSTOP"}:
        from app.core.send_gate import has_open_sensitive_escalation

        if has_open_sensitive_escalation(session, volunteer.id):
            return "escalated_sensitive"
        from app.core.confirmations import authorize_sender_fields
        authorize_sender_fields(session, volunteer, {"sms_opt_in", "status", "preferences"})
        volunteer.sms_opt_in = True
        volunteer.status = "active"
        volunteer.preferences = {
            **volunteer.preferences,
            "consent_pending": False,
            "consent_at": clock.now().isoformat(),
            "consent_source": "sms_reply",
        }
        from app.core.policies import PolicyStore
        if PolicyStore(session).get("full_text_onboarding"):
            from app.core.onboarding import start
            start(session, clock, gate, volunteer, gloo)
            return "onboarding_interests"
        gate.send(
            body=compose_signup_reply(session, clock, gloo,
                f"You’re signed up, {volunteer.name.split()[0]}! Text when you’re available or what you’d like to help with. We’ll confirm a shift before adding you.", volunteer=volunteer, signup_conversation=True),
            purpose="signup_reply",
            volunteer=volunteer,
        )
        return "signup_complete"
    if word in {"NO", "N"}:
        from app.core.confirmations import authorize_sender_fields
        authorize_sender_fields(session, volunteer, {"preferences"})
        volunteer.preferences = {**volunteer.preferences, "consent_pending": False}
        return "signup_declined"
    if word == "HELP":
        gate.send(
            body=compose_signup_reply(session, clock, gloo,
                "Text Monkey coordinates volunteer shifts by text. Reply YES to complete signup. Contact your ministry coordinator for other help.", ("Reply YES",), volunteer=volunteer, signup_conversation=True),
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
            body=compose_signup_reply(session, clock, gloo,
                "Reply YES to receive volunteer scheduling texts and finish signing up, or STOP to stop.", ("Reply YES", "STOP"), volunteer=volunteer, signup_conversation=True),
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
