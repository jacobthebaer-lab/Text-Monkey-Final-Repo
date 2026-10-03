"""Gloo-composed workflow texts with durable, source-bound exact review."""
import hashlib
import json
from datetime import datetime, timedelta
from sqlalchemy import select
from app.core import confirmations, eligibility
from app.core.policies import PolicyStore, in_quiet_hours
from app.core.send_gate import has_open_sensitive_escalation
from app.core.signup_responder import compose_signup_reply
from app.db import models as m
from app.llm.gloo_client import GlooUnavailableError
from app.sms.mock_provider import MockSMSProvider


def fingerprint(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, separators=(",", ":")).encode()).hexdigest()


def assignment_source(row, purpose):
    return {"type": "assignment", "assignment_id": row.id, "purpose": purpose,
            "shift_id": row.shift_id, "event_id": row.shift.event_id,
            "role_id": row.shift.role_id, "role_name": row.shift.role.name,
            "event_title": row.shift.event.title, "starts_at": row.shift.event.starts_at.isoformat(),
            "ends_at": row.shift.event.ends_at.isoformat(), "volunteer_id": row.volunteer_id}


def summary_source(session, day, now, tz):
    shifts = session.execute(select(m.Shift.id, m.Event.starts_at).join(m.Event).where(
        m.Event.status == "scheduled", m.Event.starts_at > now,
        m.Event.starts_at < now + timedelta(days=2))).all()
    slots = [{"shift_id": shift_id, "starts_at": starts_at.isoformat(),
              "assignments": sorted(session.scalars(select(m.Assignment.id).where(
                  m.Assignment.shift_id == shift_id, m.Assignment.status.in_(("approved", "confirmed")))).all())}
             for shift_id, starts_at in shifts if starts_at.astimezone(tz).date().isoformat() == day]
    return {"type": "summary", "date": day, "slots": sorted(slots, key=lambda item: item["shift_id"])}


def source_problem(session, volunteer, source, now):
    """Recheck before composition, exact approval, claim and native preflight."""
    session.flush()
    if volunteer is not None:
        session.refresh(volunteer)
        session.expire(volunteer, ["qualifications"])
    if volunteer is None or volunteer.status != "active":
        return "workflow recipient is no longer active"
    opted_out = session.get(m.Policy, "sms_opt_out:" + volunteer.phone)
    if not volunteer.sms_opt_in or (opted_out and opted_out.value.get("value")) or has_open_sensitive_escalation(session, volunteer.id):
        return "workflow recipient needs consent or human care"
    tz = PolicyStore(session).church_tz()
    if source.get("type") == "assignment":
        row = session.get(m.Assignment, source.get("assignment_id"))
        if row is not None:
            session.refresh(row)
            session.refresh(row.shift)
            session.refresh(row.shift.event)
            session.refresh(row.shift.role)
        if (row is None or row.volunteer_id != volunteer.id or row.status not in ("approved", "confirmed")
                or row.shift.event.status != "scheduled" or row.shift.event.starts_at <= now):
            return "reminder assignment is no longer current"
        if assignment_source(row, source["purpose"]) != source:
            return "reminder assignment details changed"
        if source["purpose"] == "confirmation" and row.source != "planner":
            return "assignment is no longer a planner assignment"
        if source["purpose"] == "reminder" and row.shift.event.starts_at.astimezone(tz).date() != now.astimezone(tz).date() + timedelta(days=1):
            return "day-before reminder is no longer due"
        if not eligibility.check(session, volunteer, row.shift, str(tz), _exclude_assignment_id=row.id):
            return "assignment recipient is no longer eligible"
    elif source.get("type") == "availability":
        approval = session.get(m.Approval, source.get("collection_id"))
        if approval is not None:
            session.refresh(approval)
        from app.core.scheduler import bounds
        if (approval is None or approval.kind != "collect_availability" or approval.status != "approved"
                or approval.payload.get("month") != source.get("month")):
            return "availability collection is no longer approved"
        if now >= bounds(source["month"], str(tz))[1]:
            return "availability collection month has ended"
        if volunteer.is_coordinator or volunteer.is_pastor:
            return "availability recipient is not a volunteer participant"
        if session.scalar(select(m.Availability.id).where(m.Availability.volunteer_id == volunteer.id,
                                                         m.Availability.month == source["month"])):
            return "availability was already supplied"
        if source.get("reminder"):
            initial = session.get(m.Policy, f"job:availability:{approval.id}:{volunteer.id}:0")
            review = session.get(m.Approval, initial.value.get("approval_id")) if initial and initial.value.get("approval_id") else None
            message_id = initial and (initial.value.get("message_id") or (review and review.payload.get("message_id")))
            delivered = session.get(m.Message, message_id) if message_id else None
            if delivered is None or delivered.status not in ("sent", "submitted", "delivered") or now < delivered.created_at + timedelta(days=3):
                return "availability reminder requires an initial ask and three-day wait"
    elif source.get("type") == "summary":
        if not volunteer.is_coordinator:
            return "summary recipient is no longer a coordinator"
        if now.astimezone(tz).date().isoformat() >= source["date"]:
            return "summary is no longer due"
        if summary_source(session, source["date"], now, tz) != source:
            return "summary coverage changed"
    else:
        return "workflow source is missing or unsupported"
    return None


def delivery_problem(session, approval, now):
    receipt = session.get(m.Policy, approval.payload.get("workflow_job_key"))
    value = receipt.value if receipt else {}
    if (not receipt or value.get("approval_id") != approval.id
            or value.get("source_hash") != approval.payload.get("workflow_source_hash")
            or value.get("phone") != approval.payload.get("phone")
            or value.get("body") != approval.payload.get("body")):
        return "workflow review no longer matches its saved source"
    return source_problem(session, session.get(m.Volunteer, value.get("volunteer_id")), value.get("source", {}), now)


def once(ctx, key, volunteer, body, purpose, *, source=None, required_phrases=()):
    """Count queue submissions, never staged reviews. Fail closed without Gloo.

    Connected providers require exact review; composed bodies and pending reviews
    survive scheduler ticks. Rejected reviews and uncertain sends never auto-retry.
    """
    if not isinstance(ctx.provider, MockSMSProvider) and not confirmations.enabled(ctx.session):
        return False
    now = ctx.clock.now()
    source = source or {}
    if source_problem(ctx.session, volunteer, source, now):
        return False
    selected = None
    if hasattr(ctx.provider, "allows"):
        selected = getattr(ctx.provider, "test_sessions", {}).get(volunteer.phone)
        if not ctx.provider.allows(volunteer.phone) or selected is None or not selected.active(now):
            return False
    key = "job:" + key
    receipt = ctx.session.scalar(select(m.Policy).where(m.Policy.key == key).with_for_update())
    value = dict(receipt.value) if receipt else {}
    prior = ctx.session.get(m.Approval, value["approval_id"]) if value.get("approval_id") else None
    if value.get("message_id") or (prior and prior.payload.get("message_id")) or value.get("state") in ("uncertain", "gloo_blocked"):
        return False
    signature = fingerprint({"source": source, "phone": volunteer.phone, "purpose": purpose,
                             "facts": body, "required": list(required_phrases),
                             "session_id": selected.id if selected else None})
    if value.get("source_hash") != signature:
        if prior and prior.status in ("pending", "approved"):
            prior.status = "expired"
        value = {"source": source, "source_hash": signature, "volunteer_id": volunteer.id,
                 "phone": volunteer.phone, "purpose": purpose, "state": "pending"}
        prior = None
    elif prior:
        if prior.status in ("approved", "rejected") or (prior.status == "pending" and confirmations.valid(prior, now)):
            return False
        if prior.status == "pending":
            prior.status = "expired"
    if value.get("retry_at") and now < datetime.fromisoformat(value["retry_at"]):
        return False
    policies = PolicyStore(ctx.session)
    if in_quiet_hours(now.astimezone(policies.church_tz()), *policies.quiet_hours()):
        return False
    if receipt is None:
        receipt = m.Policy(key=key, value=dict(value))
        ctx.session.add(receipt)
    if not value.get("body"):
        try:
            value["body"] = compose_signup_reply(ctx.session, ctx.clock, ctx.gloo, body,
                required_phrases, volunteer=volunteer, require_gloo=True)
        except GlooUnavailableError:
            attempts = value.get("gloo_attempts", 0) + 1
            value.update(state="gloo_unavailable", gloo_attempts=attempts,
                         retry_at=(now + timedelta(minutes=2)).isoformat())
            if attempts == 3:
                value["state"] = "gloo_blocked"
                ctx.session.add(m.Escalation(category="system_error", severity="normal",
                    summary="A planning workflow message needs review because Gloo could not compose it.",
                    related_ids={"workflow_job_key": key}, status="open", created_at=now))
            receipt.value = dict(value)
            ctx.session.flush()
            return False
    # Composition flushes its audit record. Reload mutable source records after
    # the network call rather than validating the ORM's cached schedule.
    ctx.session.expire_all()
    now = ctx.clock.now()
    current = getattr(ctx.provider, "test_sessions", {}).get(volunteer.phone) if selected else None
    scope_changed = selected and (current is None or current.id != selected.id or not current.active(now))
    if source_problem(ctx.session, volunteer, source, now) or volunteer.phone != value["phone"] or scope_changed:
        value["state"] = "source_changed"
        receipt.value = dict(value)
        ctx.session.flush()
        return False
    try:
        outcome = ctx.gate.send(volunteer=volunteer, body=value["body"], purpose=purpose, kind="ai")
    except ValueError:
        if not confirmations.enabled(ctx.session):
            raise
        # Exact-mode staging cannot call a provider. Its preflight failures are
        # review blockers, never uncertain delivery or permission to bypass review.
        value["state"] = "blocked_for_review"
        receipt.value = dict(value)
        ctx.session.flush()
        return False
    except Exception:
        value["state"] = "uncertain"
        receipt.value = dict(value)
        ctx.session.add(m.Escalation(category="system_error", severity="normal",
            summary="Workflow delivery is uncertain; reconcile transport before retrying.",
            related_ids={"workflow_job_key": key}, status="open", created_at=now))
        ctx.session.flush()
        return False
    value.update(state=outcome.status.value, approval_id=outcome.approval_id, message_id=outcome.message_id)
    if outcome.approval_id:
        approval = ctx.session.get(m.Approval, outcome.approval_id)
        payload = {**approval.payload, "workflow_job_key": key, "workflow_source_hash": signature}
        payload["content_hash"] = confirmations.digest(payload)
        approval.payload = payload
    receipt.value = dict(value)
    ctx.session.flush()
    return outcome.sent


def process(ctx):
    now = ctx.clock.now()
    tz = PolicyStore(ctx.session).church_tz()
    local = now.astimezone(tz)
    counts = {"reminders": 0, "confirmations": 0, "summaries": 0}
    for row in ctx.session.scalars(select(m.Assignment).where(m.Assignment.status.in_(("approved", "confirmed")))):
        event = row.shift.event
        if event.status != "scheduled" or event.starts_at <= now:
            continue
        when = event.starts_at.astimezone(tz).strftime("%b %d %I:%M%p")
        role = row.shift.role.name
        instruction = "Reply C to confirm or X if something came up."
        if row.source == "planner":
            body = f"Hi {row.volunteer.name.split()[0]}! You're scheduled for {role} at {when}. Thank you! {instruction}"
            counts["confirmations"] += once(ctx, f"assignment:{row.id}", row.volunteer, body, "confirmation",
                source=assignment_source(row, "confirmation"), required_phrases=(role, when, instruction))
        if event.starts_at.astimezone(tz).date() == local.date() + timedelta(days=1):
            body = f"Hi {row.volunteer.name.split()[0]}! A reminder: {role} tomorrow at {when}. Thank you! {instruction}"
            counts["reminders"] += once(ctx, f"reminder:{row.id}", row.volunteer, body, "reminder",
                source=assignment_source(row, "reminder"), required_phrases=(role, "tomorrow", when, instruction))
    if local.weekday() == 5 and local.hour >= 18:
        coordinator = ctx.session.scalar(select(m.Volunteer).where(
            m.Volunteer.is_coordinator.is_(True), m.Volunteer.status == "active", m.Volunteer.sms_opt_in.is_(True)))
        source = summary_source(ctx.session, (local.date() + timedelta(days=1)).isoformat(), now, tz)
        filled = sum(bool(slot["assignments"]) for slot in source["slots"])
        body = f"Tomorrow: {filled}/{len(source['slots'])} volunteer slots filled. Review gaps and personal-care escalations on the coordinator dashboard."
        if coordinator:
            counts["summaries"] += once(ctx, f"summary:{source['date']}", coordinator, body, "coordinator_notify",
                source=source, required_phrases=(body,))
    return counts
