"""Recheck empty replacement pools without retrying delivery or human holds."""
from datetime import timedelta

from sqlalchemy import select

from app.core import offer_windows as offers
from app.db import models as m


def watch(session, fill, now, rejected):
    shift = session.get(m.Shift, fill.shift_id)
    key = f"replacement-pool:{fill.id}"
    row = session.get(m.Notification, key)
    if row is None:
        row = m.Notification(key=key, event_id=shift.event_id, purpose="replacement_pool",
                             body="", created_at=now, due_at=now, detail={})
        session.add(row)
    row.state = "waiting_candidates"
    row.due_at = now + timedelta(minutes=1)
    row.expires_at = offers.cutoff(session, shift.starts_at)
    row.detail = {"fill_request_id": fill.id, "snapshot": offers.snapshot(shift),
                  "excluded": [{"volunteer_id": ident, "reasons": reasons}
                               for ident, reasons in sorted(rejected.items())]}
    session.flush()
    return row


def advance_due(ctx):
    """Pool polling never defines batch size, recipient order or reply timing."""
    from app.agents import fill_agent
    from app.llm.tools import replacement_pool
    from app.core.policies import PolicyStore

    session = ctx.session
    offers.begin_decision(session)
    now = offers.decision_time(session, ctx.clock)
    outcomes = []
    for row in session.scalars(select(m.Notification).where(
            m.Notification.purpose == "replacement_pool",
            m.Notification.state == "waiting_candidates", m.Notification.due_at <= now)).all():
        fill = session.get(m.FillRequest, row.detail["fill_request_id"])
        shift = session.get(m.Shift, fill.shift_id) if fill else None
        if shift:
            session.scalar(select(m.Event).where(m.Event.id == shift.event_id).with_for_update()
                           .execution_options(populate_existing=True))
            session.scalar(select(m.Shift).where(m.Shift.id == shift.id).with_for_update()
                           .execution_options(populate_existing=True))
            session.scalar(select(m.Role).where(m.Role.id == shift.role_id).with_for_update(read=True)
                           .execution_options(populate_existing=True))
            session.scalar(select(m.FillRequest).where(m.FillRequest.id == fill.id).with_for_update()
                           .execution_options(populate_existing=True))
        row = session.scalar(select(m.Notification).where(m.Notification.key == row.key).with_for_update()
                             .execution_options(populate_existing=True))
        if row.state != "waiting_candidates":
            continue
        now = offers.decision_time(session, ctx.clock)
        if (not fill or fill.state != "escalated" or fill.closed_at or
                shift.event.status != "scheduled" or offers.snapshot(shift) != row.detail["snapshot"] or
                now >= row.expires_at or offers.delivery_hold(session, shift_id=shift.id)):
            row.state = "closed"
            continue
        tasks = session.scalars(select(m.Escalation).where(m.Escalation.status == "open")).all()
        related = [t for t in tasks if t.related_ids.get("fill_request_id") == fill.id]
        if not any(t.category == "unfillable" for t in related) or any(t.category != "unfillable" for t in related):
            row.state = "closed"
            continue
        occupied = session.scalar(select(m.Assignment.id).where(m.Assignment.shift_id == shift.id,
            m.Assignment.status.in_(fill_agent.eligibility.ACTIVE_ASSIGNMENT_STATUSES)))
        rejected = {}
        candidates = replacement_pool(session, fill, now, str(PolicyStore(session).church_tz()), rejected=rejected)
        if not candidates and not occupied:
            row.detail = {**row.detail, "excluded": [{"volunteer_id": ident, "reasons": reasons}
                          for ident, reasons in sorted(rejected.items())]}
            for task in related:
                task.related_ids = {**task.related_ids, "excluded": row.detail["excluded"]}
            row.due_at = min(now + timedelta(minutes=1), row.expires_at)
            continue
        row.state = "resumed"
        fill.state, fill.next_action_at = "in_progress", now
        # The existing locked advance performs all fresh checks and delegates
        # selection and batching to Clyde, composition to Gloo, delivery to SendGate.
        outcomes.append(fill_agent._advance(ctx, fill))
    return outcomes
