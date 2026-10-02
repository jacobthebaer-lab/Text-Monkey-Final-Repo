"""Phase 7: solver, validator, availability parsing, planning flow, reminders.

Done-when: a month planned from seed data with >= 90% fill and zero hard-rule
violations; unknown event triggers the admin question.
"""

import json
from datetime import datetime, timedelta
from pathlib import Path
from types import SimpleNamespace
from zoneinfo import ZoneInfo

import pytest
from sqlalchemy import select

from app import jobs
from app.agents import planning_agent
from app.agents.fill_agent import FillContext
from app.clock import FakeClock
from app.core import availability as avail
from app.core import scheduler
from app.core.inbound import handle_inbound
from app.db import models as m
from app.db.seed import SEED_ANCHOR, seed
from app.db.session import make_engine, make_session_factory, reset_db
from app.llm.gloo_client import GlooUnavailableError
from app.llm.parser import ParsedMessage
from app.sms.mock_provider import MockSMSProvider

DENVER = ZoneInfo("America/Denver")


class QuietReviewGloo:
    """Planning review that makes no swaps, just summarizes."""

    def create_response(self, *, model, input, instructions=None, tools=None, **kwargs):
        return SimpleNamespace(
            output=[SimpleNamespace(type="message")],
            output_text="Draft looks reasonable; remaining gaps are kids roles with no qualified people left.",
            usage=SimpleNamespace(input_tokens=30, output_tokens=12),
        )


class DownGloo:
    def create_response(self, **kwargs):
        raise GlooUnavailableError("down")


@pytest.fixture(scope="module")
def seeded():
    """One seeded DB shared by the module; each test uses its own ctx."""
    engine = make_engine("sqlite://")
    reset_db(engine)
    session = make_session_factory(engine)()
    seed(session)
    clock = FakeClock(SEED_ANCHOR)
    provider = MockSMSProvider()
    ctx = FillContext(session, clock, provider, QuietReviewGloo(),
                      log_dir=Path("/tmp") / "servfrictionless-test-logs")
    return ctx


def parser_returning(**kwargs):
    defaults = dict(intent="other", confidence=0.95)
    defaults.update(kwargs)
    return lambda text: ParsedMessage(**defaults)


# --- availability parsing (pure) ------------------------------------------------


class TestAvailabilityParsing:
    def test_month_sundays_nov_2026(self):
        sundays = avail.month_sundays("2026-11")
        assert [d.day for d in sundays] == [1, 8, 15, 22, 29]

    def test_ordinals(self, seeded):
        vol = seeded.session.scalar(select(m.Volunteer).where(m.Volunteer.name == "Leah Simmons"))
        available, unavailable = avail.interpret_reply(seeded.session, vol, "2nd and 4th", "2026-11", SEED_ANCHOR)
        assert [d.day for d in available] == [8, 22]
        assert unavailable == []

    def test_first_and_third_words(self, seeded):
        vol = seeded.session.scalar(select(m.Volunteer).where(m.Volunteer.name == "Leah Simmons"))
        available, _ = avail.interpret_reply(
            seeded.session, vol, "I can do first and third sundays", "2026-11", SEED_ANCHOR
        )
        assert [d.day for d in available] == [1, 15]

    def test_not_this_month(self, seeded):
        vol = seeded.session.scalar(select(m.Volunteer).where(m.Volunteer.name == "Leah Simmons"))
        available, unavailable = avail.interpret_reply(seeded.session, vol, "not this month", "2026-11", SEED_ANCHOR)
        assert available == []
        assert len(unavailable) >= 5  # every November event date

    def test_except_date(self, seeded):
        vol = seeded.session.scalar(select(m.Volunteer).where(m.Volunteer.name == "Leah Simmons"))
        available, unavailable = avail.interpret_reply(
            seeded.session, vol, "all sundays work except the 15th", "2026-11", SEED_ANCHOR
        )
        assert available == []
        assert [d.day for d in unavailable] == [15]

    def test_out_of_town_date(self, seeded):
        vol = seeded.session.scalar(select(m.Volunteer).where(m.Volunteer.name == "Leah Simmons"))
        _, unavailable = avail.interpret_reply(
            seeded.session, vol, "we're out of town nov 8", "2026-11", SEED_ANCHOR
        )
        assert [d.day for d in unavailable] == [8]

    def test_same_as_usual_infers_pattern(self, seeded):
        # Grace Chen serves 1st/3rd Sundays at 11:00 in the seed history.
        vol = seeded.session.scalar(select(m.Volunteer).where(m.Volunteer.name == "Grace Chen"))
        available, _ = avail.interpret_reply(seeded.session, vol, "same as usual", "2026-11", SEED_ANCHOR)
        assert [d.day for d in available] == [1, 15]

    def test_uninterpretable_is_fully_available(self, seeded):
        vol = seeded.session.scalar(select(m.Volunteer).where(m.Volunteer.name == "Leah Simmons"))
        assert avail.interpret_reply(seeded.session, vol, "only mornings", "2026-11", SEED_ANCHOR) == ([], [])


# --- the full planning flow -----------------------------------------------------


def test_planning_flow_end_to_end(seeded):
    ctx = seeded
    session, provider = ctx.session, ctx.provider
    coordinator = session.scalar(select(m.Volunteer).where(m.Volunteer.is_coordinator))

    # Start planning October: Fall Festival has no event type -> admin question.
    planning_agent.start_planning(ctx, "2026-10")
    unknown = session.scalar(select(m.Escalation).where(m.Escalation.category == "unknown_event"))
    assert unknown is not None and "Fall Festival" in unknown.summary
    assert any("Fall Festival" in s.body for s in provider.sent_to(coordinator.phone))
    # Running again doesn't duplicate the escalation.
    planning_agent.start_planning(ctx, "2026-10")
    assert len(session.scalars(select(m.Escalation).where(m.Escalation.category == "unknown_event")).all()) == 1

    # Now plan November properly.
    result = planning_agent.start_planning(ctx, "2026-11")
    assert result["availability_asks"] > 30  # all active opted-in volunteers

    # A few availability replies arrive through the normal inbound path.
    leah = session.scalar(select(m.Volunteer).where(m.Volunteer.name == "Leah Simmons"))
    handle_inbound(session, ctx.clock, provider, leah.phone, "2nd and 4th",
                   parser_returning(intent="availability", confidence=0.95), ctx=ctx)
    row = session.scalar(select(m.Availability).where(m.Availability.volunteer_id == leah.id))
    assert row.month == "2026-11" and len(row.available_dates) == 2

    nina = session.scalar(select(m.Volunteer).where(m.Volunteer.name == "Nina Petrov"))
    handle_inbound(session, ctx.clock, provider, nina.phone, "not this month",
                   parser_returning(intent="availability", confidence=0.95), ctx=ctx)

    # Build the draft: solver + validator + (quiet) review + publish approval.
    validation = planning_agent.build_draft(ctx, "2026-11")

    # DONE-WHEN: >= 90% fill, zero hard-rule violations.
    assert validation.violations == []
    assert validation.fill_pct >= 90.0

    # Nina is nowhere on the draft; Leah only on her stated Sundays.
    # (auto-publish already flipped planner assignments to approved)
    session.expire_all()
    november = scheduler._month_shifts(session, "2026-11", DENVER)
    checked = 0
    for shift in november:
        for a in shift.assignments:
            if a.status not in ("proposed", "approved") or a.source != "planner":
                continue
            checked += 1
            assert a.volunteer_id != nina.id
            if a.volunteer_id == leah.id:
                assert shift.event.starts_at.astimezone(DENVER).day in (8, 22)
    assert checked >= 100

    # Fully automated publish: approval row is an audit record, auto-approved,
    # assignments approved and confirmation texts out with no human step.
    approval = session.scalar(select(m.Approval).where(m.Approval.kind == "publish_schedule"))
    assert approval is not None and approval.status == "approved"
    assert approval.decided_by == "auto-publish"
    assert any("schedule published" in s.body for s in provider.sent_to(coordinator.phone))
    session.expire_all()
    approved = [
        a for s in november for a in s.assignments if a.status == "approved" and a.source == "planner"
    ]
    assert len(approved) >= 100
    assert session.get(m.Policy, planning_agent.PLANNING_STATE_KEY).value["value"] == {}

    # Solver respects the hard rule everywhere (independent re-check).
    revalidation = scheduler.validate_month(session, "2026-11")
    assert revalidation.violations == []


def test_availability_nudge_after_three_days():
    engine = make_engine("sqlite://")
    reset_db(engine)
    session = make_session_factory(engine)()
    seed(session)
    clock = FakeClock(SEED_ANCHOR)
    provider = MockSMSProvider()
    ctx = FillContext(session, clock, provider, QuietReviewGloo(), log_dir=Path("/tmp/sf-logs"))

    planning_agent.start_planning(ctx, "2026-11")
    leah = session.scalar(select(m.Volunteer).where(m.Volunteer.name == "Leah Simmons"))
    handle_inbound(session, clock, provider, leah.phone, "2nd and 4th",
                   parser_returning(intent="availability", confidence=0.95), ctx=ctx)
    asks_before = len(provider.sent_to(leah.phone))

    assert planning_agent.nudge_nonresponders(ctx) == 0  # too early
    clock.advance(timedelta(days=3, hours=1))
    nudged = planning_agent.nudge_nonresponders(ctx)
    assert nudged > 20
    assert len(provider.sent_to(leah.phone)) == asks_before  # she responded: no nudge
    assert planning_agent.nudge_nonresponders(ctx) == 0  # one nudge only


def test_gloo_down_review_skipped_never_guesses():
    engine = make_engine("sqlite://")
    reset_db(engine)
    session = make_session_factory(engine)()
    seed(session)
    ctx = FillContext(session, FakeClock(SEED_ANCHOR), MockSMSProvider(), DownGloo(),
                      log_dir=Path("/tmp/sf-logs"))
    validation = planning_agent.build_draft(ctx, "2026-11")
    assert validation.violations == []  # deterministic draft still stands
    assert session.scalar(select(m.Approval).where(m.Approval.kind == "publish_schedule")) is not None


# --- reminders -------------------------------------------------------------------


def test_day_before_reminders_and_x_reply(seeded):
    ctx = seeded
    session, provider, clock = ctx.session, ctx.provider, ctx.clock

    # Move to Saturday Oct 3 — Sunday Oct 4 is tomorrow.
    clock.set_time(datetime(2026, 10, 3, 9, 0, tzinfo=DENVER))
    sent = jobs.run_daily_reminders(ctx)
    assert sent > 10
    assert jobs.run_daily_reminders(ctx) == 0  # idempotent

    # One reminded volunteer confirms with C; another cancels with X.
    reminded = session.scalars(
        select(m.Assignment).where(m.Assignment.reminded_at.isnot(None), m.Assignment.status == "approved")
    ).all()
    confirmer = reminded[0].volunteer
    handle_inbound(session, clock, provider, confirmer.phone, "C",
                   parser_returning(intent="confirm", confidence=0.99), ctx=ctx)
    assert reminded[0].status == "confirmed"

    canceller_assignment = next(a for a in reminded[1:] if a.volunteer.id != confirmer.id)
    canceller = canceller_assignment.volunteer
    handle_inbound(session, clock, provider, canceller.phone, "X",
                   parser_returning(intent="cancel", confidence=0.95), ctx=ctx)
    assert canceller_assignment.status == "cancelled"
    assert session.scalar(
        select(m.FillRequest).where(m.FillRequest.shift_id == canceller_assignment.shift_id)
    ) is not None  # fill process started


def test_saturday_coordinator_summary(seeded):
    ctx = seeded
    clock, provider = ctx.clock, ctx.provider
    coordinator = ctx.session.scalar(select(m.Volunteer).where(m.Volunteer.is_coordinator))

    clock.set_time(datetime(2026, 10, 3, 17, 0, tzinfo=DENVER))  # Saturday 5pm: too early
    assert jobs.run_coordinator_summary(ctx) is False
    clock.set_time(datetime(2026, 10, 3, 18, 5, tzinfo=DENVER))
    assert jobs.run_coordinator_summary(ctx) is True
    summary = [s for s in provider.sent_to(coordinator.phone) if s.body.startswith("Tomorrow:")]
    assert len(summary) == 1
    assert jobs.run_coordinator_summary(ctx) is False  # once per Saturday
