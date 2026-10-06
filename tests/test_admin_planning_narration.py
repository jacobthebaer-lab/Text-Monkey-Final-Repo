"""Explicit planner labels/claims, not general natural-language truth checking."""
from datetime import datetime
from types import SimpleNamespace
import pytest
from sqlalchemy import select
from fastapi.testclient import TestClient
from app.agents.admin_agent import prepare, _event_labels, _summary_problem
from app.agents.fill_agent import FillContext
from app.db import models as m
from app.web.texty import admin
from tests.test_confirmations import mode_app
from tests.test_admin_planning_tools import historical, PlanningGloo, context, stage_args, tools
from tests.test_planning_patterns import event, ZONE


def facts():
    labels=_event_labels(SimpleNamespace(id=6,starts_at=datetime(2026,12,15,9,tzinfo=ZONE),
        ends_at=datetime(2026,12,15,10,tzinfo=ZONE)),'America/Denver')
    return {'active':True,'ordinals':{(2,6)},'dates':{'2026-07-12','2026-08-09','2026-12-15'},'events':{6:labels}}


def test_event_labels_use_future_event_dst_not_current_offset():
    labels=facts()['events'][6]
    assert labels['weekday_name']=='Tuesday'
    assert labels['local_starts_at']=='2026-12-15T09:00:00-07:00'
    assert labels['start_label']=='2026-12-15 Tuesday 9:00 AM America/Denver (UTC-07:00)'
    assert '10:00 AM America/Denver (UTC-07:00)' in labels['end_label']


@pytest.mark.parametrize('text',[
    'Pattern: 2nd Saturday. Pending only.',
    '2026-07-12 (Saturday)',
    'Avery served on 2026-07-13.',
    'Event 6 starts 10:00 AM.',
    'Event 6 ends 11:00 AM.',
    'Event 6: 10:00 AM,11:00 AM MT.',
    'Event 6 starts 2026-12-15T10:00:00-07:00.',
    'December event at 11:00 AM.',
])
def test_explicit_wrong_or_unsupported_structured_claims_hold(text):
    assert _summary_problem(text,facts())


@pytest.mark.parametrize('text',[
    'Avery: 2nd Sunday, observed 2026-07-12 and 2026-08-09. Review pending.',
    'Event 6: 9:00 AM to 10:00 AM America/Denver.',
    'Event 6 starts 9:00 AM. Event 6 ends 10:00 AM.',
    '2026-12-15T09:00:00-07:00 America/Denver.',
    '2026-12-15T16:00:00Z is the same saved event instant.',
    'Two prior years support reviewing two additional slots, no change applied.',
])
def test_exact_labels_and_evidence_claims_pass(text):
    assert _summary_problem(text,facts()) is None


def test_nonplanner_prose_is_not_subject_to_generic_nlp_validation():
    evidence=facts();evidence['active']=False
    assert _summary_problem('Hypothetical 2nd Saturday at 11:00 AM.',evidence) is None


class NarratingGloo(PlanningGloo):
    def __init__(self,steps,text):super().__init__(steps);self.text=text
    def create_response(self,**kwargs):
        result=super().create_response(**kwargs)
        if not result.output:result.output_text=self.text
        return result


def test_learned_and_staged_outputs_carry_named_calendar_evidence(session,clock,provider,tmp_path,historical):
    coordinator,person,_=historical
    ctx=context(session,clock,provider,tmp_path,person)
    result=prepare(ctx,coordinator,'Review the serving rhythm')
    evidence=tools(ctx)['learned_patterns']
    assert evidence['pattern_labels'][0]['weekday_name']=='Sunday'
    assert evidence['pattern_labels'][0]['ordinal_labels']==['2nd Sunday']
    item=evidence['evidence'][0]
    assert item['weekday_name']=='Sunday' and item['ordinal_label']=='2nd Sunday'
    observation=item['observations'][0]
    assert observation['local_starts_at'].endswith('-06:00')
    assert 'America/Denver (UTC-06:00)' in observation['start_label']
    assert tools(ctx)['stage_pattern_review']['pattern_labels']==evidence['pattern_labels']
    assert result['approval_ids'] and not provider.sent


def test_seasonal_tool_exposes_target_and_original_local_offsets(session,clock,provider,tmp_path,make_volunteer):
    coordinator=make_volunteer(coordinator=True)
    kind=m.EventType(name='Holiday service',title_patterns=[]);session.add(kind);session.flush()
    for year,count in ((2024,2),(2025,4),(2026,1)):
        event(session,datetime(year,12,15,9,tzinfo=ZONE),count=count,event_type=kind,
            status='scheduled' if year==2026 else 'completed')
    ctx=FillContext(session,clock,provider,PlanningGloo([('read_context',{}),
        ('seasonal_staffing_report',{'month':'2026-12'})]),log_dir=tmp_path)
    prepare(ctx,coordinator,'Review December slots')
    item=tools(ctx)['seasonal_staffing_report']['recommendations'][0]
    assert item['local_starts_at']=='2026-12-15T09:00:00-07:00'
    assert all(e['local_starts_at'].endswith('-07:00') and e['timezone']=='America/Denver' for e in item['evidence'])
    assert not session.scalar(select(m.Approval)) and not provider.sent


def test_unsupported_summary_keeps_exact_pending_review_visible_via_existing_api(mode_app,session,historical):
    app,_,_=mode_app
    coordinator,person,_=historical;session.commit()
    app.state.gloo=NarratingGloo([('read_context',{}),('learned_patterns',{'volunteer_id':person.id}),stage_args(person)],
        'Avery Sample: 2nd Saturday. Approval 1 is pending.')
    app.dependency_overrides[admin]=lambda:{'email':'coordinator@example.test'}
    with TestClient(app) as client:
        response=client.post('/api/coordinator/command',json={'coordinator_id':coordinator.id,'command':'Review the serving rhythm'})
        assert response.status_code==200,response.text
        data=response.json()
        assert data['outcome']=='completed' and data['narration_state']=='held' and data['final_text'] is None
        assert data['state']=='pending_exact_review' and data['reviews'][0]['status']=='pending'
        assert data['reviews'][0]['after']['preferences']['calendar_patterns']['weekday_ordinals'][0]['weekday']==6
        assert any(p['id']==str(data['reviews'][0]['id']) for p in client.get('/api/state').json()['proposals'])
        assert data['sent']==0
    assert not session.scalar(select(m.Message))
