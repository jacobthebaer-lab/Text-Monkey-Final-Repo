"""Coordinator-only tools prepare mutations; an explicit approval applies them."""
import json
from datetime import date
from pathlib import Path
from sqlalchemy import select
from app.config import get_settings
from app.db import models as m
from app.llm.agent_loop import RunLogger, run_agent
from app.llm.tools import ToolDef

PROMPT=Path(__file__).resolve().parents[2]/"prompts/admin_agent.md"

def prepare(ctx, coordinator, command):
    if not coordinator.is_coordinator:raise ValueError("coordinator identity required")
    logger=RunLogger(ctx.session,ctx.clock,agent="admin_agent",trigger=command,log_dir=ctx.log_dir)
    created=[]
    def read(args):
        return {"events":[{"id":e.id,"title":e.title,"starts_at":e.starts_at.isoformat()} for e in ctx.session.scalars(select(m.Event).where(m.Event.starts_at>=ctx.clock.now()).order_by(m.Event.starts_at).limit(60))],
                "roles":[{"id":r.id,"name":r.name} for r in ctx.session.scalars(select(m.Role))],
                "volunteers":[{"id":v.id,"name":v.name} for v in ctx.session.scalars(select(m.Volunteer))]}
    def proposal(args):
        action=args.get("action");s=ctx.session
        if action=="add_slots":
            event=s.get(m.Event,args.get("event_id"));role=s.get(m.Role,args.get("role_id"));count=args.get("count",0)
            if not event or event.starts_at<=ctx.clock.now() or not role or not isinstance(count,int) or not 1<=count<=30:return {"error":"valid future event, role and count 1-30 required"}
        elif action=="pause_role":
            if not s.get(m.Volunteer,args.get("volunteer_id")) or not s.get(m.Role,args.get("role_id")):return {"error":"unknown volunteer or role"}
        elif action=="mark_unavailable":
            if not s.get(m.Volunteer,args.get("volunteer_id")):return {"error":"unknown volunteer"}
            dates=args.get("dates",[])
            if not dates:return {"error":"specific dates required"}
            try:
                if any(date.fromisoformat(d)<ctx.clock.now().date() for d in dates):return {"error":"dates must be current or future"}
            except (ValueError,TypeError):return {"error":"ISO dates required"}
        else:return {"error":"unsupported action; qualifications require human evidence review"}
        payload={k:args[k] for k in ("action","event_id","role_id","count","volunteer_id","dates") if k in args}
        payload["requested_by"]=coordinator.id
        row=m.Approval(kind="admin_change",payload=payload,status="pending",requested_at=ctx.clock.now());s.add(row);s.flush();created.append(row.id)
        return {"approval_id":row.id,"proposed_change":payload,"applied":False}
    tools={"read_context":ToolDef("read_context","Read future events, roles and volunteers",{"type":"object","properties":{}},read),
           "propose_change":ToolDef("propose_change","Prepare a coordinator approval; no changes applied",{"type":"object","properties":{"action":{"type":"string","enum":["add_slots","pause_role","mark_unavailable"]},"event_id":{"type":"integer"},"role_id":{"type":"integer"},"count":{"type":"integer"},"volunteer_id":{"type":"integer"},"dates":{"type":"array","items":{"type":"string"}}},"required":["action"]},proposal)}
    result=run_agent(ctx.gloo,logger,model=get_settings().agent_model,instructions=PROMPT.read_text(),user_input=command,tools=tools,max_steps=get_settings().max_agent_steps)
    logger.close(result["outcome"])
    if result["outcome"]!="completed":
        ctx.session.add(m.Escalation(category="system_error",summary="Coordinator command could not complete; review manually.",severity="normal",related_ids={"approval_ids":created},status="open",created_at=ctx.clock.now()))
    return {**result,"approval_ids":created}

def apply(ctx,approval):
    if approval.status!="approved":raise ValueError("explicit coordinator approval required")
    p=approval.payload;s=ctx.session
    if p["action"]=="add_slots":
        event=s.get(m.Event,p["event_id"]);role=s.get(m.Role,p["role_id"])
        if event is None or event.status!="scheduled" or event.starts_at<=ctx.clock.now() or role is None:raise ValueError("event or role changed; review again")
        slots=list(s.scalars(select(m.Shift).where(m.Shift.event_id==event.id,m.Shift.role_id==role.id)))
        start=max((x.slot_index for x in slots),default=-1)+1
        for n in range(p["count"]):s.add(m.Shift(event_id=event.id,role_id=role.id,slot_index=start+n))
    elif p["action"]=="pause_role":
        vol=s.get(m.Volunteer,p["volunteer_id"]);role=s.get(m.Role,p["role_id"])
        if vol is None or role is None:raise ValueError("volunteer or role changed")
        prefs=dict(vol.preferences);prefs["paused_roles"]=sorted(set(prefs.get("paused_roles",[])+[role.name]));vol.preferences=prefs
        # Surface existing future assignments; no silent removal from the roster.
        s.add(m.Escalation(category="unclear",severity="normal",summary=f"Review {vol.name}'s existing {role.name} assignments after the approved pause.",related_ids={"volunteer_id":vol.id,"role_id":role.id},status="open",created_at=ctx.clock.now()))
    elif p["action"]=="mark_unavailable":
        for d in p["dates"]:
            month=d[:7];row=s.scalar(select(m.Availability).where(m.Availability.volunteer_id==p["volunteer_id"],m.Availability.month==month).order_by(m.Availability.id.desc()))
            if row is None:row=m.Availability(volunteer_id=p["volunteer_id"],month=month,available_dates=[],unavailable_dates=[]);s.add(row)
            row.unavailable_dates=sorted(set((row.unavailable_dates or [])+[d]));row.parsed_at=ctx.clock.now()
    else:raise ValueError("unknown approved action")
    s.flush();return {"applied":p["action"]}
