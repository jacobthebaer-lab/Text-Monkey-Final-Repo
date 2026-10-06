"""Real admin tool loop and exact reviews, synthetic evidence only."""
import json
from copy import deepcopy
from datetime import datetime
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select

from app.agents.admin_agent import prepare
from app.agents.fill_agent import FillContext
from app.core import confirmations as c
from app.db import models as m
from app.llm.gloo_client import GlooUnavailableError
from app.web.texty import admin
from tests.test_confirmations import mode_app
from tests.test_planning_patterns import event, ZONE


class PlanningGloo:
    def __init__(self, steps, *, fail=False):
        self.steps=list(steps);self.inputs=[];self.fail=fail
    def create_response(self, **kwargs):
        self.inputs.append(kwargs)
        if self.steps:
            step=self.steps.pop(0)
            name,args=step(kwargs) if callable(step) else step
            return SimpleNamespace(output=[SimpleNamespace(type='function_call',name=name,
                arguments=json.dumps(args),call_id='synthetic-'+str(len(self.inputs)))],output_text='',usage=None)
        if self.fail:raise GlooUnavailableError('Synthetic planning outage')
        return SimpleNamespace(output=[],output_text='Evidence ready for review.',usage=None)


def stage_args(person, mutate=None):
    def step(kwargs):
        result=json.loads(next(i['output'] for i in reversed(kwargs['input'])
            if i.get('type')=='function_call_output'))
        if mutate:mutate()
        return 'stage_pattern_review',{'volunteer_id':person.id,'evidence_hash':result['evidence_hash']}
    return step


def tools(ctx):
    return {row.tool_name:row.result for row in ctx.session.scalars(select(m.AgentStep)
        .where(m.AgentStep.type=='tool_result').order_by(m.AgentStep.id))}


@pytest.fixture
def historical(session,clock,make_volunteer):
    coordinator=make_volunteer(coordinator=True)
    person=make_volunteer(opt_in=False,status='inactive',prefs={'private_note':'Synthetic private detail'})
    observations=[]
    for when in (datetime(2026,7,12,9,tzinfo=ZONE),datetime(2026,8,9,9,tzinfo=ZONE)):
        row=event(session,when)
        item=m.Assignment(shift_id=row.shifts[0].id,volunteer_id=person.id,status='completed',source='admin',
            created_at=when,updated_at=when)
        session.add(item);session.flush();observations.append(item)
    return coordinator,person,observations


def context(session,clock,provider,tmp_path,person,*,mutate=None,fail=False):
    return FillContext(session,clock,provider,PlanningGloo([('read_context',{}),
        ('learned_patterns',{'volunteer_id':person.id}),stage_args(person,mutate)],fail=fail),log_dir=tmp_path)


def test_pattern_tool_stages_exact_evidence_no_contacts_and_deduplicates(session,clock,provider,tmp_path,historical):
    coordinator,person,observations=historical
    before=c.values(person);count=len(session.scalars(select(m.Assignment)).all())
    ctx=context(session,clock,provider,tmp_path,person)
    result=prepare(ctx,coordinator,'Review the serving rhythm')
    review=session.get(m.Approval,result['approval_ids'][0])
    assert review.kind=='confirm_record' and review.status=='pending' and c.valid(review,clock.now())
    assert review.payload['after']['preferences']['calendar_patterns']=={
        'weekday_ordinals':[{'weekday':6,'ordinals':[2]}],'annual_unavailable_months':[]}
    assert c.values(person)==before and not result['applied']
    exported=json.dumps(ctx.gloo.inputs)
    assert person.phone not in exported and 'Synthetic private detail' not in exported
    again=prepare(context(session,clock,provider,tmp_path,person),coordinator,'Review the same rhythm')
    assert again['approval_ids']==result['approval_ids']
    c.decide(session,ctx.gate,review,approve=True,actor='coordinator@example.test',
        expected=review.payload['content_hash'],now=clock.now())
    assert person.preferences['calendar_patterns']['weekday_ordinals'][0]['ordinals']==[2]
    assert not person.sms_opt_in and person.status=='inactive' and person.preferences['private_note']==before['preferences']['private_note']
    assert len(session.scalars(select(m.Assignment)).all())==count
    assert not session.scalar(select(m.Qualification)) and not session.scalar(select(m.Message)) and not provider.sent


@pytest.mark.parametrize('change',['profile','history','coordinator','timezone'])
def test_changed_read_evidence_cannot_stage(session,clock,provider,tmp_path,historical,change):
    coordinator,person,observations=historical
    def mutate():
        if change=='profile':person.preferences={**person.preferences,'new_fact':'Actual changed preference'}
        elif change=='history':observations[0].status='cancelled'
        elif change=='coordinator':coordinator.is_coordinator=False
        else:session.add(m.Policy(key='church_timezone',value={'value':'UTC'}))
        session.flush()
    ctx=context(session,clock,provider,tmp_path,person,mutate=mutate)
    result=prepare(ctx,coordinator,'Review unchanged evidence')
    assert result['approval_ids']==[] and 'error' in tools(ctx)['stage_pattern_review']
    assert not session.scalar(select(m.Approval)) and not provider.sent


@pytest.mark.parametrize('change',['assignment','event','coordinator','timezone','profile'])
def test_staged_original_sources_rechecked_at_exact_approval(session,clock,provider,tmp_path,historical,change):
    coordinator,person,observations=historical
    ctx=context(session,clock,provider,tmp_path,person)
    result=prepare(ctx,coordinator,'Prepare review')
    review=session.get(m.Approval,result['approval_ids'][0])
    if change=='assignment':observations[0].status='cancelled'
    elif change=='event':observations[0].shift.event.status='cancelled'
    elif change=='coordinator':coordinator.status='inactive'
    elif change=='timezone':session.add(m.Policy(key='church_timezone',value={'value':'UTC'}))
    else:person.preferences={'private_note':'Changed source'}
    session.flush()
    with pytest.raises(ValueError):
        c.decide(session,ctx.gate,review,approve=True,actor='coordinator@example.test',
            expected=review.payload['content_hash'],now=clock.now())
    assert 'calendar_patterns' not in person.preferences and not provider.sent


@pytest.mark.parametrize('name,args,read_first',[
    ('learned_patterns',{'volunteer_id':True},True),
    ('learned_patterns',{'volunteer_id':99999},True),
    ('learned_patterns',{'volunteer_id':1},False),
    ('stage_pattern_review',{'volunteer_id':1,'evidence_hash':'invented'},True),
    ('stage_pattern_review',{'volunteer_id':1,'evidence_hash':'invented','patterns':{}},True),
    ('seasonal_staffing_report',{'month':'2026-99'},True),
    ('seasonal_staffing_report',{'month':'2028-12'},True),
    ('seasonal_staffing_report',{'month':'2026-12','add_slots':True},True),
])
def test_untrusted_tool_arguments_do_not_stage(session,clock,provider,tmp_path,make_volunteer,name,args,read_first):
    coordinator=make_volunteer(coordinator=True)
    steps=([('read_context',{})] if read_first else [])+[(name,args)]
    ctx=FillContext(session,clock,provider,PlanningGloo(steps),log_dir=tmp_path)
    assert prepare(ctx,coordinator,'Synthetic invalid tool request')['approval_ids']==[]
    assert 'error' in tools(ctx)[name] and not provider.sent


def test_no_history_or_one_time_absence_never_becomes_annual_pattern(session,clock,provider,tmp_path,make_volunteer):
    coordinator=make_volunteer(coordinator=True)
    person=make_volunteer(prefs={'onboarding_availability_draft':{'unavailable_dates':['2026-12-06']}})
    ctx=context(session,clock,provider,tmp_path,person)
    assert prepare(ctx,coordinator,'Learn a rhythm')['approval_ids']==[]
    report=tools(ctx)['learned_patterns']
    assert report['proposal'] is None and not report['unavailable_months_inferred']
    assert not session.scalar(select(m.Approval)) and 'calendar_patterns' not in person.preferences


def test_outage_expires_new_pattern_review_without_apply(session,clock,provider,tmp_path,historical):
    coordinator,person,_=historical
    ctx=context(session,clock,provider,tmp_path,person,fail=True)
    result=prepare(ctx,coordinator,'Prepare exact review')
    assert result['outcome']=='gloo_unavailable'
    assert session.get(m.Approval,result['approval_ids'][0]).status=='expired'
    assert 'calendar_patterns' not in person.preferences and not provider.sent


def test_seasonal_tool_returns_verified_slots_only(session,clock,provider,tmp_path,make_volunteer):
    coordinator=make_volunteer(coordinator=True)
    kind=m.EventType(name='Synthetic holiday',title_patterns=[]);session.add(kind);session.flush()
    for year,count in ((2024,2),(2025,4)):
        event(session,datetime(year,12,15,9,tzinfo=ZONE),event_type=kind,count=count)
    event(session,datetime(2026,12,15,9,tzinfo=ZONE),event_type=kind,count=1,status='scheduled')
    count=len(session.scalars(select(m.Shift)).all())
    ctx=FillContext(session,clock,provider,PlanningGloo([('read_context',{}),
        ('seasonal_staffing_report',{'month':'2026-12'})]),log_dir=tmp_path)
    assert prepare(ctx,coordinator,'Review December staffing')['approval_ids']==[]
    report=tools(ctx)['seasonal_staffing_report']
    assert report['recommendations'][0]['review_difference']==2 and not report['staffing_changed']
    assert len(session.scalars(select(m.Shift)).all())==count and not provider.sent


def test_existing_ask_text_monkey_route_can_stage_pattern_review(mode_app,session,historical):
    app,_,_=mode_app
    coordinator,person,_=historical;session.commit()
    app.state.gloo=PlanningGloo([('read_context',{}),('learned_patterns',{'volunteer_id':person.id}),stage_args(person)])
    with TestClient(app) as client:
        payload={'coordinator_id':coordinator.id,'command':'Review the completed serving rhythm'}
        assert client.post('/api/coordinator/command',json=payload).status_code==401
        app.dependency_overrides[admin]=lambda:{'email':'coordinator@example.test'}
        response=client.post('/api/coordinator/command',json=payload)
        assert response.status_code==200,response.text
        data=response.json();assert data['state']=='pending_exact_review' and data['sent']==0
        review=data['reviews'][0]
        assert review['after']['preferences']['calendar_patterns']['annual_unavailable_months']==[]
        assert any(p['id']==str(review['id']) for p in client.get('/api/state').json()['proposals'])


def test_committed_history_change_between_model_turns_refreshes_cached_event(session,clock,provider,tmp_path,historical):
    from sqlalchemy.orm import Session
    coordinator,person,observations=historical
    event_id=observations[0].shift.event.id
    session.commit()
    def mutate():
        with Session(session.bind) as other:
            other.get(m.Event,event_id).status='cancelled';other.commit()
    ctx=context(session,clock,provider,tmp_path,person,mutate=mutate)
    result=prepare(ctx,coordinator,'Review current completed evidence')
    assert result['approval_ids']==[] and 'error' in tools(ctx)['stage_pattern_review']
    assert not session.scalar(select(m.Approval)) and not provider.sent
