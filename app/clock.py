"""Clock abstraction.

Everything that needs the current time asks a Clock, never datetime.now()
directly. RealClock is used in production; FakeClock is used in tests, evals,
and demo fast-forward (e.g., "+20 min" to trigger the next tranche).

All times are timezone-aware, in the church's timezone.
"""

from datetime import datetime, timedelta
from typing import Protocol
from zoneinfo import ZoneInfo


class Clock(Protocol):
    def now(self) -> datetime:
        """Current timezone-aware datetime in the church's timezone."""
        ...


class RealClock:
    def __init__(self, timezone: str = "America/Denver") -> None:
        self._tz = ZoneInfo(timezone)

    def now(self) -> datetime:
        return datetime.now(self._tz)


class FakeClock:
    """A clock that only moves when told to."""

    def __init__(self, start: datetime, timezone: str = "America/Denver") -> None:
        self._tz = ZoneInfo(timezone)
        self._now = start if start.tzinfo else start.replace(tzinfo=self._tz)

    def now(self) -> datetime:
        return self._now

    def advance(self, delta: timedelta) -> datetime:
        if delta < timedelta(0):
            raise ValueError("FakeClock cannot move backwards; use set_time instead")
        self._now += delta
        return self._now

    def set_time(self, when: datetime) -> datetime:
        self._now = when if when.tzinfo else when.replace(tzinfo=self._tz)
        return self._now
