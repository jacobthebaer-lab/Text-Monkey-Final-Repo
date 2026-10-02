"""Durable delivery with dedupe, quiet-hour retry, and event-level summaries."""
from datetime import timedelta
from sqlalchemy import select, func
from app.db import models as m
from app.core.send_gate import SendStatus
from app.core.signup_responder import compose_signup_reply
from app.llm.gloo_client import GlooUnavailableError


def deliver(ctx, *, key, body, purpose, volunteer, event_id=None):
    if volunteer is None:
        return None
    row = ctx.session.get(m.Notification, key)
    if row is not None:
        if row.state == "pending" and ctx.reply_to_message_id:
            incoming = ctx.session.get(m.Message, ctx.reply_to_message_id)
            if incoming and incoming.direction == "in" and incoming.volunteer_id == volunteer.id:
                _dispatch(ctx, row)
        return row
    row = m.Notification(key=key, volunteer_id=volunteer.id, event_id=event_id,
                         purpose=purpose, body=body, state="pending",
                         created_at=ctx.clock.now(), due_at=ctx.clock.now(),
                         expires_at=ctx.clock.now() + timedelta(days=2), detail={})
    ctx.session.add(row)
    ctx.session.flush()
    _dispatch(ctx, row)
    return row


def staffing_snapshot(session, event):
    return staffing_snapshots(session, [event])[0]


def staffing_snapshots(session, events):
    """Fetch whole-event staffing in batches, including required missing slots."""
    events = list(events)
    if not events:
        return []
    shifts = session.scalars(select(m.Shift).where(m.Shift.event_id.in_([e.id for e in events]))).all()
    recipes = session.scalars(select(m.RoleRecipe).where(
        m.RoleRecipe.event_type_id.in_({e.event_type_id for e in events if e.event_type_id is not None}))).all()
    role_ids = {s.role_id for s in shifts} | {r.role_id for r in recipes}
    roles = {r.id: r for r in session.scalars(select(m.Role).where(m.Role.id.in_(role_ids)))}
    counts = dict(session.execute(select(m.Assignment.shift_id, func.count()).where(
        m.Assignment.shift_id.in_([s.id for s in shifts]),
        m.Assignment.status.in_(("approved", "confirmed"))).group_by(m.Assignment.shift_id)).all())
    snapshots = []
    for event in events:
        event_shifts = [s for s in shifts if s.event_id == event.id]
        minima = {r.role_id: r.count for r in recipes if r.event_type_id == event.event_type_id}
        gaps = []
        covered = needed = 0
        for role_id in sorted({s.role_id for s in event_shifts} | set(minima)):
            role = roles[role_id]
            role_shifts = [s for s in event_shifts if s.role_id == role_id]
            count = sum(counts.get(s.id, 0) for s in role_shifts)
            minimum = minima.get(role_id, len(role_shifts) if role.criticality != "optional" else 0)
            needed += minimum
            covered += min(count, minimum)
            if count < minimum:
                gaps.append({"role": role.name, "open": minimum-count})
        snapshots.append({"event_id": str(event.id), "title": event.title,
                          "starts_at": event.starts_at.isoformat(), "covered": covered,
                          "required": needed, "fully_staffed": not gaps, "gaps": gaps})
    return snapshots


def queue_staffing(ctx, event):
    """One pending event summary; each change restarts a five-minute debounce."""
    ctx.session.scalar(select(m.Event).where(m.Event.id == event.id).with_for_update())
    coordinator = ctx.session.scalar(select(m.Volunteer).where(m.Volunteer.is_coordinator))
    if coordinator is None:
        return
    key = f"staffing:{event.id}"
    row = ctx.session.scalar(select(m.Notification).where(m.Notification.key == key).with_for_update().execution_options(populate_existing=True))
    if row is None:
        row = m.Notification(key=key, event_id=event.id, volunteer_id=coordinator.id,
                             purpose="coordinator_notify", body="", detail={},
                             created_at=ctx.clock.now(), due_at=ctx.clock.now(), state="pending",
                             expires_at=event.starts_at)
        ctx.session.add(row)
    due = ctx.clock.now()+timedelta(minutes=5)
    last = (row.detail or {}).get("last_sent_at")
    if last:
        from datetime import datetime
        due = max(due, datetime.fromisoformat(last)+timedelta(minutes=15))
    row.state = "pending"
    row.due_at = min(due, event.starts_at)
    row.expires_at = event.starts_at+timedelta(minutes=5)
    ctx.session.flush()


def _dispatch(ctx, row):
    now = ctx.clock.now()
    if row.expires_at and now >= row.expires_at:
        row.state = "expired"
        return
    body = row.body
    urgent = False
    if row.key.startswith("staffing:"):
        recent = ctx.session.scalar(select(m.Message).where(
            m.Message.volunteer_id == row.volunteer_id, m.Message.direction == "out",
            m.Message.purpose == "coordinator_notify",
            m.Message.status.in_(("sent", "queued", "dispatching", "submitted", "uncertain")))
            .order_by(m.Message.created_at.desc()).limit(1))
        if recent and recent.created_at+timedelta(minutes=15) > now:
            row.due_at = recent.created_at+timedelta(minutes=15)
            return
        event = ctx.session.get(m.Event, row.event_id)
        if event is None or event.status in ("cancelled", "completed"):
            row.state = "expired"
            return
        snapshot = staffing_snapshot(ctx.session, event)
        urgent = not snapshot["fully_staffed"] and event.starts_at-now <= timedelta(hours=24)
        fills = ctx.session.scalars(select(m.FillRequest).join(m.Shift).where(m.Shift.event_id == event.id)).all()
        pending_ids = {f.id for f in fills if f.state == "waiting_approval"}
        approvals = [a for a in ctx.session.scalars(select(m.Approval).where(m.Approval.status == "pending"))
                     if a.payload.get("fill_request_id") in pending_ids]
        batches = {}
        for a in approvals:
            batches.setdefault(a.payload["fill_request_id"], a.id)
        attention = len([f for f in fills if f.state == "escalated"])
        signature = {k: snapshot[k] for k in ("covered", "required", "gaps")}
        signature.update(approval_batches=len(batches), attention=attention)
        if (row.detail or {}).get("last_snapshot") == signature:
            row.state = "unchanged"
            return
        when = event.starts_at.astimezone(ctx.gate.policies.church_tz()).strftime("%a %b %-d, %-I:%M%p")
        if snapshot["fully_staffed"]:
            body = f"Fully staffed: {event.title[:100]}, {when}. All {snapshot['required']} required spots are covered. The calendar is updated."
        else:
            gaps = ", ".join(f"{g['role']} ({g['open']})" for g in snapshot["gaps"][:3])
            if len(snapshot["gaps"]) > 3:
                gaps += f", and {len(snapshot["gaps"])-3} more roles"
            body = f"Still needs cover: {event.title[:100]}, {when}. {snapshot['covered']}/{snapshot['required']} required spots covered. Open: {gaps}. Check Texty for search status."
        from app.core.confirmations import enabled
        if enabled(ctx.session) and batches:
            body += f" {len(approvals)} exact invitations await review in Texty; sign in to review each recipient and text."
        elif len(batches) == 1:
            body += f" One restricted-role batch needs approval. Reply YES A{next(iter(batches.values()))} to send, or NO to decline."
        elif batches:
            body += f" {len(batches)} restricted-role batches await review in Texty."
        if attention:
            body += f" {attention} search(es) need your help; review Texty."
        row.detail = {**(row.detail or {}), "pending_snapshot": signature, "urgent": urgent}
    volunteer = ctx.session.get(m.Volunteer, row.volunteer_id)
    if volunteer is None:
        row.state = "blocked"
        return
    # Gloo writes within application facts; code alone decides staffing/assignment.
    try:
        # Preserve exact approved status/counts/codes; Gloo may adjust surrounding tone.
        required = (body,)
        rendered = compose_signup_reply(ctx.session, ctx.clock, ctx.gloo, body, required, volunteer=volunteer)
    except GlooUnavailableError:
        attempts = row.detail.get("gloo_attempts", 0)+1
        row.detail = {**row.detail, "gloo_attempts": attempts}
        row.due_at = now+timedelta(minutes=2)
        if attempts >= 3:
            row.state = "blocked"
            ctx.session.add(m.Escalation(category="system_error", severity="normal",
                summary="A saved notification needs review because Gloo could not compose it.",
                related_ids={"notification_key": row.key}, status="open", created_at=now))
        return
    result = ctx.gate.send(body=rendered, purpose=row.purpose, volunteer=volunteer, kind="ai", urgent=urgent)
    row.body = body
    if result.status == SendStatus.HELD_QUIET_HOURS:
        row.due_at = result.retry_at
        row.state = "pending"
    elif result.sent:
        row.message_id = result.message_id
        row.state = "sent"
        if row.key.startswith("staffing:"):
            row.detail = {"last_sent_at": now.isoformat(),
                          "last_snapshot": row.detail["pending_snapshot"], "urgent": urgent}
    else:
        row.state = "blocked"
        row.detail = {**row.detail, "reason": result.reason}


def flush_due(ctx):
    rows = ctx.session.scalars(select(m.Notification).where(
        m.Notification.state == "pending", m.Notification.due_at <= ctx.clock.now()
    ).order_by(m.Notification.due_at).with_for_update(skip_locked=True)).all()
    for row in rows:
        _dispatch(ctx, row)
    return len(rows)
