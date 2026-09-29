"""Shared fixtures: in-memory DB, factories, a frozen clock, and a gated mock provider."""

import itertools
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

import pytest
from sqlalchemy import create_engine, select
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from app.clock import FakeClock
from app.core.send_gate import SendGate
from app.db import models as m
from app.db.models import Base
from app.sms.mock_provider import MockSMSProvider

DENVER = ZoneInfo("America/Denver")
# Thursday Oct 1 2026, 10:00 Denver — mid-morning, outside quiet hours.
NOW = datetime(2026, 10, 1, 10, 0, tzinfo=DENVER)


@pytest.fixture
def session():
    engine = create_engine(
        "sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool
    )
    Base.metadata.create_all(engine)
    with sessionmaker(bind=engine, expire_on_commit=False)() as s:
        yield s


@pytest.fixture
def clock():
    return FakeClock(NOW)


@pytest.fixture
def provider():
    return MockSMSProvider()


@pytest.fixture
def gate(session, clock, provider):
    return SendGate(session, clock, provider)


@pytest.fixture
def make_volunteer(session):
    seq = itertools.count(1)

    def _make(
        name=None,
        *,
        opt_in=True,
        status="active",
        quals=(),  # tuples of (type, status, expires_on-or-None)
        prefs=None,
        coordinator=False,
        pastor=False,
        created_at=NOW - timedelta(days=400),
    ):
        i = next(seq)
        vol = m.Volunteer(
            name=name or f"Test Person {i}",
            phone=f"+1555020{i:04d}",
            sms_opt_in=opt_in,
            status=status,
            is_coordinator=coordinator,
            is_pastor=pastor,
            preferences=prefs or {},
            created_at=created_at,
        )
        session.add(vol)
        session.flush()
        for q_type, q_status, expires_on in quals:
            session.add(
                m.Qualification(
                    volunteer_id=vol.id,
                    type=q_type,
                    status=q_status,
                    verified_by="Admin" if q_status == "verified" else None,
                    verified_at=NOW - timedelta(days=100) if q_status == "verified" else None,
                    expires_on=expires_on,
                )
            )
        session.flush()
        return vol

    return _make


@pytest.fixture
def make_shift(session):
    def _make(
        role_name="usher",
        *,
        required=(),
        criticality="standard",
        fill_policy="auto",
        starts=NOW + timedelta(days=3, hours=-1),  # Sunday Oct 4, 9:00
        minutes=75,
        title="Sunday Service 9:00",
    ):
        role = session.scalar(select(m.Role).where(m.Role.name == role_name))
        if role is None:
            role = m.Role(
                name=role_name,
                ministry="test",
                required_qualifications=list(required),
                criticality=criticality,
                fill_policy=fill_policy,
            )
            session.add(role)
            session.flush()
        event = m.Event(
            title=title, starts_at=starts, ends_at=starts + timedelta(minutes=minutes), status="scheduled"
        )
        session.add(event)
        session.flush()
        shift = m.Shift(event_id=event.id, role_id=role.id, slot_index=0)
        session.add(shift)
        session.flush()
        return shift

    return _make


@pytest.fixture
def assign(session):
    def _assign(volunteer, shift, status="approved"):
        row = m.Assignment(
            shift_id=shift.id,
            volunteer_id=volunteer.id,
            status=status,
            source="planner",
            created_at=NOW - timedelta(days=7),
            updated_at=NOW - timedelta(days=7),
        )
        session.add(row)
        session.flush()
        return row

    return _assign
