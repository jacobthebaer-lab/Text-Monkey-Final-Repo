"""Evidence-based capacity flags; never contacts volunteers or verifies qualifications."""
from collections import Counter, defaultdict
from datetime import timedelta
from sqlalchemy import select
from app.db import models as m
from app.core import eligibility
from app.core.recurring_availability import global_frequency_limit, normalize_role_frequency_caps
from app.core.send_gate import has_open_sensitive_escalation
from app.llm.agent_loop import RunLogger

def scan(ctx):
    s,now=ctx.session,ctx.clock.now();flags=[]
    volunteers=list(s.scalars(select(m.Volunteer).where(m.Volunteer.is_coordinator.is_(False),m.Volunteer.is_pastor.is_(False))))
    roles=list(s.scalars(select(m.Role)))
    limits={}
    for v in volunteers:
        try:
            limits[v.id]=(global_frequency_limit(v.preferences),
                normalize_role_frequency_caps(v.preferences.get("role_frequency_caps", []), roles))
        except (ValueError, TypeError):
            limits[v.id]=(None, [])  # Invalid facts cannot establish a capacity ceiling.
    assignments=list(s.scalars(select(m.Assignment).where(m.Assignment.status.in_(("approved","confirmed","completed")))))
    past=[a for a in assignments if now-timedelta(weeks=6)<=a.shift.event.starts_at<now]
    recent=Counter(a.volunteer_id for a in past)
    def flag(kind,type,subject,summary,evidence,action):
        key=f"{type}:{subject}";evidence={**evidence,"key":key}
        old=next((f for f in s.scalars(select(m.Flag).where(m.Flag.type==type)) if f.evidence.get("key")==key and f.status!="dismissed"),None)
        if old:old.evidence=evidence;old.summary=summary
        else:
            old=m.Flag(kind=kind,type=type,summary=summary,evidence=evidence,suggested_action=action,status="open",created_at=now);s.add(old)
        flags.append(old)
    for v in volunteers:
        monthly=[a for a in past if a.volunteer_id==v.id and a.shift.event.starts_at>=now-timedelta(days=30)]
        maximum=limits[v.id][0]
        if maximum is not None and len(monthly)>maximum:
            flag("concern","burnout",v.id,f"{v.name} served {len(monthly)} times in 30 days against a preference of {maximum}.",{"volunteer_id":v.id,"count":len(monthly),"maximum":maximum},"Coordinator reviews workload with the volunteer.")
        history=[a for a in assignments if a.volunteer_id==v.id and now-timedelta(weeks=20)<=a.shift.event.starts_at<now-timedelta(weeks=6)]
        history_months=Counter(a.shift.event.starts_at.strftime("%Y-%m") for a in history)
        if sum(n>=2 for n in history_months.values())>=3 and not recent[v.id]:
            flag("concern","drop_off",v.id,f"{v.name} previously served regularly and has no service in six weeks.",{"volunteer_id":v.id,"prior_month_counts":dict(history_months),"last_six_weeks":0},"A human may check in personally; no automated message.")
        if v.status=="active" and v.sms_opt_in and v.created_at<=now-timedelta(days=30) and not any(a.volunteer_id==v.id for a in assignments):
            flag("opportunity","untapped",v.id,f"{v.name} opted in over 30 days ago and has never served.",{"volunteer_id":v.id,"created_at":v.created_at.isoformat()},"Coordinator reviews interests and proposes an invitation.")
        for q in v.qualifications:
            if q.status=="verified" and q.expires_on and now.date()<=q.expires_on<=now.date()+timedelta(days=30):
                flag("concern","expiring",q.id,f"{v.name}'s {q.type} expires on {q.expires_on}.",{"qualification_id":q.id,"volunteer_id":v.id,"expires_on":q.expires_on.isoformat()},"Coordinator requests renewal; verify evidence before updating.")
    supplies={}
    for role in roles:
        qualified=[v for v in volunteers if v.status=="active" and v.sms_opt_in and not has_open_sensitive_escalation(s,v.id) and all(any(q.type==t and q.status=="verified" and (not q.expires_on or q.expires_on>=now.date()) for q in v.qualifications) for t in role.required_qualifications)]
        interested=[v for v in qualified if role.name in v.preferences.get("interested_roles",[])]
        supplies[role.id]=len(interested)
        role_past=[a for a in past if a.shift.role_id==role.id];counts=Counter(a.volunteer_id for a in role_past)
        dominant=counts.most_common(1)
        if len(interested)<=2 or dominant and dominant[0][1]>len(role_past)/2:
            flag("concern","single_point_of_failure",role.id,f"{role.name} relies on a small pool or one dominant volunteer.",{"role_id":role.id,"interested_qualified":len(interested),"assignments":dict(counts)},"Coordinator reviews cross-training and backup coverage.")
        for v in qualified:
            if role.required_qualifications and not any(a.volunteer_id==v.id and a.shift.role_id==role.id for a in assignments):
                flag("opportunity","unused_skill",f"{v.id}:{role.id}",f"{v.name} holds verified skills for {role.name} but has not served there.",{"volunteer_id":v.id,"role_id":role.id},"Coordinator checks interest before proposing a new role.")
        history_shifts=list(s.scalars(select(m.Shift).join(m.Event).where(m.Shift.role_id==role.id,m.Event.starts_at>=now-timedelta(weeks=6),m.Event.starts_at<now)))
        gaps=[sh.id for sh in history_shifts if not any(a.shift_id==sh.id for a in past)]
        if len(gaps)>=3:flag("concern","chronic_gap",role.id,f"{role.name} had {len(gaps)} uncovered slots in six weeks.",{"role_id":role.id,"shift_ids":gaps},"Review the recipe and recruit or train with approval.")
        future=list(s.scalars(select(m.Shift).join(m.Event).where(m.Shift.role_id==role.id,m.Event.status=="scheduled",m.Event.starts_at>=now,m.Event.starts_at<now+timedelta(weeks=8))))
        role_limits=[]
        for v in interested:
            global_limit,caps=limits[v.id]
            scoped=next((cap["max_per_month"] for cap in caps if cap["role_id"]==role.id), None)
            known=[limit for limit in (global_limit, scoped) if limit is not None]
            role_limits.append(min(known) if known else None)
        # An unstated ceiling is unknown capacity, never zero or another role's cap.
        if all(limit is not None for limit in role_limits):
            capacity=sum(limit*2 for limit in role_limits)
            if len(future)>capacity:flag("opportunity","growing_need",role.id,f"Eight-week {role.name} demand exceeds stated capacity.",{"role_id":role.id,"slots":len(future),"capacity":capacity},"Coordinator approves recruitment or training invitations.")
    if supplies and min(supplies.values())<=2 and max(supplies.values())>=8:
        flag("opportunity","rebalance","ministries","Some roles have large pools while others have two or fewer.",{"interested_qualified_by_role":supplies},"Review willing volunteers for training; never transfer without consent.")
    logger=RunLogger(s,ctx.clock,agent="capacity_agent",trigger="weekly capacity scan",log_dir=ctx.log_dir)
    logger.step("decision",result={"flags":[{"type":f.type,"evidence":f.evidence} for f in flags]});logger.close(f"{len(flags)} evidence-based flags")
    s.flush();return flags
