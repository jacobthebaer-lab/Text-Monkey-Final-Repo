"""One-worker scheduled jobs, also callable with the demo/test clock."""
from datetime import timedelta
from sqlalchemy import select
from app.agents import fill_agent
from app.db import models as m

def process_due_fill_requests(ctx):return fill_agent.advance_due(ctx)

def process_jobs(ctx, calendar=False):
    from app.core.reminders import process
    from app.agents.planning_agent import request_collection, collect, plan_month
    from app.agents.capacity_agent import scan
    now=ctx.clock.now();result={"fills":process_due_fill_requests(ctx),"messages":process(ctx)}
    week=f"job:capacity:{now:%G-%V}"
    if not ctx.session.get(m.Policy,week):
        result["flags"]=len(scan(ctx));ctx.session.add(m.Policy(key=week,value={"done":True}))
    next_month=(now.replace(day=1)+timedelta(days=32)).strftime("%Y-%m")
    if now.day>=15:request_collection(ctx,next_month)
    for a in list(ctx.session.scalars(select(m.Approval).where(m.Approval.kind=="collect_availability",m.Approval.status=="approved"))):
        collect(ctx,a)
        if a.decided_at and now>=a.decided_at+timedelta(days=3):
            collect(ctx,a,reminder=True)
            key=f"job:plan:{a.id}"
            if not ctx.session.get(m.Policy,key):
                result["plan"]=plan_month(ctx,a.payload["month"]);ctx.session.add(m.Policy(key=key,value={"done":True}))
    if calendar:
        from app.integrations.gcal import sync
        result["calendar"]=sync(ctx)
    ctx.session.flush();return result
