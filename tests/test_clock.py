from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

import pytest

from app.clock import FakeClock, RealClock

DENVER = ZoneInfo("America/Denver")


def test_real_clock_returns_aware_church_time():
    now = RealClock("America/Denver").now()
    assert now.tzinfo is not None
    assert now.utcoffset() == datetime.now(DENVER).utcoffset()


def test_fake_clock_is_frozen_until_advanced():
    clock = FakeClock(datetime(2026, 10, 4, 9, 0, tzinfo=DENVER))
    assert clock.now() == clock.now()

    clock.advance(timedelta(minutes=20))
    assert clock.now() == datetime(2026, 10, 4, 9, 20, tzinfo=DENVER)


def test_fake_clock_assumes_church_timezone_for_naive_datetimes():
    clock = FakeClock(datetime(2026, 10, 4, 9, 0), timezone="America/Denver")
    assert clock.now().tzinfo is not None
    assert clock.now().utcoffset() == datetime(2026, 10, 4, 9, 0, tzinfo=DENVER).utcoffset()


def test_fake_clock_set_time():
    clock = FakeClock(datetime(2026, 10, 4, 9, 0, tzinfo=DENVER))
    clock.set_time(datetime(2026, 11, 1, 7, 30, tzinfo=DENVER))
    assert clock.now() == datetime(2026, 11, 1, 7, 30, tzinfo=DENVER)


def test_fake_clock_refuses_to_go_backwards():
    clock = FakeClock(datetime(2026, 10, 4, 9, 0, tzinfo=DENVER))
    with pytest.raises(ValueError):
        clock.advance(timedelta(minutes=-5))
