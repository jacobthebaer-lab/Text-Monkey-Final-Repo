"""Verified, allowlisted admin setup API. Owner comes only from Supabase /user.

All queries scope by server-derived owner/workspace. Never accept tenant or owner
IDs from clients. Staged records never enter the scheduling store. Only the
explicit saved-admin connection check invokes Gloo and the Messages provider.
"""
import json
import re
import time
from datetime import datetime, timezone
from uuid import UUID, uuid4
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from fastapi import APIRouter, Depends, HTTPException, Request, UploadFile, File
from sqlalchemy import select, update
from sqlalchemy.exc import SQLAlchemyError, IntegrityError

from app.admin_setup.imports import MAX_BYTES, parse_file, preview, normalize_phone
from app.admin_setup.models import Workspace, StagedContact, ImportBatch
from app.web.routes import db
from app.web.texty import admin
from app.db import models as m
from app.sms.mac_provider import MacMessagesProvider
from app.sms.transport import session_transport, transport_name, queue_result

router = APIRouter(prefix="/api/setup", tags=["Coordinator setup"])
DEFAULTS = {"country": "US", "timezone": "America/Denver", "quiet_start": "21:00", "quiet_end": "07:00", "monthly_ask_limit": 4}
TEXT_FIELDS = {"church_name": 160, "affiliation": 160, "address": 240, "city": 100, "region": 100,
               "postal_code": 30, "country": 20, "timezone": 80, "coordinator_name": 160,
               "coordinator_role": 100, "coordinator_phone": 40, "church_size": 40, "service_times": 500, "ministries": 500,
               "website": 240, "church_phone": 40, "quiet_start": 5, "quiet_end": 5}


def now():
    return datetime.now(timezone.utc)


def owner(user):
    try:
        return str(UUID(user["id"]))
    except (KeyError, TypeError, ValueError):
        raise HTTPException(403, "A verified administrator identity is required.")


async def payload(request):
    chunks, size = [], 0
    async for chunk in request.stream():
        size += len(chunk)
        if size > 6 * 1024 * 1024:
            raise HTTPException(413, "Import is too large. Use at most 2,000 contact rows.")
        chunks.append(chunk)
    try:
        data = json.loads(b"".join(chunks))
        if not isinstance(data, dict):
            raise ValueError()
        return data
    except (ValueError, UnicodeDecodeError):
        raise HTTPException(422, "Provide a valid setup request.")


def workspace(session, user, *, lock=False):
    query = select(Workspace).where(Workspace.owner_id == owner(user))
    if lock:
        query = query.with_for_update()
    try:
        return session.scalar(query)
    except SQLAlchemyError:
        session.rollback()
        raise HTTPException(503, "Church setup storage needs its reviewed migration. Existing login and scheduling remain available.")


def snapshot(w):
    return {"details": {**DEFAULTS, **(w.details if w else {})}, "completed": bool(w and w.completed),
            "revision": w.revision if w else 0, "saved_at": w.updated_at.isoformat() if w else None,
            "scheduling_scope": "existing_single_church", "delivery_enabled": False}


def validate_details(details, complete=False):
    if not isinstance(details, dict) or set(details) - (set(TEXT_FIELDS) | {"monthly_ask_limit"}):
        raise ValueError("Use supported church setup fields.")
    clean = dict(DEFAULTS)
    for key, limit in TEXT_FIELDS.items():
        value = details.get(key, clean.get(key, ""))
        if not isinstance(value, str) or len(value.strip()) > limit or any(ord(c) < 32 and c not in "\n\t" for c in value):
            raise ValueError(f"Check {key.replace('_', ' ')} (up to {limit} characters).")
        clean[key] = value.strip()
    if clean["country"] not in {"US", "CA", "international"}:
        raise ValueError("Choose United States, Canada or another country.")
    if clean["coordinator_phone"]:
        try:
            clean["coordinator_phone"] = normalize_phone(clean["coordinator_phone"], clean["country"])
        except ValueError as exc:
            raise ValueError(f"Check your mobile number: {exc}")
    try:
        ZoneInfo(clean["timezone"])
    except (ZoneInfoNotFoundError, ValueError):
        raise ValueError("Use a valid timezone, such as America/Denver.")
    for key in ("quiet_start", "quiet_end"):
        if not re.fullmatch(r"(?:[01][0-9]|2[0-3]):[0-5][0-9]", clean[key]):
            raise ValueError("Quiet hours must use valid 24-hour times.")
    if clean["quiet_start"] == clean["quiet_end"]:
        raise ValueError("Choose different quiet-hour start and end times.")
    limit = details.get("monthly_ask_limit", 4)
    if type(limit) is not int or not 1 <= limit <= 8:
        raise ValueError("Choose 1–8 monthly asks per volunteer.")
    clean["monthly_ask_limit"] = limit
    if clean["website"] and not re.fullmatch(r"https?://[^\s]+", clean["website"]):
        raise ValueError("Website must start with https:// or http://.")
    if complete:
        for field in ("church_name", "address", "city", "region", "postal_code", "coordinator_name", "coordinator_role", "coordinator_phone"):
            if not clean[field]:
                raise ValueError(f"Add your {field.replace('_', ' ')} before finishing setup.")
    return clean


def text_recipients(session, user, *, lock=False):
    query = select(m.Volunteer).where(
        m.Volunteer.preferences["admin_text_owner"].as_string() == owner(user)
    )
    return session.scalars(query.with_for_update() if lock else query).all()


def admin_text_status(request, session, user, w):
    recipients = text_recipients(session, user)
    recipient = next((v for v in recipients if v.status == "active"), None)
    phone = recipient.phone if recipient else (w.details.get("coordinator_phone", "") if w else "")
    opt_out = session.get(m.Policy, "sms_opt_out:" + phone) if phone else None
    stopped = bool((recipient and not recipient.sms_opt_in) or (opt_out and opt_out.value.get("value")))
    enabled = bool(recipient and recipient.sms_opt_in and not stopped)
    settings, provider = request.app.state.settings, request.app.state.provider
    mac = isinstance(provider, MacMessagesProvider)
    cloud = transport_name(provider) == "google_voice"
    selected = provider.test_sessions.get(phone) if session_transport(provider) else None
    delivery_now = request.app.state.mac_delivery_clock.now()
    session_active = bool(selected and selected.active(delivery_now))
    if not phone:
        session_issue = "Save your mobile number before connecting its Messages session."
    elif not selected:
        session_issue = "No Messages session is configured for this mobile number. Ask the church owner to connect it."
    elif delivery_now < selected.starts_at:
        session_issue = "The Messages session for this number has not started yet. Wait until its start time below."
    else:
        session_issue = "The texting test for this number has expired. Ask the church owner to renew it."
    checks = []
    def check(code, label, ready, success, missing, action="connection-help", next_step=""):
        checks.append({"code": code, "label": label, "ready": bool(ready),
                       "detail": success if ready else missing, "action": None if ready else action,
                       "next_step": "" if ready else next_step})
    check("setup", "Church setup", w and w.completed, "Your church setup is complete.",
          "Finish church setup before sending admin updates.", "setup")
    check("recipient", "Your mobile and consent", enabled, "Your saved mobile is enrolled for admin updates.",
          "This number opted out. Text START to the church line before enabling updates again." if stopped else
          "Admin text updates are off. Save your mobile number and turn them on below.",
          "connection-help" if stopped else "mobile",
          "Text START from this same mobile to the church line, then refresh this checklist." if stopped else "")
    check("gloo", "Gloo AI", settings.gloo_api_key and not settings.demo_mode,
          "Gloo is configured. Each outgoing text still requires successful Gloo composition.",
          "Gloo AI is disconnected. The church owner needs to restore it before texts can be written.",
          next_step="Have the church owner restore the Gloo connection on the live backend. Texts stay unsent if Gloo fails.")
    if cloud:
        from app.integrations.google_voice_runtime import get_cloud_status
        cloud_status = get_cloud_status(request.app.state, session)
        check("transport", "Cloud Google Voice", settings.google_voice_enabled and not settings.demo_mode,
              "Google Voice is the configured cloud transport.", "Cloud Google Voice is disabled.",
              next_step="Have a superadmin configure the cloud connection in Settings.")
        check("allowlist", "Enabled recipient", phone and provider.allows(phone),
              "Your saved mobile is an enabled cloud test recipient.",
              "Your saved mobile needs an approved cloud test session.", "connection-help" if phone else "mobile")
        check("session", "Cloud test session", session_active,
              "Your saved mobile has an active cloud test session.",
              "Start or renew the bounded cloud test session for your saved mobile.")
        check("bridge", "Cloud connection", cloud_status["ready"],
              "The cloud connector is connected and sending is enabled.",
              "Cloud sending is paused or the verified connection is unavailable.",
              next_step="Have a superadmin check the Google Voice session, live sending setting and pause control.")
    else:
        check("transport", "Laptop Messages", mac and not settings.demo_mode, "Laptop Messages is the configured transport.",
              "The laptop Messages connection is not configured for real delivery.",
              next_step="Have the church owner connect Text Monkey to Messages on the sending laptop.")
        check("allowlist", "Enabled recipient", mac and phone and provider.allows(phone),
              "Your saved mobile is in the enabled recipients.",
              "This mobile number is outside the enabled test recipients. Ask the church owner to enable it." if phone else
              "Save your mobile number before checking enabled recipients.", "connection-help" if phone else "mobile",
              "Have the church owner enable this exact saved mobile on the laptop connection." if phone else "")
        check("session", "Messages session", session_active, "Your saved mobile has an active Messages session.", session_issue,
              "connection-help" if phone else "mobile",
              "Have the church owner connect or renew the Messages session for this exact saved mobile; then refresh.")
        check("bridge", "Laptop online", mac and time.monotonic() - getattr(request.app.state, "mac_last_poll", 0) < 180,
              "The laptop Messages bridge has checked in within the last three minutes.",
              "The laptop Messages connection is offline. Open the laptop and restart its Messages bridge.",
              next_step="Open Messages on the sending laptop and have the church owner restart its bridge. Keep the laptop awake and online.")
    check_ready = all(item["ready"] for item in checks)
    check("scheduler", "Scheduled updates", settings.automation_enabled and not settings.demo_mode,
          "Background scheduling is running. Quiet hours and consent still apply.",
          "Automatic scheduling is paused. The church owner needs to start it before scheduled updates can run.",
          next_step="Have the church owner start background scheduling on the live backend. A one-time connection check can run while scheduling is paused.")
    issues = [item["detail"] for item in checks if not item["ready"]]
    pending_check = session.scalar(select(m.Notification).where(
        m.Notification.key.startswith(f"admin-check:{owner(user)}:"),
        m.Notification.volunteer_id == recipient.id,
        m.Notification.state == "pending", m.Notification.message_id.is_(None),
        m.Notification.expires_at > request.app.state.clock.now()
    ).order_by(m.Notification.created_at.desc()).limit(1)) if recipient else None
    recent = session.scalars(select(m.Message).where(
        m.Message.volunteer_id.in_([v.id for v in recipients]),
        m.Message.purpose.in_(("coordinator_notify", "escalation_notify")),
        m.Message.direction == "out").order_by(m.Message.created_at.desc()).limit(5)).all() if recipients else []
    return {"phone": phone, "enabled": enabled, "ready": not issues, "issues": issues,
            "checks": checks, "connection_check_ready": check_ready,
            "session_starts_at": selected.starts_at.isoformat() if selected else None,
            "session_expires_at": selected.expires_at.isoformat() if selected else None,
            "pending_check": {"request_id": pending_check.key.rsplit(":", 1)[1],
                              "retry_at": pending_check.due_at.isoformat()} if pending_check else None,
            "review_required": settings.competition_confirmation_required, "pre_event_hours": 3,
            "recent": [{"body": row.body, "status": row.status, "created_at": row.created_at.isoformat()} for row in recent]}


@router.get("/admin-texts")
def get_admin_texts(request: Request, user=Depends(admin), session=Depends(db)):
    return admin_text_status(request, session, user, workspace(session, user))


@router.post("/admin-texts")
async def save_admin_texts(request: Request, user=Depends(admin), session=Depends(db)):
    data = await payload(request)
    if set(data) - {"phone", "enabled", "consent"} or type(data.get("enabled")) is not bool:
        raise HTTPException(422, "Choose whether to enable your admin text updates.")
    w = workspace(session, user, lock=True)
    if not w or not w.completed:
        raise HTTPException(409, "Finish your church setup before enabling admin updates.")
    recipients = text_recipients(session, user, lock=True)
    current = None
    if data["enabled"]:
        if data.get("consent") is not True or not isinstance(data.get("phone"), str) or len(data["phone"]) > 40:
            raise HTTPException(422, "Confirm this is your mobile number and you want admin updates.")
        try:
            phone = normalize_phone(data["phone"], w.details.get("country", "US"))
        except ValueError as exc:
            raise HTTPException(422, f"Check your mobile number: {exc}")
        current = session.scalar(select(m.Volunteer).where(m.Volunteer.phone == phone).with_for_update())
        stopped = session.get(m.Policy, "sms_opt_out:" + phone)
        if stopped and stopped.value.get("value"):
            raise HTTPException(409, "This number opted out. Text START to the church line before enabling updates again.")
        if current and current not in recipients:
            raise HTTPException(409, "This number already belongs to another record. Use your own mobile number or ask the church owner to review the existing record.")
        if current and not current.sms_opt_in:
            raise HTTPException(409, "This number opted out. Text START to the church line before enabling updates again.")
        if current is None:
            current = m.Volunteer(name=w.details["coordinator_name"][:120], phone=phone,
                is_coordinator=True, sms_opt_in=True, status="active",
                preferences={"admin_text_owner": owner(user), "signup_source": "admin_settings"},
                created_at=request.app.state.clock.now())
            session.add(current)
        current.status = "active"
        current.preferences = {**current.preferences, "admin_text_consent_at": now().isoformat()}
        w.details = {**w.details, "coordinator_phone": phone}
        w.revision += 1
        w.updated_at = now()
    for recipient in recipients:
        if recipient is not current:
            recipient.status = "inactive"
            session.execute(update(m.Message).where(m.Message.volunteer_id == recipient.id,
                m.Message.direction == "out", m.Message.purpose.in_(("coordinator_notify", "escalation_notify")),
                m.Message.status.in_(("draft", "queued", "dispatching"))).values(status="blocked_admin_updates"))
            for notification in session.scalars(select(m.Notification).where(
                m.Notification.volunteer_id == recipient.id, m.Notification.state == "pending")):
                notification.state = "expired"
    try:
        session.flush()
    except IntegrityError:
        session.rollback()
        raise HTTPException(409, "This mobile number changed in another request. Reload and try again.")
    return admin_text_status(request, session, user, w)


@router.post("/admin-texts/send-check")
async def send_admin_check(request: Request, user=Depends(admin), session=Depends(db)):
    """A real, idempotent Gloo-composed text to this account's saved recipient."""
    data = await payload(request)
    try:
        if set(data) != {"request_id"}:
            raise ValueError()
        request_id = str(UUID(data["request_id"]))
    except (KeyError, ValueError, TypeError, AttributeError):
        raise HTTPException(422, "Provide a unique request ID for this connection check.")
    w = workspace(session, user, lock=True)
    if not w or not w.completed:
        raise HTTPException(409, "Finish church setup before sending your admin check.")
    recipient = next((v for v in text_recipients(session, user, lock=True)
                      if v.status == "active" and v.sms_opt_in), None)
    if recipient is None:
        raise HTTPException(409, "Save your mobile number and enable admin updates first.")
    stopped = session.get(m.Policy, "sms_opt_out:" + recipient.phone)
    if stopped and stopped.value.get("value"):
        raise HTTPException(409, "This number opted out. Text START to the church line before enabling updates again.")
    key = f"admin-check:{owner(user)}:{request_id}"
    previous = session.scalar(select(m.Notification).where(m.Notification.key == key)
                              .with_for_update().execution_options(populate_existing=True))
    if previous and previous.volunteer_id != recipient.id:
        raise HTTPException(409, "Your admin mobile number changed. Start a new connection check for the saved number.")
    state = request.app.state
    if state.settings.demo_mode or not state.settings.gloo_api_key or not session_transport(state.provider):
        raise HTTPException(503, "Real texting needs Gloo AI and a configured texting connection.")
    if not state.provider.allows(recipient.phone):
        raise HTTPException(403, "Your saved mobile number is outside the enabled recipients.")
    selected = state.provider.test_sessions.get(recipient.phone)
    if not selected:
        raise HTTPException(409, "No Messages session is configured for your saved mobile number.")
    if state.mac_delivery_clock.now() < selected.starts_at:
        raise HTTPException(409, "The Messages session for your saved mobile number has not started yet.")
    if not selected.active(state.mac_delivery_clock.now()):
        raise HTTPException(409, "The Messages session for your saved mobile number has expired.")
    if transport_name(state.provider) == "google_voice":
        from app.integrations.google_voice_runtime import get_cloud_status
        if not get_cloud_status(state, session)["ready"]:
            raise HTTPException(503, "Cloud texting is paused or disconnected. Ask a superadmin to check the connection.")
    elif time.monotonic() - getattr(state, "mac_last_poll", 0) >= 180:
        raise HTTPException(503, "The laptop Messages connection is offline. Restart its bridge.")
    from app.agents.fill_agent import FillContext
    from app.core.notifications import deliver, _dispatch
    context = FillContext(session, state.clock, state.provider, state.gloo)
    if previous is not None:
        notice = previous
        # Retry the same durable request after its backoff, even with scheduling
        # paused. The row lock also prevents a concurrent scheduler duplicate.
        if notice.state == "pending" and not notice.message_id and notice.due_at <= state.clock.now():
            _dispatch(context, notice)
    else:
        notice = deliver(context, key=key, purpose="coordinator_notify", volunteer=recipient,
            body="Text Monkey admin connection check. Event updates include coverage, open roles, and your next step.")
    session.flush()
    message = session.get(m.Message, notice.message_id) if notice.message_id else None
    return {"delivery": queue_result(state.provider) if message and message.status in {"queued", "dispatching", "submitted", "sent", "delivered"} else notice.state,
            "message_id": notice.message_id, "status": message.status if message else notice.state,
            "retry_at": notice.due_at.isoformat() if notice.state == "pending" else None}


@router.get("")
def get_setup(user=Depends(admin), session=Depends(db)):
    w = workspace(session, user)
    result = snapshot(w)
    result["account_setup_available"] = w is None and account_details(user) is not None
    return result


def account_details(user):
    """Editable profile data only; never grants access, consent or permissions."""
    metadata = user.get("user_metadata")
    details = metadata.get("church_setup") if isinstance(metadata, dict) else None
    if details is None:
        return None
    try:
        return validate_details(details, complete=True)
    except ValueError:
        return None


@router.post("/from-account")
async def setup_from_account(request: Request, user=Depends(admin), session=Depends(db)):
    # admin verifies the actual Supabase user, confirmed email and allowlist first.
    if await payload(request) != {}:
        raise HTTPException(422, "Account details come from your verified account.")
    w = workspace(session, user, lock=True)
    if w is not None:
        return snapshot(w)  # Never overwrite a saved profile or an unfinished draft.
    details = account_details(user)
    if details is None:
        raise HTTPException(409, "Finish your church details to complete your account.")
    w = Workspace(id=str(uuid4()), owner_id=owner(user), details=details,
                  completed=True, revision=1, updated_at=now())
    session.add(w)
    try:
        session.flush()
    except IntegrityError:
        session.rollback()
        return snapshot(workspace(session, user))
    return snapshot(w)


@router.post("")
async def save_setup(request: Request, user=Depends(admin), session=Depends(db)):
    data = await payload(request)
    if set(data) - {"details", "revision", "complete"} or type(data.get("revision")) is not int or type(data.get("complete", False)) is not bool:
        raise HTTPException(422, "Provide church details, revision and completion status only.")
    w = workspace(session, user, lock=True)
    if data["revision"] != (w.revision if w else 0):
        raise HTTPException(409, "Setup changed in another tab. Reload saved setup before editing.")
    incoming = data.get("details")
    # New clients omit the retired field; retain existing values for old clients.
    if isinstance(incoming, dict) and w and "affiliation" not in incoming:
        incoming = {**incoming, "affiliation": w.details.get("affiliation", "")}
    try:
        details = validate_details(incoming, data.get("complete", False))
    except ValueError as exc:
        raise HTTPException(422, str(exc))
    if w is None:
        w = Workspace(id=str(uuid4()), owner_id=owner(user), revision=0, updated_at=now())
        session.add(w)
    w.details, w.completed, w.updated_at = details, data.get("complete", False), now()
    w.revision += 1
    try:
        session.flush()
    except IntegrityError:
        session.rollback()
        raise HTTPException(409, "Setup was saved by another request. Reload saved setup before editing.")
    return snapshot(w)


@router.post("/parse")
async def parse_upload(file: UploadFile = File(...), user=Depends(admin)):
    # Authentication executes before this endpoint reads the selected file.
    owner(user)
    try:
        data = await file.read(MAX_BYTES + 1)
        return {"sheets": parse_file(file.filename or "", data)}
    except ValueError as exc:
        raise HTTPException(422, str(exc))
    finally:
        await file.close()


def preview_data(session, w, data):
    if set(data) - {"rows", "mapping", "source", "country", "preview_hash", "submission_id"}:
        raise ValueError("Use supported import fields only; consent cannot be imported.")
    existing = session.scalars(select(StagedContact.phone).where(StagedContact.workspace_id == w.id)).all()
    return preview(data.get("rows"), data.get("mapping"), data.get("country", "US"), data.get("source"), existing)


@router.post("/preview")
async def preview_import(request: Request, user=Depends(admin), session=Depends(db)):
    data = await payload(request)
    w = workspace(session, user)
    if not w:
        raise HTTPException(409, "Save your church setup before importing contacts.")
    try:
        result, _ = preview_data(session, w, data)
        return result
    except ValueError as exc:
        raise HTTPException(422, str(exc))


@router.post("/import")
async def import_contacts(request: Request, user=Depends(admin), session=Depends(db)):
    data = await payload(request)
    try:
        submission = str(UUID(data.get("submission_id", "")))
    except (ValueError, TypeError, AttributeError):
        raise HTTPException(422, "Preview the import before saving.")
    w = workspace(session, user, lock=True)
    if not w:
        raise HTTPException(409, "Save your church setup before importing contacts.")
    batch = session.scalar(select(ImportBatch).where(ImportBatch.workspace_id == w.id, ImportBatch.submission_id == submission))
    if batch:
        if data.get("preview_hash") != batch.fingerprint:
            raise HTTPException(409, "This submission was already used for another preview.")
        return batch.result
    try:
        result, contacts = preview_data(session, w, data)
    except ValueError as exc:
        raise HTTPException(422, str(exc))
    if not contacts:
        raise HTTPException(422, "There are no valid new contacts to save.")
    if data.get("preview_hash") != result["preview_hash"]:
        raise HTTPException(409, "Contacts changed since preview. Preview again before saving.")
    total = session.scalars(select(StagedContact.id).where(StagedContact.workspace_id == w.id)).all()
    if len(total) + len(contacts) > 10000:
        raise HTTPException(422, "The MVP supports 10,000 staged contacts per workspace.")
    for contact in contacts:
        session.add(StagedContact(id=str(uuid4()), workspace_id=w.id, created_at=now(), **contact))
    saved = {"imported": len(contacts), "counts": result["counts"], "consent": "not_recorded", "texts_sent": 0}
    session.add(ImportBatch(id=str(uuid4()), workspace_id=w.id, submission_id=submission,
                            fingerprint=result["preview_hash"], result=saved, created_at=now()))
    try:
        session.flush()
    except IntegrityError:
        session.rollback()
        raise HTTPException(409, "Another import changed these contacts. Reload staged contacts and preview again.")
    return saved


@router.get("/contacts")
def contacts(user=Depends(admin), session=Depends(db)):
    w = workspace(session, user)
    rows = session.scalars(select(StagedContact).where(StagedContact.workspace_id == w.id).order_by(StagedContact.created_at, StagedContact.id)).all() if w else []
    return {"contacts": [{"id": row.id, "name": row.name, "phone": row.phone, "email": row.email,
                          "ministry": row.ministry, "source": row.source, "consent": "not_recorded",
                          "status": "staged", "can_text": False} for row in rows], "texts_sent": 0}


@router.delete("/contacts/{contact_id}")
def remove_contact(contact_id: str, user=Depends(admin), session=Depends(db)):
    w = workspace(session, user, lock=True)
    row = session.scalar(select(StagedContact).where(StagedContact.id == contact_id, StagedContact.workspace_id == w.id)) if w else None
    if not row:
        raise HTTPException(404, "Staged contact not found.")
    session.delete(row)
    return {"removed": True}
