"""Explicit opt-in test sessions; markers are routing labels, not credentials."""
import json
import re
import secrets
from dataclasses import dataclass, field
from datetime import datetime, timedelta

STOP_WORDS = {"STOP", "STOPALL", "UNSUBSCRIBE", "END", "QUIT"}


@dataclass(frozen=True)
class TestSession:
    id: str
    starts_at: datetime
    expires_at: datetime | None
    original_expires_at: datetime | None = field(default=None, kw_only=True)
    ongoing_since: datetime | None = field(default=None, kw_only=True)
    enrolled_at: datetime | None = field(default=None, kw_only=True)

    @property
    def prefix(self):
        return f"[TEXTY {self.id}] "

    @property
    def outbound_prefix(self):
        return f"MAC{self.id}:"

    def active(self, now):
        return self.starts_at <= now and (self.expires_at is None or now < self.expires_at) and (self.ongoing_since is None or self.ongoing_since <= now)

    def window(self, column, *, outbound=False):
        from sqlalchemy import true
        floor = self.ongoing_since if outbound and self.ongoing_since else self.starts_at
        return (column >= floor) & (column < self.expires_at if self.expires_at else true())

    def end_iso(self):
        return self.expires_at.isoformat() if self.expires_at else None

    def review_until(self, now):
        deadline=now+timedelta(hours=2)
        return min(deadline,self.expires_at) if self.expires_at else deadline

    def spec(self):
        value={'id':self.id,'starts_at':self.starts_at.isoformat(),'expires_at':self.end_iso()}
        if self.expires_at is None:
            value.update(until_stopped=True,ongoing_since=self.ongoing_since.isoformat())
            if self.enrolled_at is not None:
                value['enrolled_at']=self.enrolled_at.isoformat()
            else:
                value['original_expires_at']=self.original_expires_at.isoformat()
        return value


def parse_sessions(raw, phones, *, allow_ongoing=False):
    data = json.loads(raw) if isinstance(raw, str) and raw else raw or {}
    if not isinstance(data, dict) or not set(data) <= set(phones):
        raise ValueError("Test sessions must select configured demo phones only")
    result = {}
    for phone, spec in data.items():
        if not isinstance(spec, dict) or not isinstance(spec.get("id"), str) or not re.fullmatch(r"[0-9a-f]{32}", spec["id"]):
            raise ValueError("Each test session needs a fresh 32-character lowercase hex ID")
        try:
            start=datetime.fromisoformat(spec['starts_at'])
            ongoing=spec.get('until_stopped') is True
            enrolled=None
            if ongoing:
                if not allow_ongoing or spec.get('expires_at','missing') is not None:raise ValueError('Ongoing authority is explicit Mac-only')
                since=datetime.fromisoformat(spec['ongoing_since'])
                if since.tzinfo is None or since<start:raise ValueError('Invalid ongoing authorization time')
                if 'enrolled_at' in spec:
                    enrolled=datetime.fromisoformat(spec['enrolled_at'])
                    if enrolled.tzinfo is None or enrolled!=start or since!=start or 'original_expires_at' in spec:
                        raise ValueError('Fresh enrollment must begin at its explicit approval')
                    end=None
                else:
                    end=datetime.fromisoformat(spec['original_expires_at'])
            else:
                if 'enrolled_at' in spec:raise ValueError('Enrollment requires explicit ongoing authority')
                if 'until_stopped' in spec and spec['until_stopped'] is not False:raise ValueError('Invalid ongoing mode')
                end=datetime.fromisoformat(spec['expires_at'])
        except (KeyError, TypeError, ValueError) as error:
            raise ValueError("Test session times must be ISO datetimes") from error
        if start.tzinfo is None or (end is not None and (end.tzinfo is None or not timedelta(0) < end-start <= timedelta(hours=2))):
            raise ValueError("Test sessions must be timezone-aware and last at most two hours")
        result[phone] = TestSession(spec["id"], start, None if ongoing else end, original_expires_at=end if ongoing else None,
            ongoing_since=since if ongoing else None, enrolled_at=enrolled)
    if len({s.id for s in result.values()}) != len(result):
        raise ValueError("Each test phone needs its own session ID")
    return result


def permitted(session, session_id, body, now):
    return bool(session and secrets.compare_digest(session.id, session_id)
                and (session.active(now) or body.strip().upper() in STOP_WORDS))
