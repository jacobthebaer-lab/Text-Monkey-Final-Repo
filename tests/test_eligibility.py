"""Every hard rule in eligibility.py."""

from datetime import date, timedelta

from app.core import eligibility
from app.db import models as m
from tests.conftest import NOW


def test_eligible_when_no_requirements(session, make_volunteer, make_shift):
    vol = make_volunteer()
    shift = make_shift("usher")
    assert eligibility.check(session, vol, shift).eligible


def test_missing_qualification_blocks(session, make_volunteer, make_shift):
    vol = make_volunteer()
    shift = make_shift("nursery", required=("background_check", "child_safety_training"))
    result = eligibility.check(session, vol, shift)
    assert not result.eligible
    assert any("missing qualification: background_check" in r for r in result.reasons)


def test_pending_qualification_not_trusted(session, make_volunteer, make_shift):
    """Self-reported training doesn't count until an admin verifies it."""
    vol = make_volunteer(
        quals=[("background_check", "verified", None), ("child_safety_training", "pending", None)]
    )
    shift = make_shift("nursery", required=("background_check", "child_safety_training"))
    result = eligibility.check(session, vol, shift)
    assert not result.eligible
    assert any("not verified: child_safety_training" in r for r in result.reasons)


def test_expired_qualification_blocks(session, make_volunteer, make_shift):
    shift = make_shift("nursery", required=("background_check",))  # event on Oct 4
    expired = make_volunteer(quals=[("background_check", "verified", date(2026, 10, 3))])
    valid = make_volunteer(quals=[("background_check", "verified", date(2026, 10, 4))])
    assert not eligibility.check(session, expired, shift).eligible
    assert eligibility.check(session, valid, shift).eligible


def test_inactive_volunteer_blocks(session, make_volunteer, make_shift):
    vol = make_volunteer(status="inactive")
    assert not eligibility.check(session, vol, make_shift()).eligible


def test_double_booking_blocks(session, make_volunteer, make_shift, assign):
    vol = make_volunteer()
    other_shift = make_shift("greeter")  # same Sunday 9:00
    assign(vol, other_shift, status="confirmed")
    result = eligibility.check(session, vol, make_shift("usher"))
    assert not result.eligible
    assert any("already booked" in r for r in result.reasons)


def test_non_overlapping_or_cancelled_assignments_dont_block(
    session, make_volunteer, make_shift, assign
):
    vol = make_volunteer()
    same_time = make_shift("greeter")
    assign(vol, same_time, status="cancelled")  # cancelled: no conflict
    later = make_shift("greeter", starts=NOW + timedelta(days=3, hours=1))  # 11:00 service
    assign(vol, later, status="approved")  # doesn't overlap 9:00-10:15
    assert eligibility.check(session, vol, make_shift("usher")).eligible


def test_stated_unavailability_blocks(session, make_volunteer, make_shift):
    vol = make_volunteer()
    session.add(
        m.Availability(volunteer_id=vol.id, month="2026-10", unavailable_dates=["2026-10-04"])
    )
    session.flush()
    result = eligibility.check(session, vol, make_shift())
    assert not result.eligible
    assert any("unavailable" in r for r in result.reasons)


def test_available_list_without_date_blocks(session, make_volunteer, make_shift):
    vol = make_volunteer()
    session.add(
        m.Availability(volunteer_id=vol.id, month="2026-10", available_dates=["2026-10-11", "2026-10-18"])
    )
    session.flush()
    assert not eligibility.check(session, vol, make_shift()).eligible


def test_available_list_with_date_allows(session, make_volunteer, make_shift):
    vol = make_volunteer()
    session.add(
        m.Availability(volunteer_id=vol.id, month="2026-10", available_dates=["2026-10-04"])
    )
    session.flush()
    assert eligibility.check(session, vol, make_shift()).eligible
