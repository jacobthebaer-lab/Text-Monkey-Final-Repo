"""Opt-in exact human confirmation. No provider calls or model calls here."""
import hashlib
import json
from datetime import date, datetime, timedelta, timezone

from sqlalchemy import event, inspect, select, update
from sqlalchemy.orm import Session
from app.db import models as m
from app.sms.transport import transport_name

MODE_KEY = "competition_confirmation_required"
CONTENT_KEYS = ("action", "phone", "volunteer_id", "body", "purpose", "kind", "role_id",
                "fill_request_id", "urgent", "transport", "session_id", "reply_to_message_id",
                "expires_at", "session_starts_at", "reason", "outreach_id", "record", "record_id", "before", "after",
                "workflow_job_key", "workflow_source_hash", "workflow_plan_source", "workflow_plan_timezone",
                "month", "collection_owner_id", "collection_scope", "collection_authorization_expires_at", "conversation",
                "pre_event_source", "workflow_planning_rules", "workflow_pair_source")
RECORD_FIELDS = {
    "Volunteer": ("name", "phone", "status", "sms_opt_in", "is_coordinator", "is_pastor", "preferences"),
    "Assignment": ("shift_id", "volunteer_id", "status", "source"),
    "Qualification": ("volunteer_id", "type", "status", "expires_on", "verified_by", "verified_at"),
    "Event": ("gcal_event_id", "title", "event_type_id", "starts_at", "ends_at", "status"),
    "Shift": ("event_id", "role_id", "slot_index"),
    "Role": ("name", "ministry", "required_qualifications", "criticality", "fill_policy"),
    "RoleRecipe": ("event_type_id", "role_id", "count"),
    "EventType": ("name", "title_patterns"),
    "Availability": ("volunteer_id", "month", "available_dates", "unavailable_dates", "raw_reply", "parsed_at"),
}


def enabled(session):
    return session.info.get(MODE_KEY) is True


def digest(payload):
    content = {k: payload[k] for k in CONTENT_KEYS if k in payload}
    return hashlib.sha256(json.dumps(content, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode()).hexdigest()


def valid(approval, now, expected=None):
    p = approval.payload
    try:
        expires = datetime.fromisoformat(p["expires_at"])
        return (approval.kind in {"confirm_text", "confirm_record", "confirm_collection"} and
                isinstance(p.get("content_hash"), str) and p["content_hash"] == digest(p) and
                (expected is None or expected == p["content_hash"]) and
                expires.tzinfo is not None and now < expires)
    except (KeyError, TypeError, ValueError):
        return False


def stage(session, now, payload, *, record=False):
    p = {**payload, "expires_at": payload.get("expires_at", (now + timedelta(hours=2)).isoformat())}
    p["content_hash"] = digest(p)
    # Repeated job ticks must not create another identical pending review.
    query = select(m.Approval).where(m.Approval.kind == ("confirm_record" if record else "confirm_text"), m.Approval.status == "pending")
    if p.get("phone"):
        query = query.where(m.Approval.payload["phone"].as_string() == p["phone"])
    if p.get("session_id"):
        query = query.where(m.Approval.payload["session_id"].as_string() == p["session_id"], m.Approval.requested_at >= datetime.fromisoformat(p["session_starts_at"]))
    for prior in session.scalars(query):
        if {k:v for k,v in prior.payload.items() if k in CONTENT_KEYS and k != "expires_at"} == {k:v for k,v in p.items() if k in CONTENT_KEYS and k != "expires_at"} and valid(prior, now):
            return prior
    a = m.Approval(kind="confirm_record" if record else "confirm_text", payload=p,
                   status="pending", requested_at=now)
    session.add(a)
    session.flush()
    return a


def stage_text(gate, payload):
    now = gate.clock.now()
    if payload.get("fill_request_id") and payload.get("volunteer_id"):
        fill = gate.session.get(m.FillRequest, payload["fill_request_id"])
        outreach = gate.session.scalar(select(m.Outreach).where(m.Outreach.fill_request_id == fill.id, m.Outreach.volunteer_id == payload["volunteer_id"], m.Outreach.tranche == fill.current_tranche)) if fill else None
        payload = {**payload, "outreach_id": outreach.id if outreach else None}
    selected = getattr(gate.provider, "test_sessions", {}).get(payload["phone"])
    if hasattr(gate.provider, "allows"):
        if selected is None or not selected.active(now):
            raise ValueError("An active selected test session is required before review")
        payload = {**payload, "session_id": selected.id, "session_starts_at": selected.starts_at.isoformat(),
                   "expires_at": min(now + timedelta(hours=2), selected.expires_at).isoformat()}
    authority = "signup conversation authorization or human confirmation required" if (
        getattr(selected, "continuous", False) and payload['purpose'] == 'signup_reply') else "human confirmation required"
    return stage(gate.session, now, {**payload, "action": "send_text", "reply_to_message_id": gate.reply_to_message_id,
                                   "reason": f"{payload['purpose'].replace('_', ' ')}; {authority}"})


def delivery_problem(session, provider, approval, now, message=None):
    """Recheck mutable restrictions. Used at approval, claim and native preflight."""
    p = approval.payload
    if approval.status != "approved" or not valid(approval, now):
        return "approval is missing, changed or expired"
    if p.get("workflow_job_key"):
        from app.core.reminders import delivery_problem as workflow_problem
        problem = workflow_problem(session, approval, now)
        if problem:
            return problem
    if message is not None and (p["phone"] != message.phone or p["body"] != message.body or
                                 p["purpose"] != message.purpose or p.get("volunteer_id") != message.volunteer_id or
                                 p.get("message_id") != message.id):
        return "approved recipient or content changed"
    from app.core.notifications import pre_event_approval_problem
    if error := pre_event_approval_problem(session, approval, now, message):
        return error
    from app.core.admin_check_copy import approval_problem as admin_check_problem
    if error := admin_check_problem(session, provider, approval, now):
        return error
    if hasattr(provider, "allows"):
        selected = provider.test_sessions.get(p["phone"])
        if (p.get("transport") != transport_name(provider) or not provider.allows(p["phone"]) or selected is None or
                selected.id != p.get("session_id") or not selected.active(now)):
            return "selected transport or test session changed"
    elif p.get("transport") in {"mac_messages", "google_voice"}:
        return "origin transport changed"
    v = session.get(m.Volunteer, p.get("volunteer_id")) if p.get("volunteer_id") else None
    if v and v.phone != p["phone"]:
        return "volunteer phone changed"
    optout = session.get(m.Policy, "sms_opt_out:" + p["phone"])
    signup = v and p["purpose"] == "signup_reply" and v.preferences.get("signup_source") == "sms" and v.preferences.get("consent_pending") is True
    if p["purpose"] == "signup_reply":
        from app.core.send_gate import cloud_demo_pending
        signup = signup or cloud_demo_pending(provider, session, p["phone"])
    if p["purpose"] != "stop_confirm" and ((optout and optout.value.get("value")) or (v and not v.sms_opt_in and not signup)):
        return "recipient opted out"
    for hold in session.scalars(select(m.Escalation.related_ids).where(m.Escalation.category == "sensitive", m.Escalation.status.in_(("open", "acknowledged")))):
        if hold.get("phone") == p["phone"] or (v and hold.get("volunteer_id") == v.id):
            return "personal concern needs human follow-up"
    if p["purpose"] in {"outreach", "availability_ask"} and v:
        from app.core.policies import PolicyStore
        policies = PolicyStore(session)
        from app.core.send_gate import UNSENT_STATUSES
        others = select(m.Message.created_at).where(m.Message.direction == "out", m.Message.volunteer_id == v.id, m.Message.purpose.in_(("outreach", "availability_ask")), m.Message.status.not_in(UNSENT_STATUSES))
        if message is not None:
            others = others.where(m.Message.id != message.id)
        asks = session.scalars(others).all()
        month = now.astimezone(policies.church_tz()).replace(day=1, hour=0, minute=0, second=0, microsecond=0)
        if sum(a >= month for a in asks) >= policies.ask_budget():
            return "monthly ask budget reached"
        if p["purpose"] == "outreach" and any(a > now - timedelta(hours=int(policies.get("outreach_cooldown_hours"))) for a in asks):
            return "outreach cooldown reached"
    if p["purpose"] == "outreach":
        from app.core import eligibility
        fill = session.get(m.FillRequest, p.get("fill_request_id"))
        slot = session.get(m.Shift, fill.shift_id) if fill else None
        if (not slot or not v or slot.role_id != p.get("role_id") or fill.state not in ("waiting_approval", "in_progress", "escalated") or
                slot.event.starts_at <= now or slot.event.status in ("cancelled", "completed") or
                not eligibility.check(session, v, slot, tz=provider_timezone(session))):
            return "offer closed or volunteer no longer eligible"
    return None


def provider_timezone(session):
    from app.core.policies import PolicyStore
    return PolicyStore(session).get("church_timezone")


def proof_for(session, message):
    proof = session.get(m.Notification, f"confirmation:{message.id}")
    approval = session.get(m.Approval, proof.detail.get("approval_id")) if proof else None
    return approval if approval and proof.detail.get("content_hash") == approval.payload.get("content_hash") else None


def audit(session, approval, now, action, actor, detail=""):
    session.add(m.Notification(key=f"review:{approval.id}:{action}", purpose="human_review", body="", state="sent",
                               due_at=now, created_at=now,
                               detail={"approval_id": approval.id, "action": action, "actor": actor,
                                       "content_hash": approval.payload.get("content_hash"), "detail": detail}))


def decide(session, gate, approval, *, approve, actor, expected, now, ctx=None):
    if approval.kind == "confirm_collection":
        raise ValueError("Review availability collection scope through the signed-in planning workflow")
    a = session.scalar(select(m.Approval).where(m.Approval.id == approval.id).with_for_update().execution_options(populate_existing=True))
    if a.status != "pending":
        raise ValueError("This exact action was already reviewed")
    if not valid(a, now, expected):
        raise ValueError("This review is changed or expired; request a new proposal")
    a.decided_at, a.decided_by, a.via = now, actor, "web"
    a.status = "approved" if approve else "rejected"
    audit(session, a, now, "approve" if approve else "reject", actor)
    if not approve:
        if a.kind == "confirm_text" and a.payload.get("fill_request_id"):
            outreach = session.get(m.Outreach, a.payload["outreach_id"]) if a.payload.get("outreach_id") else None
            if outreach:
                outreach.response = "blocked"
        return ["Exact action rejected; nothing delivered or changed."]
    if a.kind == "confirm_record":
        apply_record(session, a, now)
        return ["Exact record change approved and applied."]
    error = delivery_problem(session, gate.provider, a, now)
    if error:
        a.status = "expired"
        from app.core.notifications import pre_event_approval_problem, invalidate_pre_event_review
        if pre_event_approval_problem(session, a, now):
            invalidate_pre_event_review(session, a, now, error)
        audit(session, a, now, "blocked", actor, error)
        return ["Nothing delivered: " + error]
    gate.reply_to_message_id = a.payload.get("reply_to_message_id")
    result = gate.send(body=a.payload["body"], purpose=a.payload["purpose"], phone=a.payload["phone"],
                       volunteer=session.get(m.Volunteer, a.payload.get("volunteer_id")) if a.payload.get("volunteer_id") else None,
                       kind=a.payload["kind"], role=session.get(m.Role, a.payload.get("role_id")) if a.payload.get("role_id") else None,
                       fill_request_id=a.payload.get("fill_request_id"), urgent=a.payload.get("urgent", False),
                       conversation=a.payload.get('conversation'), _confirmation=a)
    if result.sent:
        a.payload = {**a.payload, "message_id": result.message_id}
        from app.core.notifications import link_pre_event_message
        link_pre_event_message(session, a, result.message_id)
        session.add(m.Notification(key=f"confirmation:{result.message_id}", volunteer_id=a.payload.get("volunteer_id"),
            purpose="human_review", body="", state="sent", due_at=now, created_at=now, message_id=result.message_id,
            detail={"approval_id": a.id, "content_hash": a.payload["content_hash"]}))
        outreach = session.get(m.Outreach, a.payload["outreach_id"]) if a.payload.get("outreach_id") else None
        if outreach:
            outreach.message_id = result.message_id
    else:
        # A quiet-hour hold cannot silently release later; it requires fresh review.
        a.status = "expired"
        from app.core.notifications import pre_event_approval_problem, invalidate_pre_event_review
        if pre_event_approval_problem(session, a, now):
            invalidate_pre_event_review(session, a, now, result.reason)
        audit(session, a, now, "blocked", actor, result.reason)
    if ctx and a.payload.get("fill_request_id"):
        from app.agents.fill_agent import on_outreach_approved
        on_outreach_approved(ctx, a.payload["fill_request_id"])
    session.flush()
    return ["Exact message: " + result.status.value + ("; " + result.reason if result.reason else "")]


def suppress_phone(session, phone):
    session.execute(update(m.Message).where(m.Message.phone == phone, m.Message.direction == "out",
        m.Message.status.in_(("queued", "dispatching")), m.Message.purpose != "stop_confirm").values(status="blocked_opt_out"))
    for a in session.scalars(select(m.Approval).where(m.Approval.kind == "confirm_text", m.Approval.status == "pending")):
        if a.payload.get("phone") == phone and a.payload.get("purpose") != "stop_confirm":
            a.status = "expired"


def json_value(value):
    return value.isoformat() if isinstance(value, (date, datetime)) else value


def authorize_sender_fields(session, obj, fields):
    # Called only after a control or an active setup step recognizes this sender instruction.
    if enabled(session) and session.info.get("sender_phone") == getattr(obj, "phone", None):
        permissions = session.info.setdefault("sender_record_permissions", {})
        permissions[id(obj)] = set(permissions.get(id(obj), ())) | set(fields)


def authorize_sender_assignment(session, volunteer, shift_id, status):
    action = "cancel" if status == "cancelled" else "accept"
    if enabled(session) and session.info.get("sender_phone") == volunteer.phone and session.info.get("sender_schedule_action") == action:
        session.info.setdefault("sender_assignment_permissions", set()).add((shift_id, volunteer.id, status))


def values(obj):
    result = {}
    for key in RECORD_FIELDS[type(obj).__name__]:
        value = getattr(obj, key)
        default = inspect(type(obj)).columns[key].default
        if value is None and (inspect(obj).transient or inspect(obj).pending):
            if default is not None:
                value = default.arg(None) if default.is_callable else default.arg
        result[key] = json_value(value)
    return result


def apply_record(session, approval, now):
    p = approval.payload
    if p.get('record') == 'AssignmentPair':
        from app.core.paired_planning import apply_pair
        apply_pair(session, approval, now)
        return
    if p.get('workflow_planning_rules'):
        from app.core.paired_planning import rule_review_problem
        if problem := rule_review_problem(session, approval):
            raise ValueError(problem)
    if p.get("workflow_plan_source"):
        from app.core.scheduler import planning_problem
        problem = planning_problem(session, approval, now)
        if problem:
            raise ValueError(problem)
    cls = getattr(m, p["record"], None)
    if p["record"] not in RECORD_FIELDS or cls is None or set(p["after"]) - set(RECORD_FIELDS[p["record"]]):
        raise ValueError("Unsupported record change")
    obj = session.scalar(select(cls).where(cls.id == p["record_id"]).with_for_update().execution_options(populate_existing=True)) if p["record_id"] else None
    if obj is not None and values(obj) != p["before"]:
        raise ValueError("Record changed since review; request a new proposal")
    if p["record_id"] and obj is None:
        raise ValueError("Record no longer exists")
    if p["record"] == "Assignment" and p["after"].get("status") in {"approved", "confirmed"}:
        from app.core import eligibility
        slot = session.get(m.Shift, p["after"]["shift_id"])
        volunteer = session.get(m.Volunteer, p["after"]["volunteer_id"])
        if (not slot or not volunteer or slot.event.starts_at <= now or slot.event.status in ("cancelled", "completed") or
                not eligibility.check(session, volunteer, slot, tz=provider_timezone(session), _exclude_assignment_id=obj.id if obj else None)):
            raise ValueError("Assignment is no longer eligible")
        occupied = session.scalar(select(m.Assignment.id).where(m.Assignment.shift_id == slot.id,
            m.Assignment.status.in_(("proposed", "approved", "confirmed")), m.Assignment.id != (obj.id if obj else -1)))
        if occupied:
            raise ValueError("Slot is already occupied")
    old = session.info.get("record_authorized")
    session.info["record_authorized"] = True
    try:
        if obj is None:
            timestamps = {"created_at": now} if p["record"] in {"Assignment", "Volunteer"} else {}
            if p["record"] == "Assignment":
                timestamps["updated_at"] = now
            obj = cls(**timestamps)
            session.add(obj)
        for key, value in p["after"].items():
            if key in {"verified_at", "parsed_at", "starts_at", "ends_at"} and value:
                value = datetime.fromisoformat(value)
            if key == "expires_on" and value:
                value = date.fromisoformat(value)
            setattr(obj, key, value)
        session.flush()
        approval.payload = {**p, "applied_record_id": obj.id}
        if p.get('workflow_planning_rules'):
            from app.core.paired_planning import record_rule_receipt
            record_rule_receipt(session, approval)
    finally:
        session.info["record_authorized"] = old


@event.listens_for(Session, "before_flush")
def hold_automated_records(session, flush_context, instances):
    """A guard for agent/tool initiated roster, consent, qualification and schedule writes.

    Explicit coordinator requests and bounded exact-sender instructions are
    authorized separately. Operational receipts, proposals and timers aren't records of record.
    """
    for obj in session.new:
        if isinstance(obj, m.Escalation) and session.info.get("conversation_origin") == "mock_or_twilio":
            obj.related_ids = {**obj.related_ids, "transport":"mock_or_twilio"}
    if not enabled(session) or session.info.get("record_authorized"):
        return
    now = session.info.get("confirmation_now", datetime.now(timezone.utc))
    for obj in list(session.new) + list(session.dirty) + list(session.deleted):
        label = type(obj).__name__
        if label not in RECORD_FIELDS:
            continue
        state = inspect(obj)
        sender = session.info.get("sender_phone")
        changes = {k for k in RECORD_FIELDS[label] if state.attrs[k].history.has_changes()}
        allowed = session.info.get("sender_record_permissions", {}).get(id(obj), set())
        own = label == "Volunteer" and sender and obj.phone == sender and changes <= allowed
        # Sender YES/cancellation only authorizes their own schedule.
        if label == "Availability" and sender and session.info.get("sender_profile_instruction"):
            volunteer = session.get(m.Volunteer, obj.volunteer_id)
            own = volunteer is not None and volunteer.phone == sender
        if label == "Assignment" and sender:
            own = (obj.shift_id, obj.volunteer_id, obj.status) in session.info.get("sender_assignment_permissions", set())
            if obj not in session.new:
                own = own and changes <= {"status"}
        if own and obj not in session.deleted:
            continue
        if obj in session.deleted:
            raise ValueError("Automated record deletion is unsupported in confirmation mode")
        after = values(obj)
        before = None
        if obj not in session.new:
            with session.no_autoflush:
                stored = session.execute(select(*(getattr(type(obj), k) for k in RECORD_FIELDS[label])).where(type(obj).id == obj.id)).first()
            if stored is None:
                raise ValueError("Record no longer exists")
            before = {k: json_value(stored[i]) for i, k in enumerate(RECORD_FIELDS[label])}
        if before == after:
            continue
        p = {"action": "record_change", "record": label, "record_id": obj.id, "before": before, "after": after,
             "reason": "Automated record change requires coordinator confirmation",
             "expires_at": (now + timedelta(hours=2)).isoformat()}
        selected = session.info.get("mac_test_session")
        p.update(transport=session.info.get("conversation_origin", "mac_messages") if selected else "mock_or_twilio")
        if selected:
            p.update(phone=sender, session_id=selected.id, expires_at=min(now+timedelta(hours=2), selected.expires_at).isoformat())
        p["content_hash"] = digest(p)
        session.add(m.Approval(kind="confirm_record", status="pending", payload=p, requested_at=now))
        if obj in session.new:
            session.expunge(obj)
        else:
            for key, value in before.items():
                if key in {"verified_at", "parsed_at", "starts_at", "ends_at"} and value:
                    value = datetime.fromisoformat(value)
                if key == "expires_on" and value:
                    value = date.fromisoformat(value)
                setattr(obj, key, value)


@event.listens_for(Session, "do_orm_execute")
def refuse_unreviewed_bulk_writes(execution):
    if not enabled(execution.session) or execution.session.info.get("record_authorized"):
        return
    statement = execution.statement
    protected = {getattr(m, label).__tablename__ for label in RECORD_FIELDS}
    if (execution.is_update or execution.is_delete or execution.is_insert) and getattr(getattr(statement, "table", None), "name", None) in protected:
        raise ValueError("Automated bulk record writes are unsupported; stage exact ORM changes for review")
    from sqlalchemy.sql.elements import TextClause
    if isinstance(statement, TextClause):
        import re
        if re.search(r"\b(insert|update|delete|alter|drop|truncate|replace)\b", statement.text, re.I):
            raise ValueError("Raw record writes are unsupported in confirmation mode")
