"""Offer-only deadlines. Durable metadata uses Notification, without a migration.

Provider dispatch is the send gate for synchronous transports and native
preflight for the durable Mac queue. Submission remains a separate ack.
"""
from datetime import datetime, timedelta, timezone
import math

from sqlalchemy import func, select

from app.clock import RealClock
from app.core.policies import PolicyStore
from app.db import models as m

DEFAULT_POLICY = {"max_minutes": 120, "min_minutes": 2,
                  "lead_time_divisor": 6, "cutoff_minutes": 10}
OPEN_RESPONSES = ("none", "partial", "unclear")
OPEN_FILLS = ("open", "in_progress", "waiting_approval", "waiting_quiet")


def begin_decision(session):
    # SQLite has no row locks. Acquire its writer lock before taking a snapshot
    # when a fresh timer/dispatch transaction starts. HTTP ingress already
    # writes a unique receipt/inbound row before making scheduling decisions.
    if session.get_bind().dialect.name == "sqlite":
        connection = session.connection()
        # SQLAlchemy may have autobegun after a read without SQLite having
        # begun a physical transaction. Fence that case before fresh reads too.
        if not connection.connection.driver_connection.in_transaction:
            connection.exec_driver_sql("BEGIN IMMEDIATE")


def decision_time(session, clock):
    """Read fresh DB time after locks; synthetic/injected clocks stay deterministic."""
    if not isinstance(clock, RealClock):
        return clock.now().astimezone(timezone.utc)
    if session.get_bind().dialect.name == "postgresql":
        value = session.scalar(select(func.clock_timestamp()))
    else:
        value = datetime.fromisoformat(session.scalar(select(func.strftime("%Y-%m-%d %H:%M:%f", "now"))))
    return value.replace(tzinfo=timezone.utc) if value.tzinfo is None else value.astimezone(timezone.utc)


def policy(session):
    raw = PolicyStore(session).get("offer_response_window")
    if not isinstance(raw, dict) or set(raw) - set(DEFAULT_POLICY):
        raise ValueError("Offer response policy must contain only supported settings")
    values = {**DEFAULT_POLICY, **raw}
    if any(type(v) not in (int, float) or not math.isfinite(v) or v <= 0 for v in values.values()):
        raise ValueError("Offer response policy requires finite positive numbers")
    if values["min_minutes"] > values["max_minutes"]:
        raise ValueError("Offer minimum exceeds maximum")
    return values


def deadline_for(session, starts_at, dispatched_at):
    p = policy(session)
    start, now = starts_at.astimezone(timezone.utc), dispatched_at.astimezone(timezone.utc)
    minimum = timedelta(minutes=p["min_minutes"])
    window = min(timedelta(minutes=p["max_minutes"]),
                 max(minimum, (start - now) / p["lead_time_divisor"]))
    # Persist the same second shown in the text, conservatively rounding down.
    deadline = min(now + window, start - timedelta(minutes=p["cutoff_minutes"])).replace(microsecond=0)
    return deadline if deadline - now >= minimum else None


def cutoff(session, starts_at):
    return starts_at.astimezone(timezone.utc) - timedelta(minutes=policy(session)["cutoff_minutes"])


def interval_label(session, shift):
    """Both dated boundaries and UTC offsets are explicit, including DST folds."""
    tz = PolicyStore(session).church_tz()
    render = lambda value: value.astimezone(tz).strftime('%a %b %-d, %-I:%M%p %Z (UTC%z)').replace('AM','am').replace('PM','pm')
    return render(shift.starts_at) + ' to ' + render(shift.ends_at)


def interval_copy_problem(session, shift, body):
    if shift.parent_shift_id is not None and interval_label(session, shift) not in body:
        return 'The child invitation must include both exact dated interval boundaries'
    return None


def snapshot(shift):
    result = {"start": shift.starts_at.astimezone(timezone.utc).isoformat(),
            "end": shift.ends_at.astimezone(timezone.utc).isoformat(),
            "event_title": shift.event.title, "role_id": shift.role_id,
            "role_name": shift.role.name, "qualifications": list(shift.role.required_qualifications),
            "fill_policy": shift.role.fill_policy}
    if shift.parent_shift_id is not None:
        result.update(parent_shift_id=shift.parent_shift_id, coverage_review_id=shift.coverage_review_id)
    return result


def metadata(session, outreach):
    return session.get(m.Notification, f"offer:{outreach.id}")


def lock(session, outreach):
    """Same event/slot/person/request/offer order as acceptance and timers."""
    fill = session.get(m.FillRequest, outreach.fill_request_id)
    shift = session.get(m.Shift, fill.shift_id)
    session.scalar(select(m.Event).where(m.Event.id == shift.event_id).with_for_update()
                   .execution_options(populate_existing=True))
    session.scalar(select(m.Shift).where(m.Shift.id == shift.id).with_for_update()
                   .execution_options(populate_existing=True))
    session.scalar(select(m.Role).where(m.Role.id == shift.role_id).with_for_update(read=True)
                   .execution_options(populate_existing=True))
    session.scalar(select(m.Volunteer).where(m.Volunteer.id == outreach.volunteer_id).with_for_update(key_share=True)
                   .execution_options(populate_existing=True))
    session.scalar(select(m.FillRequest).where(m.FillRequest.id == fill.id).with_for_update()
                   .execution_options(populate_existing=True))
    return session.scalar(select(m.Outreach).where(m.Outreach.id == outreach.id).with_for_update()
                          .execution_options(populate_existing=True))


def copy_with_deadline(session, body, deadline):
    local = deadline.astimezone(PolicyStore(session).church_tz())
    # Include seconds and UTC offset to make the exact boundary/DST fold explicit.
    when = local.strftime("%a %b %-d, %-I:%M:%S%p %Z (UTC%z)")
    return body + f" Reply yes or no by {when}. If I don't hear back, I'll ask someone else."


def prepare(session, outreach, body, now):
    shift = session.get(m.Shift, session.get(m.FillRequest, outreach.fill_request_id).shift_id)
    deadline = deadline_for(session, shift.starts_at, now)
    if deadline is None:
        return None
    row = metadata(session, outreach)
    if row is None:
        row = m.Notification(key=f"offer:{outreach.id}", volunteer_id=outreach.volunteer_id,
                             event_id=shift.event_id, purpose="offer_window", body="",
                             state="offer_review", created_at=now, due_at=deadline,
                             detail={})
        session.add(row)
    row.detail = {"draft_body": body, "snapshot": snapshot(shift),
                  "fill_request_id": outreach.fill_request_id, "shift_id": shift.id}
    row.body = copy_with_deadline(session, body, deadline)
    row.expires_at, row.due_at = deadline, deadline
    session.flush()
    return row


def task_once(session, fill, now, reason):
    """Existing dashboard coordinator task only; no outbound escalation text."""
    for row in session.scalars(select(m.Escalation).where(m.Escalation.status == "open")):
        if row.related_ids.get("fill_request_id") == fill.id:
            return row
    coordinator = session.scalar(select(m.Volunteer).where(m.Volunteer.is_coordinator))
    row = m.Escalation(category="unfillable", severity="normal", summary=reason,
                       related_ids={"fill_request_id": fill.id}, status="open", created_at=now,
                       assigned_to=coordinator.id if coordinator else None)
    session.add(row)
    session.flush()
    return row


def close(session, outreach, response, now):
    """Never touch assignments, consent, preferences, or response-rate history."""
    if outreach.response not in OPEN_RESPONSES:
        return
    outreach.response = response
    row = metadata(session, outreach)
    if row:
        row.state = "offer_" + response
        row.detail = {**row.detail, "closed_at": now.isoformat()}
    message = session.get(m.Message, outreach.message_id) if outreach.message_id else None
    if message and message.status == "queued":
        message.status = "superseded"
    for approval in session.scalars(select(m.Approval).where(m.Approval.status.in_(("pending", "approved")))):
        if approval.payload.get("outreach_id") == outreach.id:
            approval.status = "expired"


def problem(session, outreach, now):
    fill = session.get(m.FillRequest, outreach.fill_request_id)
    shift = session.get(m.Shift, fill.shift_id)
    from app.core.split_coverage import children, child_problem
    if children(session, shift.id) or child_problem(session, shift):
        return "shift changed or closed"
    row = metadata(session, outreach)
    if row and interval_copy_problem(session, shift, row.body):
        return "shift changed or closed"
    if row and row.detail.get("invitation_time_contract") == 1:
        from app.core.invitation_facts import copy_problem
        if error := copy_problem(session, shift, row.detail.get("draft_body", "")):
            return error
    if not row:
        return "offer has no dispatch deadline"
    if row.detail.get("snapshot") != snapshot(shift) or shift.event.status in ("cancelled", "completed"):
        return "shift changed or closed"
    if outreach.response not in OPEN_RESPONSES or fill.state not in OPEN_FILLS:
        return "offer is closed"
    if row.state == "offer_uncertain":
        return "delivery needs reconciliation"
    if row.state != "offer_active":
        return "offer was not dispatched"
    if now >= row.expires_at:
        return "offer deadline reached"
    return None


def dispatch(session, outreach, message, now, *, exact=False, claim=False):
    """Persist immutable dispatch deadline before handing a body to a transport.

    Exact-reviewed content cannot be silently rewritten. A changed deadline
    creates fresh review, and the old message is never delivered.
    """
    fill = session.get(m.FillRequest, outreach.fill_request_id)
    shift = session.get(m.Shift, fill.shift_id)
    from app.core.split_coverage import children, child_problem
    if children(session, shift.id) or child_problem(session, shift):
        close(session, outreach, "revoked", now)
        return "reviewed slot interval changed or parent was partitioned"
    row = metadata(session, outreach)
    if (not row or outreach.response not in OPEN_RESPONSES or fill.state not in OPEN_FILLS or
            row.detail.get("snapshot") != snapshot(shift) or shift.event.status in ("cancelled", "completed")):
        close(session, outreach, "revoked", now)
        return "offer changed or closed"
    if error := interval_copy_problem(session, shift, row.detail.get('draft_body', '')):
        close(session, outreach, 'revoked', now)
        return error
    if row.detail.get("invitation_time_contract") == 1:
        from app.core.invitation_facts import copy_problem
        if error := copy_problem(session, shift, row.detail.get("draft_body", "")):
            close(session, outreach, "revoked", now)
            return error
    if row.state in ("offer_active", "offer_uncertain"):
        return "offer already dispatched"
    occupied = session.scalar(select(m.Assignment.id).where(m.Assignment.shift_id == shift.id,
        m.Assignment.status.in_(("proposed", "approved", "confirmed"))))
    from app.core.algorithm_outreach import conflicting_offer
    other = conflicting_offer(session, outreach)
    if occupied or other or delivery_hold(session, volunteer_id=outreach.volunteer_id, shift_id=shift.id,
                                          exclude_outreach_id=outreach.id):
        close(session, outreach, "blocked", now)
        fill.next_action_at = now
        return "slot occupied or another invitation is active"
    deadline = deadline_for(session, shift.starts_at, now)
    if deadline is None:
        close(session, outreach, "revoked", now)
        fill.state, fill.next_action_at = "escalated", None
        task_once(session, fill, now, "Too little time remains for an automatic offer; coordinator review required.")
        return "too little time for an offer"
    body = copy_with_deadline(session, row.detail["draft_body"], deadline)
    if exact and body != row.body:
        row.body, row.expires_at, row.due_at = body, deadline, deadline
        row.state = "offer_review"
        if message:
            message.status = "blocked_confirmation"
        task_once(session, fill, now, "Dispatch changed the reply deadline; review the refreshed exact invitation.")
        return "reply deadline changed; fresh exact review required"
    row.body, row.expires_at, row.due_at = body, deadline, deadline
    row.state = "offer_claimed" if claim else "offer_active"
    row.detail = {**row.detail, "claimed_at" if claim else "dispatched_at": now.isoformat()}
    if message:
        message.body = body
        row.message_id = message.id
    fill.next_action_at = cutoff(session, shift.starts_at) if claim else deadline
    session.flush()
    return None


def delivery_hold(session, *, volunteer_id=None, shift_id=None, exclude_outreach_id=None):
    from sqlalchemy import or_
    scopes = []
    if volunteer_id is not None:
        scopes.append(m.Notification.volunteer_id == volunteer_id)
    if shift_id is not None:
        scopes.append(m.Notification.detail["shift_id"].as_integer() == shift_id)
    return session.scalar(select(m.Notification.key).where(
        m.Notification.purpose == "offer_window", m.Notification.state.in_(("offer_uncertain", "offer_claimed")),
        m.Notification.key != f"offer:{exclude_outreach_id}", or_(*scopes)).limit(1)) is not None


def sender_busy(session, volunteer_id):
    return delivery_hold(session, volunteer_id=volunteer_id) or session.scalar(select(m.Outreach.id).join(m.FillRequest).where(
        m.Outreach.volunteer_id == volunteer_id, m.Outreach.response.in_(OPEN_RESPONSES),
        m.FillRequest.state.in_(OPEN_FILLS)).limit(1)) is not None
