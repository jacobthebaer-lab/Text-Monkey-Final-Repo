"""Verified, allowlisted admin setup API. Owner comes only from Supabase /user.

All queries scope by server-derived owner/workspace. Never accept tenant or owner
IDs from clients. The existing scheduling store is single church; no staged
record enters that store, and no endpoint invokes Gloo, signup or an SMS provider.
"""
import json
import re
from datetime import datetime, timezone
from uuid import UUID, uuid4
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from fastapi import APIRouter, Depends, HTTPException, Request, UploadFile, File
from sqlalchemy import select
from sqlalchemy.exc import SQLAlchemyError, IntegrityError

from app.admin_setup.imports import MAX_BYTES, parse_file, preview
from app.admin_setup.models import Workspace, StagedContact, ImportBatch
from app.web.routes import db
from app.web.texty import admin

router = APIRouter(prefix="/api/setup", tags=["Coordinator setup"])
DEFAULTS = {"country": "US", "timezone": "America/Denver", "quiet_start": "21:00", "quiet_end": "07:00", "monthly_ask_limit": 4}
TEXT_FIELDS = {"church_name": 160, "affiliation": 160, "address": 240, "city": 100, "region": 100,
               "postal_code": 30, "country": 20, "timezone": 80, "coordinator_name": 160,
               "coordinator_role": 100, "church_size": 40, "service_times": 500, "ministries": 500,
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
        for field in ("church_name", "address", "city", "region", "postal_code", "coordinator_name", "coordinator_role"):
            if not clean[field]:
                raise ValueError(f"Add your {field.replace('_', ' ')} before finishing setup.")
    return clean


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
