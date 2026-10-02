"""Text signup requests are interpreted by Gloo and approved by a coordinator."""

from pathlib import Path
import re
from sqlalchemy import select
from app.db import models as m
from app.llm.parser import _extract_json, keyword_sensitive
from app.llm.gloo_client import GlooUnavailableError

PROMPT = Path(__file__).resolve().parents[2] / "prompts" / "signup.md"
PHONE = re.compile(r"^\+[1-9]\d{7,14}$")


def request_signup(session, clock, gloo, phone, body):
    if not PHONE.fullmatch(phone):
        return None
    # Exact control words bypass models and do not create an account request.
    if body.strip().upper() in {"STOP", "START", "HELP", "UNSUBSCRIBE", "END", "QUIT"}:
        return None
    existing = session.scalars(
        select(m.Approval).where(
            m.Approval.kind == "signup", m.Approval.status == "pending"
        )
    ).all()
    if any(a.payload.get("phone") == phone for a in existing):
        return "signup_pending"
    try:
        response = gloo.create_response(
            model=gloo.settings.parser_model,
            instructions=PROMPT.read_text(),
            input=body,
        )
        data = _extract_json(getattr(response, "output_text", "") or "") or {}
    except GlooUnavailableError:
        data = {}
    sensitive = data.get("sensitive") is True or keyword_sensitive(body)
    if sensitive:
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
        return "escalated_sensitive"
    first, last = data.get("first_name"), data.get("last_name")
    if data.get("signup") is not True or not all(
        isinstance(n, str) and 0 < len(n.strip()) <= 80 for n in (first, last)
    ):
        return None
    session.add(
        m.Approval(
            kind="signup",
            payload={
                "phone": phone,
                "first_name": first.strip(),
                "last_name": last.strip(),
                "sms_opt_in": False,
            },
            status="pending",
            requested_at=clock.now(),
        )
    )
    session.flush()
    return "signup_pending"


def approve_signup(session, clock, approval):
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
