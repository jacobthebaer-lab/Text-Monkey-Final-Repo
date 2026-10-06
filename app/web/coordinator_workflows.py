"""Signed-in coordinator commands and capacity review, with no send controls."""
import threading

from fastapi import APIRouter, Depends, HTTPException, Request
from sqlalchemy import select
from starlette.concurrency import run_in_threadpool

from app.agents.admin_agent import prepare
from app.agents.capacity_agent import scan
from app.agents.fill_agent import FillContext
from app.core import confirmations
from app.db import models as m
from app.web.planning_workflows import body
from app.web.texty import admin

router = APIRouter(prefix="/api/coordinator", tags=["Coordinator workflow review"])
_lock = threading.RLock()


def transaction(request, operation):
    state = request.app.state
    with _lock, state.session_factory() as session:
        if not state.settings.competition_confirmation_required or not confirmations.enabled(session):
            raise HTTPException(409, "Coordinator workflows require exact human review mode.")
        session.info.update(record_authorized=False, confirmation_now=state.clock.now())
        result = operation(FillContext(session, state.clock, state.provider, state.gloo))
        session.commit()
        return result


@router.get("")
async def context(request: Request, user=Depends(admin)):
    def operation(ctx):
        return {"coordinators": [{"id": c.id, "name": c.name} for c in ctx.session.scalars(
            select(m.Volunteer).where(m.Volunteer.is_coordinator.is_(True), m.Volunteer.status == "active"))],
            "review_required": True, "delivery_enabled": False}
    return await run_in_threadpool(transaction, request, operation)


@router.post("/command")
async def command(request: Request, user=Depends(admin)):
    data = await body(request, {"command", "coordinator_id"})
    if (type(data.get("coordinator_id")) is not int or not isinstance(data.get("command"), str)
            or not 0 < len(data["command"].strip()) <= 1000):
        raise HTTPException(422, "Choose an existing coordinator and a request of 1-1000 characters.")
    def operation(ctx):
        coordinator = ctx.session.get(m.Volunteer, data["coordinator_id"])
        if not coordinator or not coordinator.is_coordinator or coordinator.status != "active":
            raise HTTPException(422, "Choose an active coordinator.")
        result = prepare(ctx, coordinator, data["command"].strip())
        return {**result, "reviews": [{"id": a.id, "content_hash": a.payload["content_hash"],
            "record": a.payload["record"], "before": a.payload["before"], "after": a.payload["after"],
            "status": a.status, "expires_at": a.payload["expires_at"]}
            for identity in result["approval_ids"] if (a := ctx.session.get(m.Approval, identity))],
            "state": "pending_exact_review" if result["approval_ids"] and result["outcome"] == "completed"
                else "answered" if result["outcome"] == "completed" else "held",
            "delivery_enabled": False, "sent": 0}
    return await run_in_threadpool(transaction, request, operation)


def flag_snapshot(flag):
    return {"id": flag.id, "kind": flag.kind, "type": flag.type, "summary": flag.summary,
        "evidence": flag.evidence, "suggested_action": flag.suggested_action, "status": flag.status}


@router.get("/capacity")
async def capacity(request: Request, user=Depends(admin)):
    return await run_in_threadpool(transaction, request, lambda ctx: {
        "flags": [flag_snapshot(f) for f in ctx.session.scalars(select(m.Flag).order_by(m.Flag.id))],
        "delivery_enabled": False})


@router.post("/capacity")
async def review_capacity(request: Request, user=Depends(admin)):
    await body(request, set())
    def operation(ctx):
        flags = scan(ctx)
        return {"flags": [flag_snapshot(f) for f in flags], "delivery_enabled": False, "sent": 0,
            "state": "ready" if all(f.evidence.get("narration", {}).get("state") == "ready" for f in flags) else "held"}
    return await run_in_threadpool(transaction, request, operation)
