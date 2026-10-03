"""Availability collection, constrained draft, bounded model review, human publication."""
import json
from datetime import date, timedelta
from pathlib import Path
from sqlalchemy import select
from app.core import scheduler
from app.db import models as m
from app.llm.agent_loop import RunLogger, run_agent
from app.llm.tools import ToolDef
from app.config import get_settings

PROMPT = Path(__file__).resolve().parents[2] / "prompts/planning_agent.md"

def request_collection(ctx, month):
    scheduler.bounds(month)
    for a in ctx.session.scalars(select(m.Approval).where(m.Approval.kind == "collect_availability")):
        if a.payload.get("month") == month and a.status in ("pending", "approved"): return a
    row=m.Approval(kind="collect_availability", payload={"month":month}, status="pending", requested_at=ctx.clock.now())
    ctx.session.add(row);ctx.session.flush();return row

def collect(ctx, approval, reminder=False):
    if approval.status != "approved": raise ValueError("collection requires approval")
    from app.sms.mock_provider import MockSMSProvider
    if not isinstance(ctx.provider, MockSMSProvider):
        return {"sent": [], "held": "Connected collection needs Gloo composition and exact review"}
    month = approval.payload["month"]
    sent=[]
    for v in ctx.session.scalars(select(m.Volunteer).where(m.Volunteer.status == "active", m.Volunteer.sms_opt_in.is_(True), m.Volunteer.is_coordinator.is_(False), m.Volunteer.is_pastor.is_(False))):
        if ctx.session.scalar(select(m.Availability).where(m.Availability.volunteer_id == v.id,m.Availability.month == month)): continue
        key=f"job:availability:{approval.id}:{v.id}:{int(reminder)}"
        if ctx.session.get(m.Policy,key):continue
        outcome=ctx.gate.send(volunteer=v,purpose="availability_ask",body=f"Hi {v.name.split()[0]}! Which {month} dates can you serve? Reply with dates, 'same as usual', or 'not this month'. Thank you!")
        if outcome.sent:
            ctx.session.add(m.Policy(key=key,value={"message_id":outcome.message_id}));sent.append(v.id)
    ctx.session.flush();return {"sent":sent}

def record_availability(ctx, volunteer, parsed, body, month=None):
    # Resolve the latest approved collection, otherwise use the current month.
    month=month or next((a.payload["month"] for a in ctx.session.scalars(select(m.Approval).where(m.Approval.kind=="collect_availability", m.Approval.status=="approved").order_by(m.Approval.id.desc()))), ctx.clock.now().strftime("%Y-%m"))
    start,end=scheduler.bounds(month)
    all_dates=[(start.date()+timedelta(days=i)).isoformat() for i in range((end-start).days)]
    text=body.lower().strip();available=[];unavailable=[]
    if "not this month" in text: unavailable=all_dates
    elif "same as usual" in text:
        # Infer weekday + ordinal rhythm from completed assignments in the prior three months.
        rows=ctx.session.scalars(select(m.Assignment).join(m.Shift).join(m.Event).where(m.Assignment.volunteer_id==volunteer.id,m.Assignment.status=="completed",m.Event.starts_at>=start-timedelta(days=93),m.Event.starts_at<start))
        patterns={}
        for a in rows:
            d=a.shift.event.starts_at.astimezone(start.tzinfo).date();key=(d.weekday(),(d.day-1)//7+1)
            patterns.setdefault(key,set()).add(d.strftime("%Y-%m"))
        available=[d for d in all_dates if len(patterns.get((date.fromisoformat(d).weekday(),(date.fromisoformat(d).day-1)//7+1),set()))>=2]
        if not available: return {"error":"no consistent serving rhythm; coordinator must clarify"}
    else:
        for raw in parsed.dates:
            try:
                d=date.fromisoformat(raw)
                if d.strftime("%Y-%m")==month:available.append(d.isoformat())
            except (ValueError,TypeError):pass
        # Ordinal Sundays are unambiguous within an approved collection month.
        import re
        ordinals=[int(x) for x in re.findall(r"\b([1-5])(?:st|nd|rd|th)\b",text)]
        words={"first":1,"second":2,"third":3,"fourth":4,"fifth":5}
        ordinals += [n for w,n in words.items() if re.search(r"\b"+w+r"\b",text)]
        available += [d for d in all_dates if date.fromisoformat(d).weekday()==6 and (date.fromisoformat(d).day-1)//7+1 in ordinals]
        if not available: return {"error":"dates need coordinator clarification"}
        if any(w in text for w in ("except", "away", "out of town", "can't", "cant", "unavailable")):
            unavailable,available=available,[]
    row=ctx.session.scalar(select(m.Availability).where(m.Availability.volunteer_id==volunteer.id,m.Availability.month==month).order_by(m.Availability.id.desc()))
    if row is None:
        row=m.Availability(volunteer_id=volunteer.id,month=month);ctx.session.add(row)
    row.available_dates=sorted(set(available));row.unavailable_dates=sorted(set(unavailable));row.raw_reply=body;row.parsed_at=ctx.clock.now();ctx.session.flush()
    return {"month":month,"available":row.available_dates,"unavailable":row.unavailable_dates}

def plan_month(ctx, month, use_ai=True):
    tz=get_settings().church_timezone
    report=scheduler.draft(ctx.session,ctx.clock,month,tz)
    logger=RunLogger(ctx.session,ctx.clock,agent="planning_agent",trigger=f"plan {month}",log_dir=ctx.log_dir)
    logger.step("decision",result=report)
    repairs=[0]
    def inspect(args): return scheduler.validate(ctx.session,month,tz)
    def repair(args):
        if repairs[0]>=3:return {"error":"maximum three repair rounds"}
        repairs[0]+=1
        return scheduler.draft(ctx.session,ctx.clock,month,tz)
    def swap(args):
        row=ctx.session.get(m.Assignment,args["assignment_id"]);vol=ctx.session.get(m.Volunteer,args["volunteer_id"])
        if not row or row.status!="proposed" or row.shift not in scheduler.shifts_for(ctx.session,month,tz) or vol is None:return {"error":"only this month's proposed assignments may change"}
        with ctx.session.begin_nested():
            row.status="cancelled";ctx.session.flush()
            result=scheduler.propose(ctx.session,ctx.clock,row.shift,vol,tz)
            if "error" in result:row.status="proposed";ctx.session.flush()
        return result
    defs={"inspect_schedule":ToolDef("inspect_schedule","Read gaps, loads and violations",{"type":"object","properties":{}},inspect),
          "repair_schedule":ToolDef("repair_schedule","Fill remaining gaps, at most three rounds",{"type":"object","properties":{}},repair),
          "propose_swap":ToolDef("propose_swap","Replace a proposed assignment, enforcing every hard rule",{"type":"object","properties":{"assignment_id":{"type":"integer"},"volunteer_id":{"type":"integer"}},"required":["assignment_id","volunteer_id"]},swap)}
    if use_ai:
        result=run_agent(ctx.gloo,logger,model=get_settings().agent_model,instructions=PROMPT.read_text(),user_input=json.dumps(report),tools=defs,max_steps=get_settings().max_agent_steps)
        logger.close(result["outcome"])
        if result["outcome"]!="completed":
            ctx.session.add(m.Escalation(category="system_error",severity="normal",summary=f"Schedule {month} needs human review: {result['outcome']}",related_ids={"month":month},status="open",created_at=ctx.clock.now()))
    else:logger.close("deterministic draft; AI review not run")
    report=scheduler.validate(ctx.session,month,tz)
    pending=next((a for a in ctx.session.scalars(select(m.Approval).where(m.Approval.kind=="publish_schedule",m.Approval.status=="pending")) if a.payload.get("month")==month),None)
    if pending:pending.payload=report
    else:
        pending=m.Approval(kind="publish_schedule",payload=report,status="pending",requested_at=ctx.clock.now());ctx.session.add(pending)
    ctx.session.flush();return {**report,"approval_id":pending.id}

def publish(ctx,approval):
    if approval.status!="approved":raise ValueError("publication requires coordinator approval")
    report=scheduler.validate(ctx.session,approval.payload["month"],get_settings().church_timezone)
    if report["violations"]: raise ValueError("schedule has hard-rule violations; publication blocked")
    count=0
    for shift in scheduler.shifts_for(ctx.session,approval.payload["month"],get_settings().church_timezone):
        row=scheduler.occupied(ctx.session,shift)
        if row and row.status=="proposed":row.status="approved";row.updated_at=ctx.clock.now();count+=1
    ctx.session.flush();return {"published":count,"gaps":report["gaps"]}
