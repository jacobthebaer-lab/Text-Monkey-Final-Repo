"""Authenticated workflow controls. POSTs reject cross-origin browser mutations."""
from fastapi import APIRouter, Depends, Request, Form, HTTPException
from fastapi.responses import RedirectResponse
from sqlalchemy import select
from app.web.routes import require_admin, db, fill_ctx, render
from app.db import models as m
from app.agents.planning_agent import plan_month, request_collection
from app.agents.capacity_agent import scan
from app.agents.admin_agent import prepare

router=APIRouter(dependencies=[Depends(require_admin)])

def same_origin(request):
    from app.sms.mock_provider import MockSMSProvider
    if not isinstance(request.app.state.provider, MockSMSProvider):
        raise HTTPException(409, "Legacy workflow controls need Gloo composition and exact review before connected use")
    origin=request.headers.get("origin")
    if origin and origin != str(request.base_url).rstrip("/"):
        raise HTTPException(403,"Cross-origin operation blocked")

@router.get("/operations")
def operations(request:Request,session=Depends(db)):
    return render(request,"operations.html",runs=list(session.scalars(select(m.AgentRun).order_by(m.AgentRun.id.desc()).limit(12))),coordinators=list(session.scalars(select(m.Volunteer).where(m.Volunteer.is_coordinator.is_(True)))),month=request.app.state.clock.now().strftime("%Y-%m"))

@router.post("/operations/plan")
def plan(request:Request,month:str=Form(...),session=Depends(db)):
    same_origin(request)
    try:result=plan_month(fill_ctx(request,session),month)
    except ValueError as exc:raise HTTPException(400,str(exc)) from exc
    return RedirectResponse("/approvals",status_code=303)

@router.post("/operations/availability")
def availability(request:Request,month:str=Form(...),session=Depends(db)):
    same_origin(request)
    try:request_collection(fill_ctx(request,session),month)
    except ValueError as exc:raise HTTPException(400,str(exc)) from exc
    return RedirectResponse("/approvals",status_code=303)

@router.post("/operations/capacity")
def capacity(request:Request,session=Depends(db)):
    same_origin(request);scan(fill_ctx(request,session));return RedirectResponse("/flags",status_code=303)

@router.post("/operations/command")
def command(request:Request,command:str=Form(...),coordinator_id:int=Form(...),session=Depends(db)):
    same_origin(request);coordinator=session.get(m.Volunteer,coordinator_id)
    if not coordinator or not coordinator.is_coordinator:raise HTTPException(400,"Choose a coordinator")
    if not command.strip() or len(command)>1000:raise HTTPException(400,"Enter a command under 1000 characters")
    prepare(fill_ctx(request,session),coordinator,command)
    return RedirectResponse("/operations",status_code=303)

@router.post("/operations/calendar")
def calendar(request:Request,session=Depends(db)):
    same_origin(request)
    from app.integrations.gcal import sync
    try:sync(fill_ctx(request,session),settings=request.app.state.settings)
    except ValueError as exc:raise HTTPException(400,str(exc)) from exc
    except Exception as exc:raise HTTPException(502,"Calendar import failed; check credentials and calendar access") from exc
    return RedirectResponse("/schedule",status_code=303)
