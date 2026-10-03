"""Owner-bound approval of a frozen monthly collection scope, never text approval."""
from datetime import datetime, timedelta, timezone
import re
from uuid import UUID
from sqlalchemy import select
from app.core import confirmations, scheduler
from app.core.policies import PolicyStore
from app.core.reminders import fingerprint, source_problem
from app.core.send_gate import UNSENT_STATUSES
from app.db import models as m

KIND = "confirm_collection"

class RequestConflict(ValueError):
    pass


def check_month(month, now, tz):
    if not isinstance(month, str) or not re.fullmatch(r"[0-9]{4}-[0-9]{2}", month):
        raise ValueError("Choose a month in YYYY-MM format.")
    try:
        start, end = scheduler.bounds(month, str(tz))
    except (TypeError, ValueError, OverflowError):
        raise ValueError("Choose a valid month in YYYY-MM format.") from None
    local = now.astimezone(tz)
    offset = (start.year-local.year)*12 + start.month-local.month
    if not 0 <= offset <= 12:
        raise ValueError("Choose the current month or one of the next twelve months.")
    return end


def scope(session, month, now):
    policies = PolicyStore(session)
    check_month(month, now, policies.church_tz())
    local = now.astimezone(policies.church_tz())
    month_start = local.replace(day=1, hour=0, minute=0, second=0, microsecond=0)
    availability = set(session.scalars(select(m.Availability.volunteer_id).where(m.Availability.month == month)))
    care = session.scalars(select(m.Escalation.related_ids).where(
        m.Escalation.category == "sensitive", m.Escalation.status.in_(("open", "acknowledged")))).all()
    care_ids = {item.get("volunteer_id") for item in care}
    care_phones = {item.get("phone") for item in care}
    asks = session.execute(select(m.Message.volunteer_id).where(m.Message.direction == "out",
        m.Message.purpose.in_(("outreach", "availability_ask")), m.Message.status.not_in(UNSENT_STATUSES),
        m.Message.created_at >= month_start)).all()
    counts = {}
    for (vid,) in asks: counts[vid] = counts.get(vid, 0)+1
    recipients, excluded = [], {}
    volunteers = session.scalars(select(m.Volunteer).order_by(m.Volunteer.id)
        .execution_options(populate_existing=True)).all()
    for volunteer in volunteers:
        optout = session.get(m.Policy, "sms_opt_out:" + volunteer.phone)
        reason = ("inactive" if volunteer.status != "active" else
                  "not_serving_participant" if volunteer.is_coordinator or volunteer.is_pastor else
                  "no_consent" if not volunteer.sms_opt_in or (optout and optout.value.get("value")) else
                  "availability_supplied" if volunteer.id in availability else
                  "human_follow_up" if volunteer.id in care_ids or volunteer.phone in care_phones else
                  "ask_limit" if counts.get(volunteer.id, 0) >= policies.ask_budget() else None)
        if reason:
            excluded[reason] = excluded.get(reason, 0)+1
        else:
            recipients.append({"volunteer_id":volunteer.id, "name":volunteer.name, "phone":volunteer.phone})
    if len(recipients) > 2000:
        raise ValueError("Collection scope exceeds the demo's 2,000-recipient review limit.")
    return {"scheduling_scope":"existing_single_church", "timezone":str(policies.church_tz()),
            "recipient_count":len(recipients), "recipients":recipients, "excluded_counts":excluded}


def intact(parent):
    return parent.kind == KIND and parent.payload.get("content_hash") == confirmations.digest(parent.payload)


def owned(session, ident, owner_id, *, lock=False):
    query = select(m.Approval).where(m.Approval.id == ident, m.Approval.kind == KIND,
        m.Approval.payload["collection_owner_id"].as_string() == owner_id)
    return session.scalar(query.with_for_update().execution_options(populate_existing=True) if lock else query)


def request_review(session, owner_id, month, now, request_id=None):
    owner_id = str(UUID(owner_id))
    request_id = str(UUID(request_id)) if request_id is not None else None
    tz = PolicyStore(session).church_tz()
    end = check_month(month, now, tz)
    # Fits the existing policies.key VARCHAR(80), including on PostgreSQL.
    key = "collection:" + fingerprint({"owner_id":owner_id,"request_id":request_id}) if request_id else None
    if key and (receipt := session.get(m.Policy, key)):
        if receipt.value.get("month") != month:
            raise RequestConflict("This request ID already belongs to a different month.")
        parent = owned(session, receipt.value["parent_id"], owner_id)
        if parent is None:
            raise ValueError("Collection request receipt no longer matches its owner.")
        return parent
    parents = session.scalars(select(m.Approval).where(m.Approval.kind == KIND,
        m.Approval.payload["collection_owner_id"].as_string() == owner_id,
        m.Approval.payload["month"].as_string() == month).order_by(m.Approval.id.desc())).all()
    current = scope(session, month, now)
    parent = next((p for p in parents if p.status == "approved" and intact(p)
                   and p.payload.get("collection_scope") == current), None)
    if parent is None:
        parent = next((p for p in parents if p.status == "pending" and confirmations.valid(p, now)
                       and p.payload.get("collection_scope") == current), None)
    if parent is None:
        for previous in parents:
            if previous.status in ("pending", "approved"): previous.status = "expired"
        payload = {"action":"collect_availability", "month":month, "collection_owner_id":owner_id,
            "collection_scope":current, "collection_authorization_expires_at":end.isoformat(),
            "expires_at":(now+timedelta(hours=2)).isoformat(),
            "reason":"Approve this month and recipient scope only; every text requires a separate exact review."}
        payload["content_hash"] = confirmations.digest(payload)
        parent = m.Approval(kind=KIND, status="pending", payload=payload, requested_at=now)
        session.add(parent); session.flush()
    if key:
        session.add(m.Policy(key=key, value={"month":month,"parent_id":parent.id})); session.flush()
    return parent


def approved_collection_problem(session, child, now):
    """Prove signed-in owner approval, frozen recipients and live month authorization."""
    if child is None or child.kind != "collect_availability" or child.status != "approved":
        return "Availability collection is not approved."
    payload = child.payload
    parent = session.get(m.Approval, payload.get("parent_review_id")) if payload.get("parent_review_id") else None
    if parent is None:
        return "Availability collection requires signed-in month and scope approval."
    session.refresh(parent)
    p = parent.payload
    try:
        recipients = [r["volunteer_id"] for r in p["collection_scope"]["recipients"]]
        owner_id = str(UUID(p["collection_owner_id"]))
        expiry = datetime.fromisoformat(p["collection_authorization_expires_at"])
        review_expiry = datetime.fromisoformat(p["expires_at"])
        valid = (intact(parent) and parent.status == "approved" and parent.decided_by == owner_id
            and parent.via == "web" and parent.decided_at is not None
            and parent.requested_at <= parent.decided_at < review_expiry
            and expiry.tzinfo is not None and now < expiry
            and p.get("collection_id") == child.id and payload.get("month") == p["month"]
            and payload.get("collection_owner_id") == owner_id
            and payload.get("recipient_ids") == recipients
            and payload.get("scope_hash") == fingerprint(p["collection_scope"])
            and p["collection_scope"]["timezone"] == str(PolicyStore(session).church_tz()))
        if not valid:
            return "Availability month/scope authorization changed or expired."
    except (ValueError, TypeError, KeyError):
        return "Availability month/scope authorization is invalid."
    return None


def decide(session, parent, owner_id, now, expected, approve):
    if parent.payload.get("collection_owner_id") != owner_id or not intact(parent) or expected != parent.payload.get("content_hash"):
        raise ValueError("Review the exact displayed collection hash before deciding.")
    desired = "approved" if approve else "rejected"
    if approve and now >= datetime.fromisoformat(parent.payload["collection_authorization_expires_at"]):
        raise ValueError("The collection month authorization expired; request a current scope.")
    if parent.status == desired:
        return parent  # A retry never runs Gloo or creates another child.
    if parent.status != "pending" or not confirmations.valid(parent, now, expected):
        raise ValueError("This collection review is changed, expired or already decided; request a new scope.")
    if approve:
        try:
            current = scope(session, parent.payload["month"], now)
        except ValueError:
            raise ValueError("The collection month is stale; request a new scope.") from None
        if current != parent.payload["collection_scope"]:
            raise ValueError("Recipient scope changed; request and review the current scope.")
        if not current["recipients"]:
            raise ValueError("No eligible recipients remain in this collection scope.")
        child = m.Approval(kind="collect_availability", status="approved", requested_at=now,
            decided_at=now, decided_by=owner_id, via="web", payload={"month":parent.payload["month"],
            "parent_review_id":parent.id, "collection_owner_id":owner_id,
            "recipient_ids":[r["volunteer_id"] for r in current["recipients"]], "scope_hash":fingerprint(current)})
        session.add(child); session.flush()
        parent.payload = {**parent.payload, "collection_id":child.id}
    parent.status, parent.decided_at, parent.decided_by, parent.via = desired, now, owner_id, "web"
    confirmations.audit(session, parent, now, "approve" if approve else "reject", owner_id)
    session.flush()
    return parent


def preparation(session, parent, now):
    child = session.get(m.Approval, parent.payload.get("collection_id")) if parent.payload.get("collection_id") else None
    reviews, remaining, held, retry_at = [], [], [], []
    if child is None:
        return child, reviews, remaining, held, retry_at
    problem = approved_collection_problem(session, child, now)
    if problem:
        return child, reviews, remaining, [problem], retry_at
    for recipient in parent.payload["collection_scope"]["recipients"]:
        vid = recipient["volunteer_id"]
        volunteer = session.get(m.Volunteer, vid)
        source = {"type":"availability","collection_id":child.id,"month":child.payload["month"],"reminder":False}
        if problem := source_problem(session, volunteer, source, now):
            if "approved scope" in problem: held.append("Recipient details changed; request and approve a fresh collection scope.")
            continue
        receipt = session.get(m.Policy, f"job:availability:{child.id}:{vid}:0")
        value = receipt.value if receipt else {}
        review = session.get(m.Approval, value.get("approval_id")) if value.get("approval_id") else None
        if value.get("message_id") or (review and review.payload.get("message_id")): continue
        if review and review.status == "pending" and confirmations.valid(review, now):
            reviews.append(review.id); continue
        if review and review.status in ("approved", "rejected"): continue
        if value.get("state") in ("uncertain", "gloo_blocked"):
            held.append("A preparation needs operator review; no text was sent by this workflow."); continue
        if value.get("retry_at") and now < datetime.fromisoformat(value["retry_at"]):
            retry_at.append(value["retry_at"]); held.append("Gloo preparation is waiting for its retry time."); continue
        remaining.append(vid)
    return child, reviews, remaining, held, retry_at


def prepare_one(ctx, parent, owner_id, expected):
    if expected != parent.payload.get("content_hash") or not intact(parent) or parent.payload.get("collection_owner_id") != owner_id:
        raise ValueError("Review the exact approved collection hash before preparing texts.")
    if parent.status != "approved":
        raise ValueError("Approve the month and recipient scope before preparing texts.")
    child, reviews, remaining, held, retry_at = preparation(ctx.session, parent, ctx.clock.now())
    if problem := approved_collection_problem(ctx.session, child, ctx.clock.now()):
        raise ValueError(problem)
    if remaining:
        from app.agents.planning_agent import collect
        collect(ctx, child, recipient_ids=[remaining[0]])


def snapshot(session, parent, now):
    child, reviews, remaining, held, retry_at = preparation(session, parent, now)
    status = parent.status
    if status == "pending" and not confirmations.valid(parent, now): status = "expired"
    if status in ("pending", "approved") and now >= datetime.fromisoformat(parent.payload["collection_authorization_expires_at"]): status = "expired"
    composition = ("not_started" if child is None or (remaining and not reviews and not held) else
                   "held" if held else "reviews_pending" if reviews else "no_remaining_recipients")
    return {"id":parent.id, "month":parent.payload["month"], "status":status,
        "content_hash":parent.payload["content_hash"], "expires_at":parent.payload["expires_at"],
        "authorization_expires_at":parent.payload["collection_authorization_expires_at"],
        "decided_at":parent.decided_at.astimezone(timezone.utc).isoformat() if parent.decided_at else None,
        "scope":parent.payload["collection_scope"], "collection_id":child.id if child else None,
        "composition_status":composition, "text_review_ids":reviews,
        "retry_at":min(retry_at) if retry_at else None, "hold_reason":held[0] if held else None,
        "remaining_recipient_count":len(remaining)}
