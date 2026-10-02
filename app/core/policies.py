"""Policy access with safe defaults.

Policies live in the `policies` table (seeded from data/policies.json). If a
key is missing, the defaults here apply — the send gate must never fail open
because a row wasn't seeded.
"""

from datetime import datetime, time, timedelta
from zoneinfo import ZoneInfo

from sqlalchemy.orm import Session

from app.db.models import Policy

DEFAULTS: dict = {
    "full_text_onboarding": False,
    "outreach_cooldown_hours": 24,
    "church_name": "Cedar Hills Community Church",
    "church_timezone": "America/Denver",
    "quiet_hours": {"start": "21:00", "end": "07:00"},
    "urgent_quiet_hours": {"start": "21:30", "end": "06:30"},
    "monthly_ask_budget_per_volunteer": 4,
}


def _parse_time(text: str) -> time:
    hour, minute = text.split(":")
    return time(int(hour), int(minute))


class PolicyStore:
    def __init__(self, session: Session) -> None:
        self.session = session

    def get(self, key: str):
        row = self.session.get(Policy, key)
        if row is not None:
            return row.value["value"]
        return DEFAULTS[key]

    def church_name(self) -> str:
        return self.get("church_name")

    def church_tz(self) -> ZoneInfo:
        return ZoneInfo(self.get("church_timezone"))

    def quiet_hours(self) -> tuple[time, time]:
        raw = self.get("quiet_hours")
        return _parse_time(raw["start"]), _parse_time(raw["end"])

    def urgent_quiet_hours(self) -> tuple[time, time]:
        raw = self.get("urgent_quiet_hours")
        return _parse_time(raw["start"]), _parse_time(raw["end"])

    def ask_budget(self) -> int:
        return int(self.get("monthly_ask_budget_per_volunteer"))


def in_quiet_hours(now: datetime, start: time, end: time) -> bool:
    """True when `now` (local time) falls inside the quiet window.

    Quiet windows normally wrap midnight (21:00 -> 07:00).
    """
    t = now.time()
    if start > end:
        return t >= start or t < end
    return start <= t < end


def next_send_time(now: datetime, start: time, end: time) -> datetime:
    """Earliest moment after `now` outside the quiet window."""
    if not in_quiet_hours(now, start, end):
        return now
    opens_today = now.replace(hour=end.hour, minute=end.minute, second=0, microsecond=0)
    if now.time() < end:
        return opens_today
    return opens_today + timedelta(days=1)
