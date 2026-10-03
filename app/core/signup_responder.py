"""Gloo writes signup replies; deterministic code owns consent and delivery."""

import json
import re
from pathlib import Path
from datetime import timedelta
from sqlalchemy import select
from app.db import models as m

from app.config import get_settings
from app.llm.agent_loop import RunLogger
from app.llm.gloo_client import GlooUnavailableError

PROMPT = Path(__file__).resolve().parents[2] / "prompts" / "signup_reply.md"


def _without_command_footer(text):
    """Remove known trailing notices after consent, preserving the main reply."""
    return re.sub(
        r"(?:^|[,;]?\s+or\s+|\s+)(?:(?:Reply|Text)\s+)?\bSTOP\b(?:\s+to\s+(?:stop|unsubscribe|opt[ -]?out))?"
        r"(?:\s+or\s+HELP(?:\s+for\s+help)?)?[.!]?\s*$",
        "", text, flags=re.I,
    ).rstrip()


def _without_monkey_emoji(text):
    return re.sub(r"[🐵🐒]\ufe0f?", "", text).strip()


def _signup_style(text, signup_conversation):
    text = _without_monkey_emoji(text).replace("Texty", "Text Monkey")
    return f"{text} 🐒" if signup_conversation else text


def compose_signup_reply(session, clock, gloo, approved_message, required_phrases=(), *, volunteer=None, phone=None, signup_conversation=False):
    approved_message = _without_monkey_emoji(approved_message)
    include_command_notice = volunteer is None or not volunteer.sms_opt_in
    recipient = phone or (volunteer.phone if volunteer is not None else None)
    settings = getattr(gloo, "settings", get_settings())
    selected = session.info.get("mac_test_session")
    if settings.sms_provider in {"mac_messages", "google_voice"} and settings.mac_bridge_enabled:
        from app.integrations.test_sessions import parse_sessions
        from app.sms.mac_provider import demo_phones
        selected = parse_sessions(settings.mac_test_sessions, demo_phones(settings.mac_demo_phones)).get(recipient)
        if selected is None or not selected.active(clock.now()):
            raise GlooUnavailableError("Reply needs an active recipient test session before reading history")
    if include_command_notice and recipient:
        from app.core.conversation import scope
        previous = session.scalar(scope(select(m.Message.id), selected).where(
            m.Message.phone == recipient, m.Message.direction == "out").limit(1))
        include_command_notice = previous is None
    if not include_command_notice:
        approved_message = _without_command_footer(approved_message)
        required_phrases = tuple(p for p in required_phrases if p not in {"STOP", "HELP"})
    if gloo is None or not settings.gloo_signup_replies:
        rendered = _signup_style(approved_message, signup_conversation)
        if signup_conversation and (not _without_monkey_emoji(rendered) or len(rendered) > 600):
            raise GlooUnavailableError("Signup reply exceeds the message limit")
        return rendered
    log = RunLogger(session, clock, agent="signup_reply", trigger="Text signup response", model=settings.parser_model)
    facts = {"approved_message": approved_message, "required_phrases": list(required_phrases),
             "include_command_notice": include_command_notice,
             "signup_conversation": signup_conversation, "product_name": "Text Monkey"}
    if volunteer is not None:
        from app.core.conversation import scope
        recent = session.scalars(scope(select(m.Message), selected).where(
            m.Message.phone == volunteer.phone, m.Message.volunteer_id == volunteer.id,
            m.Message.created_at >= clock.now()-timedelta(hours=24),
        ).order_by(m.Message.id.desc()).limit(8)).all()
        facts.update(sender={"name": volunteer.name, "volunteer_id": volunteer.id},
                     recent_messages=[{"direction": row.direction, "body": row.body}
                                      for row in reversed(recent)])
    try:
        response = gloo.create_response(
            model=settings.parser_model, instructions=PROMPT.read_text(),
            input=json.dumps(facts),
        )
    except GlooUnavailableError:
        log.close("gloo_unavailable")
        raise
    log.add_usage(getattr(response, "usage", None))
    text = _without_monkey_emoji(getattr(response, "output_text", "") or "")
    if not include_command_notice:
        text = _without_command_footer(text)
        if re.search(r"\b(?:STOP|HELP)\b", text):
            log.close("invalid_reply")
            raise GlooUnavailableError("Gloo repeated command guidance after consent")
    if re.search(r"\breply\s+(?:YES|NO|Y|N)\b", text, re.I) and not re.search(r"\breply\s+(?:YES|NO|Y|N)\b", approved_message, re.I):
        log.close("invalid_reply")
        raise GlooUnavailableError("Gloo added an RSVP instruction without an approved offer")
    text = _signup_style(text, signup_conversation)
    if (not _without_monkey_emoji(text) or len(text) > 600 or re.search(r"https?://|www\.", text, re.I)
            or re.search(r"[\U0001F000-\U0001FAFF\u2600-\u27BF\u23F0-\u23FF]", _without_monkey_emoji(text))
            or any(phrase not in text for phrase in required_phrases)):
        log.close("invalid_reply")
        raise GlooUnavailableError("Gloo signup reply failed validation; no substitute was sent")
    log.close("reply_composed")
    return text
