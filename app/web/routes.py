"""Admin web pages, phone simulator, and demo controls (PLAN.md section 16).

Simple Jinja2 pages, no frontend framework. Everything is protected by a
single ADMIN_PASSWORD via HTTP Basic (documented as a known gap); with the
password unset (local dev), pages are open.
"""

import secrets
from datetime import date, datetime, timedelta
from functools import partial
from pathlib import Path
from zoneinfo import ZoneInfo

from fastapi import APIRouter, Depends, Form, HTTPException, Request
from fastapi.responses import RedirectResponse
from fastapi.security import HTTPBasic, HTTPBasicCredentials
from fastapi.templating import Jinja2Templates
from sqlalchemy import select

from app.agents.fill_agent import FillContext
from app.core.inbound import decide_approval, handle_inbound
from app.core.send_gate import SendGate
from app.db import models as m
from app.db.seed import SEED_ANCHOR, seed
from app.db.session import reset_db
from app.jobs import process_jobs
from app.llm.parser import parse_inbound

templates = Jinja2Templates(directory=Path(__file__).resolve().parent / "templates")

VISIBLE_ASSIGNMENT_STATUSES = ("proposed", "approved", "confirmed", "completed")
OPEN_FILL_STATES = ("open", "in_progress", "waiting_approval")

_basic = HTTPBasic(auto_error=False)


def require_admin(request: Request, credentials: HTTPBasicCredentials | None = Depends(_basic)):
    password = request.app.state.settings.admin_password
    if not password:
        return
    if credentials is None or not secrets.compare_digest(credentials.password, password):
        raise HTTPException(status_code=401, headers={"WWW-Authenticate": "Basic"})


def db(request: Request):
    session = request.app.state.session_factory()
    try:
        yield session
        session.commit()
    except Exception:
        session.rollback()
        raise
    finally:
        session.close()


def fill_ctx(request: Request, session) -> FillContext:
    state = request.app.state
    return FillContext(session, state.clock, state.provider, state.gloo)


def _tz(request: Request) -> ZoneInfo:
    return ZoneInfo(request.app.state.settings.church_timezone)


def _localdt(value: datetime) -> str:
    from app.config import get_settings

    local = value.astimezone(ZoneInfo(get_settings().church_timezone))
    return local.strftime("%a %b %-d, %-I:%M%p").replace("AM", "am").replace("PM", "pm")


templates.env.filters["localdt"] = _localdt

router = APIRouter(dependencies=[Depends(require_admin)])


def render(request: Request, template: str, **context):
    state = request.app.state
    context.update(
        request=request,
        demo_mode=state.settings.demo_mode,
        now_local=_localdt(state.clock.now()),
    )
    return templates.TemplateResponse(request, template, context)


def _grid_rows(session, events, roles):
    rows = []
    for event in events:
        by_role: dict[int, list] = {}
        for shift in event.shifts:
            by_role.setdefault(shift.role_id, []).append(shift)
        cells = []
        for role in roles:
            role_shifts = by_role.get(role.id)
            if not role_shifts:
                cells.append(None)
                continue
            names = []
            for shift in sorted(role_shifts, key=lambda s: s.slot_index):
                active = [a for a in shift.assignments if a.status in VISIBLE_ASSIGNMENT_STATUSES]
                names.append(active[0].volunteer.name.split()[0] if active else None)
            cells.append(names)
        rows.append({"event": event, "cells": cells})
    return rows


# --- pages -------------------------------------------------------------------


@router.get("/")
def dashboard(request: Request, session=Depends(db)):
    now = request.app.state.clock.now()
    roles = session.scalars(select(m.Role).order_by(m.Role.id)).all()
    events = session.scalars(
        select(m.Event)
        .where(m.Event.starts_at >= now, m.Event.starts_at <= now + timedelta(days=14))
        .order_by(m.Event.starts_at)
    ).all()
    upcoming = _grid_rows(session, events, roles)
    gap_count = sum(
        1 for row in upcoming for cell in row["cells"] if cell for name in cell if name is None
    )

    fills = []
    for fr in session.scalars(
        select(m.FillRequest).where(m.FillRequest.state.in_(OPEN_FILL_STATES)).order_by(m.FillRequest.created_at.desc())
    ):
        shift = session.get(m.Shift, fr.shift_id)
        outreach = session.scalars(select(m.Outreach).where(m.Outreach.fill_request_id == fr.id)).all()
        fills.append(
            {
                "row": fr,
                "shift_text": f"{shift.role.name} — {_localdt(shift.event.starts_at)}",
                "asked": len(outreach),
                "yes": sum(1 for o in outreach if o.response == "yes"),
                "no": sum(1 for o in outreach if o.response == "no"),
                "next_action": _localdt(fr.next_action_at) if fr.next_action_at else None,
            }
        )

    escalations = session.scalars(
        select(m.Escalation).where(m.Escalation.status.in_(("open", "acknowledged"))).order_by(m.Escalation.created_at.desc())
    ).all()
    flags = session.scalars(select(m.Flag).where(m.Flag.status == "open")).all()
    return render(request, "dashboard.html", roles=roles, upcoming=upcoming, gap_count=gap_count,
                  fills=fills, escalations=escalations, flags=flags)


@router.get("/approvals")
def approvals(request: Request, session=Depends(db)):
    def view(row):
        vid = row.payload.get("volunteer_id")
        volunteer = session.get(m.Volunteer, vid) if vid else None
        fill_text = None
        frid = row.payload.get("fill_request_id")
        if frid and (fr := session.get(m.FillRequest, frid)):
            shift = session.get(m.Shift, fr.shift_id)
            fill_text = f"{shift.role.name} — {_localdt(shift.event.starts_at)}"
        return {"row": row, "to_name": volunteer.name if volunteer else None, "fill_text": fill_text}

    pending = [view(a) for a in session.scalars(
        select(m.Approval).where(m.Approval.status == "pending").order_by(m.Approval.requested_at)
    )]
    decided = [view(a) for a in session.scalars(
        select(m.Approval).where(m.Approval.status != "pending").order_by(m.Approval.decided_at.desc()).limit(15)
    )]
    return render(request, "approvals.html", pending=pending, decided=decided)


@router.post("/approvals/{approval_id}/{decision}")
def decide(request: Request, approval_id: int, decision: str, session=Depends(db)):
    if decision not in ("approve", "reject"):
        raise HTTPException(404)
    approval = session.get(m.Approval, approval_id)
    if approval is None or approval.status != "pending":
        raise HTTPException(404, "no such pending approval")
    state = request.app.state
    from app.sms.mac_provider import MacMessagesProvider
    from app.sms.mock_provider import MockSMSProvider

    provider = state.provider
    if isinstance(provider, MacMessagesProvider) and approval.payload.get("transport") != "mac_messages":
        provider = MockSMSProvider()
    gate = SendGate(session, state.clock, provider)
    decide_approval(
        session, gate, approval, approve=decision == "approve",
        decided_by="Coordinator (web)", via="web", now=state.clock.now(),
        ctx=FillContext(session, state.clock, provider, state.gloo),
    )
    return RedirectResponse("/approvals", status_code=303)


@router.get("/schedule")
def schedule(request: Request, month: str | None = None, session=Depends(db)):
    tz = _tz(request)
    now_local = request.app.state.clock.now().astimezone(tz)
    try:
        year, mon = map(int, (month or now_local.strftime("%Y-%m")).split("-"))
        start = datetime(year, mon, 1, tzinfo=tz)
    except ValueError:
        raise HTTPException(400, "month must look like 2026-10")
    end = datetime(year + (mon == 12), (mon % 12) + 1, 1, tzinfo=tz)

    roles = session.scalars(select(m.Role).order_by(m.Role.id)).all()
    events = session.scalars(
        select(m.Event).where(m.Event.starts_at >= start, m.Event.starts_at < end).order_by(m.Event.starts_at)
    ).all()
    prev_m = (start - timedelta(days=1)).strftime("%Y-%m")
    next_m = end.strftime("%Y-%m")
    return render(request, "schedule.html", grid=_grid_rows(session, events, roles), roles=roles,
                  month_label=start.strftime("%B %Y"), prev_month=prev_m, next_month=next_m)


@router.get("/needs")
def needs(request: Request, session=Depends(db)):
    roles = session.scalars(select(m.Role).order_by(m.Role.id)).all()
    role_names = {r.id: r.name for r in roles}
    event_types = []
    for et in session.scalars(select(m.EventType).order_by(m.EventType.id)):
        recipes = [
            {"row": r, "role_name": role_names.get(r.role_id, "?")}
            for r in session.scalars(select(m.RoleRecipe).where(m.RoleRecipe.event_type_id == et.id))
        ]
        event_types.append({"type": et, "recipes": recipes})
    return render(request, "needs.html", roles=roles, event_types=event_types)


@router.post("/needs/recipe/{recipe_id}")
def update_recipe(request: Request, recipe_id: int, count: int = Form(...), session=Depends(db)):
    recipe = session.get(m.RoleRecipe, recipe_id)
    if recipe is None:
        raise HTTPException(404)
    recipe.count = max(0, min(20, count))
    return RedirectResponse("/needs", status_code=303)


@router.get("/volunteers")
def volunteers(request: Request, session=Depends(db)):
    now = request.app.state.clock.now()
    soon = (now + timedelta(days=30)).date()
    all_vols = session.scalars(select(m.Volunteer).order_by(m.Volunteer.name)).all()
    names = {v.id: v.name for v in all_vols}
    rows = []
    for vol in all_vols:
        quals = [
            {
                "type": q.type, "status": q.status, "expires_on": q.expires_on,
                "expiring": q.status == "verified" and q.expires_on is not None and q.expires_on <= soon,
            }
            for q in vol.qualifications
        ]
        partner_id = vol.preferences.get("serves_with_volunteer_id")
        rows.append({"vol": vol, "quals": quals, "partner": names.get(partner_id)})
    return render(request, "volunteers.html", rows=rows)


@router.get("/flags")
def flags(request: Request, session=Depends(db)):
    rows = session.scalars(select(m.Flag).order_by(m.Flag.created_at.desc())).all()
    return render(request, "flags.html", flags=rows)


@router.post("/flags/{flag_id}/{action}")
def flag_action(request: Request, flag_id: int, action: str, session=Depends(db)):
    if action not in ("accept", "dismiss"):
        raise HTTPException(404)
    flag = session.get(m.Flag, flag_id)
    if flag is None:
        raise HTTPException(404)
    flag.status = "accepted" if action == "accept" else "dismissed"
    return RedirectResponse("/flags", status_code=303)


@router.get("/runs")
def runs(request: Request, session=Depends(db)):
    rows = session.scalars(select(m.AgentRun).order_by(m.AgentRun.id.desc()).limit(50)).all()
    return render(request, "runs.html", runs=rows)


@router.get("/runs/{run_id}")
def run_detail(request: Request, run_id: int, session=Depends(db)):
    run = session.get(m.AgentRun, run_id)
    if run is None:
        raise HTTPException(404)
    steps = session.scalars(
        select(m.AgentStep).where(m.AgentStep.run_id == run_id).order_by(m.AgentStep.step_no)
    ).all()
    return render(request, "run_detail.html", run=run, steps=steps)


# --- phone simulator ------------------------------------------------------------


@router.get("/simulator")
def simulator(request: Request, session=Depends(db)):
    as_id = request.query_params.get("as")
    routed = request.query_params.get("routed")
    all_vols = session.scalars(select(m.Volunteer).order_by(m.Volunteer.name)).all()

    people = []
    selected = None
    for vol in all_vols:
        last_in = session.scalar(
            select(m.Message.id).where(m.Message.phone == vol.phone, m.Message.direction == "in")
            .order_by(m.Message.id.desc())
        )
        unread = session.scalars(
            select(m.Message.id).where(
                m.Message.phone == vol.phone, m.Message.direction == "out",
                m.Message.id > (last_in or 0),
            )
        ).all()
        people.append(
            {"id": vol.id, "name": vol.name, "is_coordinator": vol.is_coordinator,
             "is_pastor": vol.is_pastor, "sms_opt_in": vol.sms_opt_in, "unread": len(unread)}
        )
        if as_id and str(vol.id) == as_id:
            selected = vol

    thread = []
    if selected is not None:
        thread = session.scalars(
            select(m.Message).where(m.Message.phone == selected.phone).order_by(m.Message.id)
        ).all()

    # Jinja resolves v.name on dicts via item lookup, so plain dicts are fine.
    return render(request, "simulator.html", volunteers=people, selected=selected,
                  thread=thread, last_result=routed)


@router.post("/simulator/{volunteer_id}/send")
def simulator_send(request: Request, volunteer_id: int, body: str = Form(...), session=Depends(db)):
    volunteer = session.get(m.Volunteer, volunteer_id)
    if volunteer is None:
        raise HTTPException(404)
    state = request.app.state
    from app.sms.mock_provider import MockSMSProvider

    provider = MockSMSProvider()
    parser = partial(parse_inbound, state.gloo)
    result = handle_inbound(
        session, state.clock, provider, volunteer.phone, body, parser,
        ctx=FillContext(session, state.clock, provider, state.gloo),
    )
    return RedirectResponse(f"/simulator?as={volunteer_id}&routed={result.routed_to}", status_code=303)


# --- demo controls (DEMO_MODE only) ------------------------------------------------


def _require_demo(request: Request):
    if not request.app.state.settings.demo_mode:
        raise HTTPException(404)


@router.post("/demo/advance")
def demo_advance(request: Request, minutes: int = Form(...), session=Depends(db)):
    _require_demo(request)
    request.app.state.clock.advance(timedelta(minutes=max(0, minutes)))
    process_jobs(fill_ctx(request, session))
    return RedirectResponse(request.headers.get("referer", "/"), status_code=303)


@router.post("/demo/reset")
def demo_reset(request: Request):
    _require_demo(request)
    state = request.app.state
    if state.settings.mac_bridge_enabled:
        raise HTTPException(409, "Stop the Mac connector before resetting its receipts and queue")
    reset_db(state.engine)
    with state.session_factory() as session:
        seed(session)
    state.clock.set_time(SEED_ANCHOR)
    state.provider.sent.clear()
    return RedirectResponse("/", status_code=303)
