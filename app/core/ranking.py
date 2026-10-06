"""Deterministic candidate ranking for fill requests (PLAN.md section 9.3).

Hard filters first (eligibility, opt-in, monthly max), then a transparent
score. The breakdown is kept per candidate so the dashboard and session log
can show why someone was ranked where they were. The monthly planner still
uses this score. Fill outreach reuses its hard filters, then Clyde's algorithm
owns recipient ranking and selection.
"""

from dataclasses import dataclass
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.core import eligibility
from app.core.recurring_availability import global_frequency_limit
from app.core.policies import PolicyStore
from app.core.send_gate import has_open_sensitive_escalation, UNSENT_STATUSES
from app.db import models as m

SERVED_STATUSES = ("approved", "confirmed", "completed")
ASKED_RECENTLY_DAYS = 14
LOW_LOAD_DAYS = 30


@dataclass
class Candidate:
    volunteer: m.Volunteer
    score: float
    breakdown: dict


def service_tag(event: m.Event, tz: ZoneInfo) -> str | None:
    """Match an event to a preferred_services tag like sun_9 / sun_11."""
    local = event.starts_at.astimezone(tz)
    if local.weekday() == 6:
        return f"sun_{local.hour}"
    return None


def _month_bounds(when: datetime, tz: ZoneInfo) -> tuple[datetime, datetime]:
    local = when.astimezone(tz)
    start = local.replace(day=1, hour=0, minute=0, second=0, microsecond=0)
    end = (start + timedelta(days=32)).replace(day=1)
    return start, end


def monthly_assignment_count(session: Session, volunteer_id: int, event: m.Event, tz: ZoneInfo) -> int:
    start, end = _month_bounds(event.starts_at, tz)
    return session.scalar(
        select(func.count())
        .select_from(m.Assignment)
        .join(m.Shift, m.Assignment.shift_id == m.Shift.id)
        .join(m.Event, m.Shift.event_id == m.Event.id)
        .where(
            m.Assignment.volunteer_id == volunteer_id,
            m.Assignment.status.in_(SERVED_STATUSES),
            m.Event.starts_at >= start,
            m.Event.starts_at < end,
        )
    )


def rank_candidates(
    session: Session,
    shift: m.Shift,
    now: datetime,
    exclude_ids: tuple[int, ...] = (),
    tz: str = "America/Denver",
) -> list[Candidate]:
    zone = ZoneInfo(tz)
    event = shift.event
    role = shift.role
    tag = service_tag(event, zone)
    candidates: list[Candidate] = []
    policies = PolicyStore(session)

    volunteers = session.scalars(select(m.Volunteer).order_by(m.Volunteer.id)).all()
    by_id = {v.id: v for v in volunteers}

    for vol in volunteers:
        # Hard filters — never scored around.
        if vol.id in exclude_ids or vol.is_coordinator or vol.is_pastor:
            continue
        if vol.preferences.get("onboarding_stage") in {"interests", "availability"}:
            continue
        if has_open_sensitive_escalation(session, vol.id):
            continue
        recent_ask = session.scalar(select(m.Message.id).where(m.Message.volunteer_id == vol.id,
            m.Message.direction == "out", m.Message.purpose.in_(("outreach", "availability_ask")),
            m.Message.status.not_in(UNSENT_STATUSES), m.Message.created_at <= now,
            m.Message.created_at > now-timedelta(hours=int(policies.get("outreach_cooldown_hours")))))
        if recent_ask:
            continue
        local = now.astimezone(zone)
        month_start = local.replace(day=1, hour=0, minute=0, second=0, microsecond=0)
        asks = session.scalar(select(func.count()).select_from(m.Message).where(m.Message.volunteer_id == vol.id,
            m.Message.direction == "out", m.Message.purpose.in_(("outreach", "availability_ask")),
            m.Message.status.not_in(UNSENT_STATUSES), m.Message.created_at <= now, m.Message.created_at >= month_start))
        if asks >= policies.ask_budget():
            continue
        if not vol.sms_opt_in:
            continue  # we cannot ask someone we may not text
        if not eligibility.check(session, vol, shift, tz=tz):
            continue
        try:
            max_per_month = global_frequency_limit(vol.preferences)
        except ValueError:
            continue
        if max_per_month is not None and monthly_assignment_count(session, vol.id, event, zone) >= max_per_month:
            continue

        breakdown: dict = {}
        prefs = vol.preferences

        if role.name in prefs.get("interested_roles", []):
            breakdown["preferred_role"] = 2.0
        if tag and tag in prefs.get("preferred_services", []):
            breakdown["attends_service"] = 1.0

        recent_load = session.scalar(
            select(func.count())
            .select_from(m.Assignment)
            .join(m.Shift, m.Assignment.shift_id == m.Shift.id)
            .join(m.Event, m.Shift.event_id == m.Event.id)
            .where(
                m.Assignment.volunteer_id == vol.id,
                m.Assignment.status.in_(SERVED_STATUSES),
                m.Event.starts_at >= now - timedelta(days=LOW_LOAD_DAYS),
                m.Event.starts_at < now,
            )
        )
        breakdown["low_recent_load"] = float(max(0, 2 - recent_load))

        responses = session.execute(
            select(m.Outreach.response).where(
                m.Outreach.volunteer_id == vol.id, m.Outreach.response.in_(("yes", "no"))
            )
        ).all()
        yeses = sum(1 for (r,) in responses if r == "yes")
        breakdown["yes_rate"] = round(yeses / len(responses), 2) if responses else 0.5

        asked_recently = session.scalar(
            select(func.count())
            .select_from(m.Message)
            .where(
                m.Message.volunteer_id == vol.id,
                m.Message.direction == "out",
                m.Message.purpose == "outreach",
                m.Message.created_at >= now - timedelta(days=ASKED_RECENTLY_DAYS),
            )
        )
        if asked_recently:
            breakdown["asked_recently"] = -min(1.5, 0.5 * asked_recently)

        partner_id = prefs.get("serves_with_volunteer_id")
        if partner_id and partner_id in by_id:
            partner = by_id[partner_id]
            if partner.sms_opt_in and eligibility.check(session, partner, shift, tz=tz):
                breakdown["partner_available"] = 1.0

        candidates.append(Candidate(vol, round(sum(breakdown.values()), 2), breakdown))

    candidates.sort(key=lambda c: (-c.score, c.volunteer.id))
    return candidates
