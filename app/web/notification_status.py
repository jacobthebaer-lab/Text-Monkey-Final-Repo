"""Read-only projection of existing assignment notices, never a scheduler."""
from datetime import datetime, timedelta
from fastapi import APIRouter, Depends, Query, Request
from sqlalchemy import select
from sqlalchemy.orm import selectinload
from app.core import confirmations, eligibility, outbound_conversation, reminders
from app.core.policies import PolicyStore, in_quiet_hours, next_send_time
from app.core.send_gate import has_open_sensitive_escalation
from app.db import models as m
from app.sms.mock_provider import MockSMSProvider
from app.sms.mac_provider import MacMessagesProvider
from app.web.texty import admin

router = APIRouter()
# Only known code-owned reasons can replace the generic label, never arbitrary
# saved notes, phone numbers or conversation content.
_POLICY_REASONS = {
    "Schedule notification requires a recorded assignment",
    "Schedule notification purpose and source do not match",
    "Schedule notification assignment does not belong to this recipient",
    "Schedule assignment or recipient is missing",
    "Schedule assignment is no longer the recorded placement",
    "Day-before reminder is not due",
    "Schedule recipient is no longer eligible or consenting",
    "This signup question or assignment notification was already requested",
    "Routine volunteer acknowledgments, progress and offer prompts are suppressed",
}


def read_session(request: Request):
    # Deliberately avoid the shared write/commit dependency and mutation helpers.
    with request.app.state.session_factory(autoflush=False) as session:
        session.info["record_authorized"] = False
        yield session


def _session_status(provider, phone, now):
    if not hasattr(provider, "allows"):
        return "not_required"
    if not provider.allows(phone):
        return "not_selected"
    selected = getattr(provider, "test_sessions", {}).get(phone)
    if selected is None:
        return "missing"
    if now < selected.starts_at:
        return "not_started"
    return "active" if selected.active(now) else "expired"


def _bound_message(session, row, notice, value, approval, reservations):
    candidates = [value.get("message_id"), approval.payload.get("message_id") if approval else None]
    candidates.extend(n.message_id for n in reservations if n.detail.get("assignment_id") == row.id
                      and n.detail.get("notice") == notice)
    for ident in candidates:
        message = session.get(m.Message, ident) if type(ident) is int else None
        if (message and message.direction == "out" and message.volunteer_id == row.volunteer_id
                and message.phone == row.volunteer.phone
                and message.purpose == ("reminder" if notice == "day_before" else "confirmation")):
            return message
    return None


def _result(item, state, reason, next_step):
    return {**item, "state":state, "reason":reason, "next_step":next_step}


def _automatic_problem(session, state, row, message, now):
    """Use delivery's existing guards without retaining recipient context in a read."""
    selected = getattr(state.provider, "test_sessions", {}).get(row.volunteer.phone)
    missing = object()
    previous = session.info.pop("mac_test_session", missing)
    try:
        if not reminders.automatic_scope(session, state.settings, row.volunteer.phone, selected, now):
            return "Automatic reminder recipient authorization changed"
        session.info["mac_test_session"] = selected
        return outbound_conversation.queued_problem(session, message, now) if message else None
    finally:
        if previous is missing:
            session.info.pop("mac_test_session", None)
        else:
            session.info["mac_test_session"] = previous


def _item(session, row, notice, state, policies, now, jobs, reservations):
    tz = policies.church_tz()
    purpose = "reminder" if notice == "day_before" else "confirmation"
    key = f"job:{'reminder' if notice == 'day_before' else 'assignment'}:{row.id}"
    value = jobs[key].value if key in jobs else {}
    approval = session.get(m.Approval, value.get("approval_id")) if type(value.get("approval_id")) is int else None
    if approval and (approval.kind != "confirm_text" or approval.payload.get("workflow_job_key") != key):
        approval = None
    message = _bound_message(session, row, notice, value, approval, reservations)
    if message and approval is None:
        proof = confirmations.proof_for(session, message)
        if proof and proof.payload.get("conversation", {}).get("assignment_id") == row.id:
            approval = proof
    local_start = row.shift.starts_at.astimezone(tz)
    due = (local_start - timedelta(days=1)).replace(hour=0, minute=0, second=0, microsecond=0) if notice == "day_before" else row.created_at
    basis = "local_day_before_window" if notice == "day_before" else "assignment_created"
    for receipt in reservations:
        if receipt.detail.get("assignment_id") == row.id and receipt.detail.get("notice") == notice:
            due, basis = receipt.due_at, "recorded_notification"
            break
    if value.get("due_at"):
        try:
            saved = datetime.fromisoformat(value["due_at"])
            if saved.tzinfo is not None: due, basis = saved, "recorded_job"
        except (ValueError, TypeError):
            pass
    selected = _session_status(state.provider, row.volunteer.phone, now)
    mock = isinstance(state.provider, MockSMSProvider) or bool(message and (message.provider_sid or "").startswith("MOCK"))
    item = {"id":f"assignment:{row.id}:{notice}", "assignment_id":row.id,
        "event_id":row.shift.event_id, "volunteer_id":row.volunteer_id,
        "recipient_name":row.volunteer.name, "role":row.shift.role.name,
        "event_title":row.shift.event.title, "starts_at":local_start.isoformat(),
        "notice":notice, "due_at":due.astimezone(tz).isoformat(), "due_basis":basis,
        "approval_id":approval.id if approval else None, "message_id":message.id if message else None,
        "provider_message_status":message.status if message else None,
        "delivery_evidence":"mock_only" if mock else "not_recorded", "recipient_session":selected}
    if message and message.status in {"sent", "submitted", "delivered"}:
        return _result(item, "held", "Mock result; no device delivery evidence." if mock else "Submission recorded; device delivery is awaiting verification.",
                       "Use a real device receipt to verify delivery; do not resend automatically.")
    if (message and message.status == "uncertain") or value.get("state") == "uncertain":
        return _result(item, "held", "Delivery is uncertain.", "Reconcile the existing native attempt before any retry.")
    if value.get("state") == "blocked_policy" or (approval and approval.status == "rejected") or (message and message.status in {"blocked_policy", "superseded"}):
        reason = value.get("policy_reason")
        return _result(item, "suppressed", reason if isinstance(reason, str) and reason in _POLICY_REASONS else "The existing workflow was suppressed or rejected.",
                       "Review the existing record internally; no automatic retry.")
    if row.status not in {"approved", "confirmed"} or row.shift.event.status != "scheduled":
        return _result(item, "suppressed", "The assignment or event is no longer scheduled.", "No notification is due for this placement.")
    if row.shift.starts_at <= now:
        return _result(item, "suppressed", "The event has started; this notice window has ended.", "Review any historical delivery internally.")
    if notice == "scheduled" and row.source != "planner" and not value and not approval and not message:
        return _result(item, "not-required", "This assignment source has no automatic initial-notice job.",
                       "The saved assignment can still receive its day-before reminder.")
    day_before = local_start.date() - timedelta(days=1)
    if notice == "scheduled" and now.astimezone(tz).date() == day_before:
        return _result(item, "suppressed", "The day-before reminder supersedes the initial scheduled notice.", "Use the existing day-before reminder status.")
    if notice == "day_before" and now.astimezone(tz).date() > day_before:
        return _result(item, "suppressed", "The local day-before reminder window has ended.", "Do not send an outdated tomorrow reminder.")
    optout = session.get(m.Policy, "sms_opt_out:" + row.volunteer.phone)
    if not row.volunteer.sms_opt_in or (optout and optout.value.get("value")):
        return _result(item, "held", "Recipient consent is unavailable.", "Resolve consent internally before preparing a notice.")
    phone_hold = any(ids.get("phone") == row.volunteer.phone for ids in session.scalars(
        select(m.Escalation.related_ids).where(m.Escalation.category == "sensitive",
                                             m.Escalation.status.in_(("open", "acknowledged")))))
    if has_open_sensitive_escalation(session, row.volunteer_id) or phone_hold:
        return _result(item, "held", "A personal-care hold requires human follow-up.", "Review the internal escalation.")
    if not eligibility.check(session, row.volunteer, row.shift, str(tz), _exclude_assignment_id=row.id):
        return _result(item, "held", "The assignment recipient is no longer eligible.", "Review availability and role requirements internally.")
    if value.get("source") and value["source"] != reminders.assignment_source(row, purpose):
        return _result(item, "held", "Saved notice details differ from the current assignment.", "Request a fresh source-bound review.")
    if message and message.status.startswith("blocked_"):
        return _result(item, "held", "Transport blocked this queued notice.", "Review consent, session, source and exact-approval guards internally.")
    if selected not in {"active", "not_required"}:
        return _result(item, "held", "The selected recipient session is " + selected.replace("_", " ") + ".",
                       "Have the runtime owner verify the intended recipient and session.")
    if approval:
        if approval.payload.get("phone") != row.volunteer.phone or approval.payload.get("volunteer_id") != row.volunteer_id:
            return _result(item, "held", "The recipient differs from the exact review.", "Request a fresh recipient-bound review.")
        if hasattr(state.provider, "allows") and getattr(state.provider, "test_sessions", {})[row.volunteer.phone].id != approval.payload.get("session_id"):
            return _result(item, "held", "The selected recipient session differs from the exact review.", "Request a fresh session-bound review.")
        if not confirmations.valid(approval, now):
            return _result(item, "held", "Exact review changed or expired.", "Request a fresh exact review.")
        if problem := reminders.delivery_problem(session, approval, now):
            return _result(item, "held", "Exact review no longer matches its workflow source.", "Request a fresh source-bound review.")
    if message and message.status in {"queued", "dispatching"}:
        automatic = (notice == "day_before" and isinstance(state.provider, MacMessagesProvider)
                     and value.get("automatic_reminder") is True and approval is None)
        if automatic:
            problem = _automatic_problem(session, state, row, message, now)
        elif approval is None or approval.status != "approved":
            return _result(item, "held", "Queued notice has no current approved exact review.", "Review the existing queue and approval internally.")
        else:
            problem = confirmations.delivery_problem(session, state.provider, approval, now, message)
        if problem:
            return _result(item, "held", "Queued notice fails current delivery preflight.", "Review the source, session and exact approval internally.")
        if in_quiet_hours(now.astimezone(tz), *policies.quiet_hours()):
            return _result(item, "held", "Quiet hours hold this queued notice.", "Review again after " + next_send_time(now.astimezone(tz), *policies.quiet_hours()).isoformat() + ".")
        return _result(item, "queued", "Provider queue or dispatch claim recorded; device delivery is unverified.", "Verify the existing native attempt; do not create a duplicate.")
    if approval and approval.status == "pending":
        if in_quiet_hours(now.astimezone(tz), *policies.quiet_hours()):
            return _result(item, "held", "Quiet hours hold this exact review.", "Review after quiet hours; an expired review must be replaced.")
        return _result(item, "awaiting-review", "A source-bound exact text is ready for human review.", "Open the existing exact approval.")
    if approval and approval.status == "approved":
        return _result(item, "held", "Approved review has no linked queue receipt.", "Reconcile the existing approval before any retry.")
    if value.get("state") in {"source_changed", "blocked_for_review", "blocked_eligibility", "blocked_transport", "blocked_style"}:
        return _result(item, "held", "The existing preparation failed a source, recipient or transport guard.", "Review the held workflow internally before requesting fresh review.")
    if value.get("state") in {"gloo_unavailable", "gloo_blocked"}:
        return _result(item, "held", "Gloo composition is unavailable or held.", "Review Gloo readiness and the existing bounded retry internally.")
    if notice == "scheduled" and row.source != "planner":
        return _result(item, "held", "This assignment source has no automatic initial-notice job.",
                       "Reconcile the existing notice record internally; do not create a duplicate.")
    if not state.settings.automation_enabled or state.settings.demo_mode:
        return _result(item, "held", "Automatic scheduling is paused or uses the demo clock.", "Have the runtime owner verify scheduling configuration; this view cannot activate it.")
    # process_jobs supplies transaction-local review mode for connected reminders,
    # independently of the global mode used by welcomes and ordinary replies.
    if (notice == "day_before" and isinstance(state.provider, MacMessagesProvider)
            and reminders.automatic_enabled(session) and not state.settings.competition_confirmation_required
            and _automatic_problem(session, state, row, None, now)):
        return _result(item, "held", "Automatic reminder recipient authorization changed.",
                       "Have the runtime owner verify the current signed recipient session.")
    if not state.settings.gloo_api_key:
        return _result(item, "held", "Gloo credentials are not configured.", "Have the runtime owner configure and verify Gloo.")
    if notice == "day_before" and now.astimezone(tz).date() < day_before:
        return _result(item, "scheduled", "The local day-before window has not opened.", "Wait for that window; timer uptime and delivery remain unverified.")
    if in_quiet_hours(now.astimezone(tz), *policies.quiet_hours()):
        return _result(item, "held", "Quiet hours hold notice preparation.", "Review after " + next_send_time(now.astimezone(tz), *policies.quiet_hours()).isoformat() + ".")
    return _result(item, "scheduled", "The existing notice workflow is due but has not prepared a review.", "Have the runtime owner verify the existing scheduler; this view does not run it.")


def snapshot(session, state, *, limit=100, offset=0):
    now = state.clock.now()
    policies = PolicyStore(session)
    # Recent/upcoming actual placements only; proposed offers are not assignments.
    rows = session.scalars(select(m.Assignment).join(m.Shift).join(m.Event).where(
        m.Assignment.status.in_(("approved", "confirmed", "cancelled", "completed")),
        m.Shift.ends_at >= now - timedelta(days=1)).options(
            selectinload(m.Assignment.volunteer).selectinload(m.Volunteer.qualifications),
            selectinload(m.Assignment.shift).selectinload(m.Shift.event),
            selectinload(m.Assignment.shift).selectinload(m.Shift.role))
        .order_by(m.Shift.starts_at, m.Assignment.id).offset(offset).limit(limit + 1)).all()
    more = len(rows) > limit
    rows = rows[:limit]
    keys = [f"job:{kind}:{row.id}" for row in rows for kind in ("assignment", "reminder")]
    jobs = {p.key:p for p in session.scalars(select(m.Policy).where(m.Policy.key.in_(keys)))}
    # Audit conversation_source / human_review rows are deliberately excluded.
    reservations = session.scalars(select(m.Notification).where(
        m.Notification.purpose == "conversation_delivery",
        m.Notification.detail["assignment_id"].as_integer().in_([r.id for r in rows]))).all()
    return {"generated_at":now.isoformat(), "timezone":str(policies.church_tz()), "read_only":True,
        "runtime":{"provider":"mock" if isinstance(state.provider, MockSMSProvider) else state.settings.sms_provider,
                   "automation_configured":state.settings.automation_enabled,
                   "confirmation_required":state.settings.competition_confirmation_required,
                   "gloo_configured":bool(state.settings.gloo_api_key),
                   "messages_connection":"not_checked", "scheduler_running":"not_checked"},
        "notifications":[_item(session, row, notice, state, policies, now, jobs, reservations)
                         for row in rows for notice in ("scheduled", "day_before")],
        "next_offset":offset + limit if more else None}


@router.get("/api/notification-status")
def notification_status(request: Request, limit: int = Query(100, ge=1, le=100),
                        offset: int = Query(0, ge=0), user=Depends(admin), session=Depends(read_session)):
    return snapshot(session, request.app.state, limit=limit, offset=offset)
