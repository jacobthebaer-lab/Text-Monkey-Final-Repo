"""Gloo writes signup replies; deterministic code owns consent and delivery."""

import json
import re
from pathlib import Path

from app.config import get_settings
from app.llm.agent_loop import RunLogger
from app.llm.gloo_client import GlooUnavailableError

PROMPT = Path(__file__).resolve().parents[2] / "prompts" / "signup_reply.md"


def compose_signup_reply(session, clock, gloo, approved_message, required_phrases=()):
    settings = getattr(gloo, "settings", get_settings())
    if gloo is None or not settings.gloo_signup_replies:
        return approved_message
    log = RunLogger(session, clock, agent="signup_reply", trigger="Text signup response", model=settings.parser_model)
    response = gloo.create_response(
        model=settings.parser_model, instructions=PROMPT.read_text(),
        input=json.dumps({"approved_message": approved_message, "required_phrases": list(required_phrases)}),
    )
    log.add_usage(getattr(response, "usage", None))
    text = (getattr(response, "output_text", "") or "").strip()
    if (not text or len(text) > 600 or re.search(r"https?://|www\.", text, re.I)
            or any(phrase not in text for phrase in required_phrases)):
        log.close("invalid_reply")
        raise GlooUnavailableError("Gloo signup reply failed validation; no substitute was sent")
    log.close("reply_composed")
    return text
