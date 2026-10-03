"""Inbound message classifier (PLAN.md section 8.5).

The model classifies; code validates. Invalid JSON gets one retry, then the
message is treated as unclear. A keyword backstop marks sensitive messages
even when the model misses them — the backstop can only ever ADD sensitivity,
never remove it.
"""

import json
import re
from dataclasses import dataclass, field
from pathlib import Path

from app.config import get_settings
from app.llm.gloo_client import GlooClient, GlooUnavailableError

PROMPT_PATH = Path(__file__).resolve().parents[2] / "prompts" / "parser.md"

INTENTS = {
    "cancel", "accept", "decline", "partial", "availability",
    "question", "confirm", "other", "unclear",
}

# Backstop keyword list lives in code (PLAN.md section 8.4). Word-boundary
# regexes to avoid false hits inside other words.
SENSITIVE_PATTERNS = [
    re.compile(p, re.IGNORECASE)
    for p in (
        r"\bhospital\b", r"\bhospice\b", r"\bicu\b", r"\ber\b", r"\bdied\b",
        r"\bdeath\b", r"\bpassed away\b", r"\bfuneral\b", r"\baccident\b",
        r"\bemergency\b", r"\bsurgery\b", r"\bcancer\b", r"\bmiscarriage\b",
        r"\bnot doing well\b", r"\bstruggling\b", r"\bdepressed\b",
        r"\bhurt myself\b", r"\bhurting myself\b", r"\bsuicide\b", r"\bsuicidal\b",
        r"\bkill myself\b", r"\bself[- ]harm\b", r"\bwant to die\b", r"\bend it all\b",
    )
]

SELF_HARM_PATTERNS = [
    re.compile(p, re.IGNORECASE)
    for p in (
        r"\bhurt myself\b", r"\bhurting myself\b", r"\bsuicide\b", r"\bsuicidal\b",
        r"\bkill myself\b", r"\bself[- ]harm\b", r"\bwant to die\b", r"\bend it all\b",
    )
]


@dataclass
class ParsedMessage:
    intent: str = "unclear"
    shift_hint: str | None = None
    dates: list = field(default_factory=list)
    partial_window: str | None = None
    sensitive: bool = False
    severity: str = "normal"
    confidence: float = 0.0
    parse_error: bool = False
    raw: dict | None = None


def keyword_sensitive(text: str) -> bool:
    return any(p.search(text) for p in SENSITIVE_PATTERNS)


def keyword_self_harm(text: str) -> bool:
    return any(p.search(text) for p in SELF_HARM_PATTERNS)


def load_prompt() -> str:
    return PROMPT_PATH.read_text()


def _extract_json(text: str) -> dict | None:
    """Pull the first JSON object out of the model output, fences and all."""
    start, end = text.find("{"), text.rfind("}")
    if start == -1 or end <= start:
        return None
    try:
        data = json.loads(text[start : end + 1])
    except json.JSONDecodeError:
        return None
    return data if isinstance(data, dict) else None


def _validate(data: dict) -> ParsedMessage | None:
    """Schema check in code; returns None when the shape is unusable."""
    intent = data.get("intent")
    if intent not in INTENTS:
        return None
    try:
        confidence = float(data.get("confidence", 0.0))
    except (TypeError, ValueError):
        return None
    if not 0.0 <= confidence <= 1.0:
        return None
    severity = data.get("severity", "normal")
    if severity not in ("normal", "urgent"):
        severity = "normal"
    dates = data.get("dates") or []
    if not isinstance(dates, list):
        dates = [str(dates)]
    return ParsedMessage(
        intent=intent,
        shift_hint=data.get("shift_hint") or None,
        dates=[str(d) for d in dates],
        partial_window=data.get("partial_window") or None,
        sensitive=bool(data.get("sensitive", False)),
        severity=severity,
        confidence=confidence,
        raw=data,
    )


def _apply_backstop(parsed: ParsedMessage, text: str) -> ParsedMessage:
    if keyword_sensitive(text):
        parsed.sensitive = True
    if keyword_self_harm(text):
        parsed.severity = "urgent"
    # Guarded model refusals must not erase an explicit logistical cancellation.
    # This only recognizes a direct first-person statement; it never interprets
    # the care issue and it never lowers the pastoral/sensitive block.
    if parsed.parse_error and parsed.sensitive and re.search(
        r"\bi\s+(?:can't|cant|cannot|won't|will not)\s+(?:come|attend|serve|make it)\b", text, re.I
    ) and not re.search(r"\b(?:if|maybe|might|not sure)\b|\?", text, re.I):
        parsed.intent = "cancel"
        parsed.confidence = 1.0
        parsed.parse_error = False
        parsed.raw = {"classification_source": "explicit_sensitive_cancel_backstop"}
    return parsed


def parse_inbound(gloo: GlooClient, text: str) -> ParsedMessage:
    """Classify one inbound SMS. Never raises: failures come back as unclear."""
    if keyword_sensitive(text):
        # Do not export recognized care/health details just to discover a hold.
        # The local backstop can retain an explicit cancellation; everything else
        # stays for internal human review, without model interpretation.
        return _apply_backstop(ParsedMessage(parse_error=True,
            raw={'classification_source': 'local_sensitive_privacy_hold'}), text)
    settings = get_settings()
    instructions = load_prompt()

    for _attempt in range(2):  # invalid JSON gets exactly one retry
        try:
            response = gloo.create_response(
                model=settings.parser_model, instructions=instructions, input=text
            )
        except GlooUnavailableError:
            return _apply_backstop(ParsedMessage(parse_error=True), text)
        data = _extract_json(getattr(response, "output_text", "") or "")
        if data is not None:
            parsed = _validate(data)
            if parsed is not None:
                return _apply_backstop(parsed, text)

    return _apply_backstop(ParsedMessage(parse_error=True), text)
