"""Durable job receipts prevent repeat reminder/confirmation sends on later ticks."""
from datetime import timedelta
from sqlalchemy import select
from app.db import models as m

def once(ctx,key,volunteer,body,purpose):
    from app.sms.mock_provider import MockSMSProvider
    if not isinstance(ctx.provider, MockSMSProvider):
        return False
    key="job:"+key
    if ctx.session.get(m.Policy,key):return False
    outcome=ctx.gate.send(volunteer=volunteer,body=body,purpose=purpose)
    if outcome.sent:
        ctx.session.add(m.Policy(key=key,value={"message_id":outcome.message_id}));ctx.session.flush();return True
    return False

def process(ctx):
    now=ctx.clock.now();counts={"reminders":0,"confirmations":0,"summaries":0}
    for a in ctx.session.scalars(select(m.Assignment).where(m.Assignment.status.in_(("approved","confirmed")))):
        event=a.shift.event
        if event.status!="scheduled" or event.starts_at<=now:continue
        if a.source=="planner":
            counts["confirmations"]+=once(ctx,f"assignment:{a.id}",a.volunteer,f"Hi {a.volunteer.name.split()[0]}! You're scheduled for {a.shift.role.name} at {event.starts_at.astimezone(now.tzinfo):%b %d %I:%M%p}. Thank you! Reply C to confirm or X if something came up.","confirmation")
        if event.starts_at.astimezone(now.tzinfo).date()==now.date()+timedelta(days=1):
            counts["reminders"]+=once(ctx,f"reminder:{a.id}",a.volunteer,f"Hi {a.volunteer.name.split()[0]}! A reminder: {a.shift.role.name} tomorrow at {event.starts_at.astimezone(now.tzinfo):%I:%M%p}. Thank you! Reply C to confirm or X if something came up.","reminder")
    if now.weekday()==5 and now.hour>=18:
        coordinator=ctx.session.scalar(select(m.Volunteer).where(m.Volunteer.is_coordinator.is_(True)))
        tomorrow=now.date()+timedelta(days=1)
        shifts=list(ctx.session.scalars(select(m.Shift).join(m.Event).where(m.Event.starts_at>=now,m.Event.starts_at<now+timedelta(days=2),m.Event.status=="scheduled")))
        shifts=[sh for sh in shifts if sh.event.starts_at.astimezone(now.tzinfo).date()==tomorrow]
        filled=sum(any(a.status in ("approved","confirmed") for a in sh.assignments) for sh in shifts)
        if coordinator:counts["summaries"]+=once(ctx,f"summary:{tomorrow}",coordinator,f"Tomorrow: {filled}/{len(shifts)} volunteer slots filled. Review gaps and personal-care escalations on the coordinator dashboard.","coordinator_notify")
    return counts
