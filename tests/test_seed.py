"""Verify the synthetic seed contains every deliberate case from PLAN.md section 17."""

import json
from datetime import timedelta
from pathlib import Path

import pytest
from sqlalchemy import select

from app.db import models as m
from app.db.seed import DATA_DIR, SEED_ANCHOR, seed
from app.db.session import make_engine, make_session_factory, reset_db


@pytest.fixture(scope="module")
def session(tmp_path_factory):
    db_path = tmp_path_factory.mktemp("db") / "seed_test.db"
    engine = make_engine(f"sqlite:///{db_path}")
    reset_db(engine)
    with make_session_factory(engine)() as s:
        seed(s)
        yield s


def completed_assignments(session, volunteer_name, since=None, until=None):
    rows = (
        session.execute(
            select(m.Assignment, m.Event)
            .join(m.Shift, m.Assignment.shift_id == m.Shift.id)
            .join(m.Event, m.Shift.event_id == m.Event.id)
            .join(m.Volunteer, m.Assignment.volunteer_id == m.Volunteer.id)
            .where(m.Volunteer.name == volunteer_name)
        )
        .all()
    )
    out = []
    for assignment, event in rows:
        if since and event.starts_at < since:
            continue
        if until and event.starts_at >= until:
            continue
        out.append((assignment, event))
    return out


def test_roster_size_and_specials(session):
    volunteers = session.scalars(select(m.Volunteer)).all()
    assert len(volunteers) == 45
    assert sum(1 for v in volunteers if v.is_coordinator) == 1
    assert sum(1 for v in volunteers if v.is_pastor) == 1
    # Only obviously fake numbers — except demo volunteers overridden from
    # the local gitignored demo_phones.json (PLAN.md section 17).
    from app.db.seed import _demo_phone_overrides

    demo_names = set(_demo_phone_overrides())
    assert all(v.phone.startswith("+1555") for v in volunteers if v.name not in demo_names)
    assert sum(1 for v in volunteers if not v.sms_opt_in) == 1  # Olivia opted out


def test_sound_single_point_of_failure(session):
    """One person covers >50% of sound slots over the last 6 weeks."""
    sound = session.scalar(select(m.Role).where(m.Role.name == "sound"))
    window_start = SEED_ANCHOR - timedelta(weeks=6)
    rows = session.execute(
        select(m.Assignment.volunteer_id)
        .join(m.Shift, m.Assignment.shift_id == m.Shift.id)
        .join(m.Event, m.Shift.event_id == m.Event.id)
        .where(m.Shift.role_id == sound.id, m.Event.starts_at >= window_start, m.Event.starts_at < SEED_ANCHOR)
    ).all()
    assert rows, "sound history exists"
    by_vol: dict[int, int] = {}
    for (vid,) in rows:
        by_vol[vid] = by_vol.get(vid, 0) + 1
    top = max(by_vol.values())
    assert top / len(rows) > 0.5
    # And at most 2 people are sound-qualified at all.
    qualified = session.scalars(
        select(m.Qualification.volunteer_id).where(
            m.Qualification.type == "sound_training", m.Qualification.status == "verified"
        )
    ).all()
    assert len(set(qualified)) <= 2


def test_burnout_case(session):
    """Dana served at least 3x her stated max in the last full month."""
    dana = session.scalar(select(m.Volunteer).where(m.Volunteer.name == "Dana Whitcomb"))
    month_start = SEED_ANCHOR.replace(day=1) - timedelta(days=1)
    month_start = month_start.replace(day=1)  # first day of September
    served = completed_assignments(session, "Dana Whitcomb", since=month_start, until=SEED_ANCHOR)
    assert len(served) >= 3 * dana.preferences["max_per_month"]


def test_drop_off_case(session):
    """Frank served ~2x/month for 3 months, then nothing for 6+ weeks."""
    recent = completed_assignments(session, "Frank Miller", since=SEED_ANCHOR - timedelta(weeks=6))
    assert recent == []
    older = completed_assignments(session, "Frank Miller", until=SEED_ANCHOR - timedelta(weeks=6))
    months = {}
    for _, event in older:
        months.setdefault(event.starts_at.strftime("%Y-%m"), 0)
        months[event.starts_at.strftime("%Y-%m")] += 1
    assert sum(1 for count in months.values() if count >= 2) >= 3


def test_untapped_volunteers(session):
    for name in ("Tessa Nguyen", "Marcus Lee"):
        vol = session.scalar(select(m.Volunteer).where(m.Volunteer.name == name))
        assert vol.sms_opt_in
        assert vol.created_at <= SEED_ANCHOR - timedelta(days=30)
        assert completed_assignments(session, name) == []


def test_unused_skill_nurse(session):
    rosa = session.scalar(select(m.Volunteer).where(m.Volunteer.name == "Rosa Alvarez"))
    quals = {q.type: q.status for q in rosa.qualifications}
    assert quals.get("first_aid") == "verified"
    first_aid_serves = [
        (a, e) for a, e in completed_assignments(session, "Rosa Alvarez")
        if session.get(m.Shift, a.shift_id).role.name == "first aid"
    ]
    assert first_aid_serves == []


def test_expiring_background_checks(session):
    cutoff = (SEED_ANCHOR + timedelta(days=30)).date()
    expiring = session.scalars(
        select(m.Qualification).where(
            m.Qualification.type == "background_check",
            m.Qualification.status == "verified",
            m.Qualification.expires_on <= cutoff,
        )
    ).all()
    assert len(expiring) == 3


def test_pending_qualification_not_verified(session):
    pending = session.scalars(
        select(m.Qualification).where(m.Qualification.status == "pending")
    ).all()
    assert len(pending) == 1
    assert pending[0].type == "child_safety_training"
    assert pending[0].verified_at is None and pending[0].verified_by is None


def test_married_couple_serve_together(session):
    grace = session.scalar(select(m.Volunteer).where(m.Volunteer.name == "Grace Chen"))
    henry = session.scalar(select(m.Volunteer).where(m.Volunteer.name == "Henry Chen"))
    assert grace.preferences["serves_with_volunteer_id"] == henry.id
    assert henry.preferences["serves_with_volunteer_id"] == grace.id
    grace_events = {e.id for _, e in completed_assignments(session, "Grace Chen")}
    henry_events = {e.id for _, e in completed_assignments(session, "Henry Chen")}
    assert grace_events and grace_events == henry_events, "they always serve the same events"


def test_special_events_exist(session):
    festival = session.scalar(select(m.Event).where(m.Event.title == "Fall Festival"))
    assert festival is not None
    assert festival.event_type_id is None, "unknown event type triggers the admin question"
    assert festival.shifts == []

    christmas = session.scalars(select(m.Event).where(m.Event.title.like("Christmas Eve%"))).all()
    assert len(christmas) == 2
    assert all(e.starts_at > SEED_ANCHOR for e in christmas)

    assert session.scalar(select(m.Event).where(m.Event.title == "Food Drive")) is not None


def test_no_unqualified_assignments(session):
    """The hard rule holds even in seed data: nobody serves a role they aren't verified for."""
    rows = session.execute(
        select(m.Assignment, m.Shift, m.Event).join(m.Shift, m.Assignment.shift_id == m.Shift.id).join(
            m.Event, m.Shift.event_id == m.Event.id
        )
    ).all()
    assert rows
    for assignment, shift, event in rows:
        role = session.get(m.Role, shift.role_id)
        quals = {
            q.type: q for q in session.scalars(
                select(m.Qualification).where(m.Qualification.volunteer_id == assignment.volunteer_id)
            )
        }
        for required in role.required_qualifications:
            q = quals.get(required)
            assert q is not None and q.status == "verified", (
                f"{assignment.volunteer_id} lacks {required} for {role.name}"
            )
            if q.expires_on is not None:
                assert q.expires_on >= event.starts_at.date()


def test_upcoming_october_schedule_is_usable(session):
    """The demo needs upcoming approved assignments to cancel against."""
    nursery = session.scalar(select(m.Role).where(m.Role.name == "nursery"))
    rows = session.execute(
        select(m.Shift, m.Event)
        .join(m.Event, m.Shift.event_id == m.Event.id)
        .where(
            m.Shift.role_id == nursery.id,
            m.Event.starts_at > SEED_ANCHOR,
            m.Event.starts_at < SEED_ANCHOR + timedelta(days=30),
            m.Event.title.like("Sunday%"),
        )
    ).all()
    assert rows
    for shift, event in rows:
        statuses = [a.status for a in shift.assignments]
        assert statuses == ["approved"], f"nursery shift {shift.id} on {event.starts_at} not staffed"

    # At most one assignment per shift anywhere.
    from sqlalchemy import func

    dupes = session.execute(
        select(m.Assignment.shift_id).group_by(m.Assignment.shift_id).having(func.count() > 1)
    ).all()
    assert dupes == []


def test_policies_loaded(session):
    quiet = session.get(m.Policy, "quiet_hours")
    assert quiet.value["value"] == {"start": "21:00", "end": "07:00"}
    budget = session.get(m.Policy, "monthly_ask_budget_per_volunteer")
    assert budget.value["value"] == 4


def test_sample_texts_file():
    data = json.loads(Path(DATA_DIR / "sample_texts.json").read_text())
    texts = data["texts"]
    assert len(texts) >= 40
    assert any(t.get("sensitive") for t in texts)
    assert any(t["text"] == "STOP" for t in texts)
