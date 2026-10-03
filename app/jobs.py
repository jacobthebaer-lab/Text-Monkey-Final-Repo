"""Background job definitions.

Phase 4: the fill-request tick. APScheduler wiring (a BackgroundScheduler
calling these every minute on the real clock) lands with the web app in
Phase 5; tests and demo fast-forward call them directly with the fake clock.
"""

from app.agents import fill_agent


def process_due_fill_requests(ctx: fill_agent.FillContext) -> list:
    """Advance every fill request whose tranche timer has expired."""
    from datetime import datetime
    from sqlalchemy import select
    from app.db import models as m
    from app.core.offer_windows import begin_decision
    begin_decision(ctx.session)
    for approval in ctx.session.scalars(select(m.Approval).where(m.Approval.status == "approved").with_for_update(skip_locked=True)):
        retry = approval.payload.get("retry_at")
        if not retry or datetime.fromisoformat(retry) > ctx.clock.now():
            continue
        fill = ctx.session.get(m.FillRequest, approval.payload.get("fill_request_id"))
        if not fill or fill.state != "waiting_approval":
            approval.status = "expired"
            continue
        shift = ctx.session.get(m.Shift, fill.shift_id)
        if shift.event.starts_at <= ctx.clock.now():
            approval.status = "expired"
            fill.state, fill.next_action_at = "in_progress", ctx.clock.now()
            continue
        result = ctx.gate.send_approved(approval)
        payload = {k: v for k, v in approval.payload.items() if k != "retry_at"}
        if result.retry_at:
            payload["retry_at"] = result.retry_at.isoformat()
        elif result.sent:
            outreach = ctx.session.get(m.Outreach, payload.get("outreach_id"))
            if outreach:
                outreach.message_id = result.message_id
            fill_agent.on_outreach_approved(ctx, fill.id)
        else:
            fill.state, fill.next_action_at = "in_progress", ctx.clock.now()
        approval.payload = payload
    outcomes = fill_agent.advance_due(ctx)
    from app.core.notifications import flush_due
    flush_due(ctx)
    return outcomes
