from datetime import timedelta
from types import SimpleNamespace
from sqlalchemy import select
from app.agents.fill_agent import FillContext
from app.agents.planning_agent import request_collection, collect, record_availability, plan_month, publish
from app.agents.capacity_agent import scan
from app.agents.admin_agent import apply
from app.core import scheduler, reminders, eligibility
from app.db import models as m
from app.llm.gloo_client import NullGloo
from app.llm.parser import ParsedMessage
from app.integrations.gcal import sync
from app.config import Settings
import pytest


def ctx(session, clock, provider, tmp_path):return FillContext(session,clock,provider,NullGloo(),log_dir=tmp_path)


def test_draft_enforces_qualifications_and_monthly_max(session,clock,provider,make_volunteer,make_shift,tmp_path):
    v=make_volunteer(quals=[('child_safety_training','verified',None)],prefs={'max_per_month':1})
    make_volunteer(quals=[('child_safety_training','pending',None)])
    s1=make_shift('nursery',required=['child_safety_training'],criticality='critical',fill_policy='needs_approval')
    s2=make_shift('nursery',starts=s1.event.starts_at+timedelta(days=7))
    report=scheduler.draft(session,clock,'2026-10')
    assert report['filled']==1 and len(report['gaps'])==1 and not report['violations']
    assert session.scalar(select(m.Assignment)).volunteer_id==v.id


def test_publication_is_approved_and_revalidated(session,clock,provider,make_volunteer,make_shift,tmp_path):
    v=make_volunteer();make_shift();c=ctx(session,clock,provider,tmp_path)
    report=plan_month(c,'2026-10',use_ai=False);a=session.get(m.Approval,report['approval_id'])
    assert session.scalar(select(m.Assignment)).status=='proposed'
    with pytest.raises(ValueError):publish(c,a)
    a.status='approved';v.sms_opt_in=False
    with pytest.raises(ValueError):publish(c,a)
    v.sms_opt_in=True;assert publish(c,a)['published']==1
    assert publish(c,a)['published']==0


def test_collection_approval_quiet_retry_and_idempotency(session,clock,provider,make_volunteer,tmp_path):
    v=make_volunteer();c=ctx(session,clock,provider,tmp_path);a=request_collection(c,'2026-11')
    assert request_collection(c,'2026-11').id==a.id
    with pytest.raises(ValueError):collect(c,a)
    a.status='approved';clock.set_time(clock.now().replace(hour=23))
    assert collect(c,a)['sent']==[]
    clock.advance(timedelta(hours=9));assert collect(c,a)['sent']==[v.id]
    assert collect(c,a)['sent']==[]


def test_availability_ordinals_exclusions_and_empty_month(session,clock,provider,make_volunteer,tmp_path):
    v=make_volunteer();c=ctx(session,clock,provider,tmp_path)
    result=record_availability(c,v,ParsedMessage(intent='availability'),'2nd and 4th','2026-11')
    assert result['available']==['2026-11-08','2026-11-22']
    result=record_availability(c,v,ParsedMessage(intent='availability',dates=['2026-11-15']),"away Nov 15",'2026-11')
    assert result['unavailable']==['2026-11-15'] and not result['available']
    result=record_availability(c,v,ParsedMessage(intent='availability'),'not this month','2026-11')
    assert len(result['unavailable'])==30


def test_reminders_and_confirmations_deduplicate(session,clock,provider,make_volunteer,make_shift,assign,tmp_path):
    v=make_volunteer();shift=make_shift(starts=clock.now()+timedelta(days=1));assign(v,shift)
    c=ctx(session,clock,provider,tmp_path)
    assert reminders.process(c)=={'reminders':1,'confirmations':1,'summaries':0}
    assert reminders.process(c)=={'reminders':0,'confirmations':0,'summaries':0}
    assert len(provider.sent)==2


def test_capacity_seed_patterns_and_dedup(session,clock,provider,tmp_path):
    from app.db.seed import seed
    seed(session);c=ctx(session,clock,provider,tmp_path);flags=scan(c)
    types={f.type for f in flags}
    assert {'single_point_of_failure','burnout','drop_off','expiring','chronic_gap','untapped','unused_skill','growing_need','rebalance'}<=types
    count=len(list(session.scalars(select(m.Flag))));scan(c)
    assert len(list(session.scalars(select(m.Flag))))==count
    assert provider.sent==[]


def test_admin_approval_controls_exact_change(session,clock,provider,make_volunteer,make_shift,tmp_path):
    v=make_volunteer();shift=make_shift();c=ctx(session,clock,provider,tmp_path)
    approval=m.Approval(kind='admin_change',payload={'action':'pause_role','volunteer_id':v.id,'role_id':shift.role_id},status='pending',requested_at=clock.now());session.add(approval);session.flush()
    with pytest.raises(ValueError):apply(c,approval)
    assert eligibility.check(session,v,shift)
    approval.status='approved';apply(c,approval)
    assert not eligibility.check(session,v,shift)
    assert provider.sent==[]


def calendar_service(items):
    return SimpleNamespace(events=lambda:SimpleNamespace(list=lambda **kw:SimpleNamespace(execute=lambda:{'items':items})))


def test_calendar_readonly_idempotent_and_unknown_handoff(session,clock,provider,tmp_path):
    c=ctx(session,clock,provider,tmp_path)
    items=[{'id':'external','summary':'New Community Gathering','start':{'dateTime':'2026-10-10T10:00:00-06:00'},'end':{'dateTime':'2026-10-10T11:00:00-06:00'}}]
    settings=Settings(google_calendar_id='synthetic-calendar')
    assert sync(c,calendar_service(items),settings)['created']==1
    assert sync(c,calendar_service(items),settings)['updated']==1
    assert len(list(session.scalars(select(m.Event))))==1
    assert len(list(session.scalars(select(m.Escalation))))==1
    assert provider.sent==[]


def test_planning_model_outage_preserves_reviewable_draft(session,clock,provider,make_volunteer,make_shift,tmp_path):
    make_volunteer();make_shift();c=ctx(session,clock,provider,tmp_path)
    r=plan_month(c,'2026-10')
    assert r['filled']==1 and session.get(m.Approval,r['approval_id']).status=='pending'
    assert session.scalar(select(m.Escalation)).category=='system_error'
    assert provider.sent==[]


def test_sensitive_cancellation_backstop_preserves_care_and_logistics():
    from app.llm.parser import _apply_backstop
    parsed=_apply_backstop(ParsedMessage(parse_error=True), "I want to hurt myself. I cant come tomorrow.")
    assert parsed.intent=='cancel' and parsed.sensitive and parsed.severity=='urgent' and not parsed.parse_error
    ambiguous=_apply_backstop(ParsedMessage(parse_error=True), "I am in hospital. Maybe I cant come?")
    assert ambiguous.parse_error and ambiguous.intent=='unclear' and ambiguous.sensitive


def test_legacy_job_copy_cannot_reach_connected_transport(session, clock, make_volunteer, tmp_path, monkeypatch):
    from app import jobs
    class ConnectedProvider:
        def send(self, *args, **kwargs):
            pytest.fail("Legacy template reached connected delivery")
    c = ctx(session, clock, ConnectedProvider(), tmp_path)
    volunteer = make_volunteer()
    assert reminders.once(c, "connected", volunteer, "Uncomposed template", "reminder") is False
    approval = request_collection(c, "2026-11")
    approval.status = "approved"
    assert collect(c, approval)["sent"] == []
    monkeypatch.setattr(jobs, "process_due_fill_requests", lambda current: ["existing reviewed jobs"])
    result = jobs.process_jobs(c)
    assert result == {"fills": ["existing reviewed jobs"], "legacy_workflows": "held_for_connected_review"}
    assert not session.scalar(select(m.Message.id))
