"""Background job definitions (PLAN.md sections 9.4, 11).

In real mode APScheduler calls tick() periodically; in demo mode the
fast-forward button calls run_time_based_jobs() after moving the fake clock.
Every job is idempotent (dedupe via reminded_at / message log / planning
state), so running them repeatedly is safe.
"""

from datetime import timedelta
from zoneinfo import ZoneInfo

from sqlalchemy import select

from app.agents import fill_agent
from app.config import get_settings
from app.core import templates
from app.db import models as m


def process_due_fill_requests(ctx: fill_agent.FillContext) -> list:
    """Advance every fill request whose tranche timer has expired."""
    return fill_agent.advance_due(ctx)


def run_daily_reminders(ctx: fill_agent.FillContext) -> int:
    """Day-before reminder for each approved/confirmed assignment, once."""
    session, now = ctx.session, ctx.clock.now()
    zone = ZoneInfo(get_settings().church_timezone)
    local_today = now.astimezone(zone).date()
    tomorrow = local_today + timedelta(days=1)

    sent = 0
    for assignment in session.scalars(
        select(m.Assignment).where(
            m.Assignment.status.in_(("approved", "confirmed")), m.Assignment.reminded_at.is_(None)
        )
    ):
        event = assignment.shift.event
        if event.starts_at.astimezone(zone).date() != tomorrow or event.status != "scheduled":
            continue
        local_start = event.starts_at.astimezone(zone)
        when_text = f"tomorrow at {local_start.strftime('%-I:%M%p').lower()}"
        outcome = ctx.gate.send(
            body=templates.reminder(
                assignment.volunteer.name, assignment.shift.role.name, when_text, event.title
            ),
            purpose="reminder", volunteer=assignment.volunteer,
        )
        # Blocked sends (opt-out, sensitive) still mark reminded: retrying
        # won't change the outcome and must never spam.
        assignment.reminded_at = now
        if outcome.sent:
            sent += 1
    session.flush()
    return sent


def run_coordinator_summary(ctx: fill_agent.FillContext) -> bool:
    """Saturday 6pm: 'Tomorrow: 14/14 filled...' text to the coordinator."""
    session, now = ctx.session, ctx.clock.now()
    zone = ZoneInfo(get_settings().church_timezone)
    local = now.astimezone(zone)
    if local.weekday() != 5 or local.hour < 18:  # Saturday evening only
        return False

    coordinator = session.scalar(select(m.Volunteer).where(m.Volunteer.is_coordinator))
    if coordinator is None:
        return False
    # Dedupe: one summary per Saturday.
    day_start = local.replace(hour=0, minute=0, second=0, microsecond=0)
    already = session.scalar(
        select(m.Message).where(
            m.Message.volunteer_id == coordinator.id,
            m.Message.direction == "out",
            m.Message.created_at >= day_start,
            m.Message.body.like("Tomorrow:%"),
        )
    )
    if already is not None:
        return False

    tomorrow = local.date() + timedelta(days=1)
    total = filled = 0
    for shift in session.scalars(select(m.Shift).join(m.Event, m.Shift.event_id == m.Event.id)):
        if shift.event.starts_at.astimezone(zone).date() != tomorrow or shift.event.status != "scheduled":
            continue
        total += 1
        if any(a.status in ("approved", "confirmed") for a in shift.assignments):
            filled += 1
    cancels_today = session.scalars(
        select(m.Assignment).where(
            m.Assignment.status == "cancelled", m.Assignment.updated_at >= day_start
        )
    ).all()
    covered = sum(
        1 for c in cancels_today
        if any(a.status in ("approved", "confirmed") and a.id != c.id
               for a in c.shift.assignments)
    )
    body = f"Tomorrow: {filled}/{total} filled."
    if cancels_today:
        body += f" {len(cancels_today)} cancellation(s) today, {covered} covered."
    ctx.gate.send(body=body, purpose="coordinator_notify", volunteer=coordinator)
    return True


def run_time_based_jobs(ctx: fill_agent.FillContext) -> dict:
    """Everything the clock drives; called on each real tick and demo advance."""
    from app.agents import planning_agent

    return {
        "fills_advanced": len(process_due_fill_requests(ctx)),
        "reminders_sent": run_daily_reminders(ctx),
        "summary_sent": run_coordinator_summary(ctx),
        "availability_nudges": planning_agent.nudge_nonresponders(ctx),
    }
