"""Explicit opt-in test sessions; markers are routing labels, not credentials."""
import json
import re
import secrets
from dataclasses import dataclass
from datetime import datetime, timedelta

STOP_WORDS = {"STOP", "STOPALL", "UNSUBSCRIBE", "END", "QUIT"}


@dataclass(frozen=True)
class TestSession:
    id: str
    starts_at: datetime
    expires_at: datetime

    @property
    def prefix(self):
        return f"[TEXTY {self.id}] "

    @property
    def outbound_prefix(self):
        return f"MAC{self.id}:"

    def active(self, now):
        return self.starts_at <= now < self.expires_at


def parse_sessions(raw, phones):
    data = json.loads(raw) if isinstance(raw, str) and raw else raw or {}
    if not isinstance(data, dict) or not set(data) <= set(phones):
        raise ValueError("Test sessions must select configured demo phones only")
    result = {}
    for phone, spec in data.items():
        if not isinstance(spec, dict) or not isinstance(spec.get("id"), str) or not re.fullmatch(r"[0-9a-f]{32}", spec["id"]):
            raise ValueError("Each test session needs a fresh 32-character lowercase hex ID")
        try:
            start, end = datetime.fromisoformat(spec["starts_at"]), datetime.fromisoformat(spec["expires_at"])
        except (KeyError, TypeError, ValueError) as error:
            raise ValueError("Test session times must be ISO datetimes") from error
        if start.tzinfo is None or end.tzinfo is None or not timedelta(0) < end-start <= timedelta(hours=2):
            raise ValueError("Test sessions must be timezone-aware and last at most two hours")
        result[phone] = TestSession(spec["id"], start, end)
    if len({s.id for s in result.values()}) != len(result):
        raise ValueError("Each test phone needs its own session ID")
    return result


def permitted(session, session_id, body, now):
    return bool(session and secrets.compare_digest(session.id, session_id)
                and (session.active(now) or body.strip().upper() in STOP_WORDS))
