"""Signed-in collection scope review. No runtime activation or text approval."""
import json
import re
import threading
from fastapi import APIRouter, Depends, HTTPException, Request
from sqlalchemy import select
from starlette.concurrency import run_in_threadpool
from app.agents.fill_agent import FillContext
from app.core import availability_review as review, confirmations
from app.db import models as m
from app.web.admin_setup import owner
from app.web.texty import admin

router = APIRouter(prefix="/api/planning/availability-collections", tags=["Planning workflow review"])
# SQLite demo is one process; Postgres additionally locks each parent row. Commit
# stays inside this lock so concurrent preparation cannot reuse an old receipt.
_lock = threading.RLock()


async def body(request, fields):
    if request.headers.get("content-type", "").split(";", 1)[0] != "application/json":
        raise HTTPException(422, "Provide a JSON planning request.")
    raw = bytearray()
    async for chunk in request.stream():
        raw.extend(chunk)
        if len(raw) > 4096: raise HTTPException(413, "Planning request is too large.")
    try:
        value = json.loads(raw)
        if not isinstance(value, dict) or set(value)-fields: raise ValueError()
        return value
    except (ValueError, UnicodeDecodeError):
        raise HTTPException(422, "Use only the supported planning request fields.") from None


def envelope(session, parent, now):
    collection = review.snapshot(session, parent, now)
    return {"collection":collection, "text_review_ids":collection["text_review_ids"],
            "sent":0, "delivery_enabled":False, "scheduler_activated":False}


def transaction(request, user, operation):
    owner_id = owner(user)
    with _lock, request.app.state.session_factory() as session:
        if not request.app.state.settings.competition_confirmation_required or not confirmations.enabled(session):
            raise HTTPException(409, "Availability collection requires exact human confirmation mode.")
        session.info["record_authorized"] = False
        session.info["confirmation_now"] = request.app.state.clock.now()
        result = operation(session, owner_id)
        session.commit()
        return result


def parent_for(session, ident, owner_id):
    parent = review.owned(session, ident, owner_id, lock=True)
    if parent is None: raise HTTPException(404, "Collection review not found.")
    return parent


@router.get("")
async def collections(request: Request, user=Depends(admin)):
    def operation(session, owner_id):
        parents = session.scalars(select(m.Approval).where(m.Approval.kind == review.KIND,
            m.Approval.payload["collection_owner_id"].as_string() == owner_id)
            .order_by(m.Approval.id.desc())).all()
        return {"collections":[review.snapshot(session, p, request.app.state.clock.now()) for p in parents]}
    return await run_in_threadpool(transaction, request, user, operation)


@router.post("")
async def request_collection(request: Request, user=Depends(admin)):
    data = await body(request, {"month", "request_id"})
    if "month" not in data or ("request_id" in data and not isinstance(data["request_id"], str)):
        raise HTTPException(422, "Provide a month and, optionally, a UUID request_id.")
    def operation(session, owner_id):
        try:
            parent = review.request_review(session, owner_id, data["month"], request.app.state.clock.now(), data.get("request_id"))
        except review.RequestConflict as error:
            raise HTTPException(409, str(error)) from None
        except (ValueError, TypeError, AttributeError) as error:
            raise HTTPException(422, str(error)) from None
        return envelope(session, parent, request.app.state.clock.now())
    return await run_in_threadpool(transaction, request, user, operation)


@router.get("/{ident}")
async def collection(request: Request, ident: int, user=Depends(admin)):
    return await run_in_threadpool(transaction, request, user,
        lambda session, owner_id: envelope(session, parent_for(session, ident, owner_id), request.app.state.clock.now()))


@router.post("/{ident}/{action}")
async def decision(request: Request, ident: int, action: str, user=Depends(admin)):
    if action not in {"approve", "reject", "retry"}: raise HTTPException(404)
    data = await body(request, {"content_hash"})
    if not isinstance(data.get("content_hash"), str) or not re.fullmatch(r"[0-9a-f]{64}", data["content_hash"]):
        raise HTTPException(422, "Provide the content_hash from the displayed collection review.")
    def operation(session, owner_id):
        parent = parent_for(session, ident, owner_id)
        try:
            if action == "retry":
                state = request.app.state
                review.prepare_one(FillContext(session, state.clock, state.provider, state.gloo), parent, owner_id, data["content_hash"])
            else:
                review.decide(session, parent, owner_id, request.app.state.clock.now(), data["content_hash"], action == "approve")
        except ValueError as error:
            raise HTTPException(409, str(error)) from None
        return envelope(session, parent, request.app.state.clock.now())
    return await run_in_threadpool(transaction, request, user, operation)
