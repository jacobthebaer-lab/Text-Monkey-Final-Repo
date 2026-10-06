"""Signed-in review API; all records, Gloo outputs and providers are fictional."""
from datetime import timedelta
import json
from types import SimpleNamespace
from uuid import uuid4

from fastapi.testclient import TestClient
import pytest
from sqlalchemy import select

from app.agents.fill_agent import FillContext
from app.config import Settings
from app.core.inbound import handle_inbound
from app.db import models as m
from app.llm.gloo_client import GlooUnavailableError
from app.main import create_app
from app.web.split_coverage import router
from app.web.texty import admin
from tests.test_fill_agent import ScriptedAgentGloo, historical_invitation, parser_returning

USER = {'id':'00000000-0000-4000-8000-000000000001','email':'fictional-admin@example.invalid'}


@pytest.fixture
def split_app(tmp_path, clock):
    application = create_app(Settings(database_url=f'sqlite:///{tmp_path}/split.db',demo_mode=True))
    application.state.clock = clock
    application.dependency_overrides[admin] = lambda:USER
    with application.state.session_factory() as session:
        session.info['record_authorized'] = True
        role=m.Role(name='Fictional Reviewed Hospitality',ministry='fictional',required_qualifications=[],
                    criticality='standard',fill_policy='auto')
        person=m.Volunteer(name='Fictional Partial Helper',phone='+15550209991',sms_opt_in=True,
                           status='active',preferences={},created_at=clock.now())
        event=m.Event(title='Fictional Sunday',starts_at=clock.now()+timedelta(days=3),
                      ends_at=clock.now()+timedelta(days=3,hours=2),status='scheduled')
        session.add_all([role,person,event]);session.flush()
        parent=m.Shift(role_id=role.id,event_id=event.id,slot_index=0)
        session.add(parent);session.flush()
        fill=m.FillRequest(shift_id=parent.id,urgency='normal',state='in_progress',created_at=clock.now())
        session.add(fill);session.flush()
        session.add(m.Policy(key=f'split_role:{role.id}',value={'value':True}))
        offer=historical_invitation(session,clock,person,fill)
        ctx=FillContext(session,clock,application.state.provider,ScriptedAgentGloo(),log_dir=tmp_path)
        handle_inbound(session,clock,application.state.provider,person.phone,'Only the first hour works',
                       parser_returning(intent='partial',partial_window='first hour'),ctx=ctx)
        incoming=session.scalar(select(m.Message).where(m.Message.direction=='in'))
        application.test_source={'parent_id':parent.id,'outreach_id':offer.id,'incoming_id':incoming.id,
                                 'request_id':str(uuid4())}
        application.test_role=role.id
        start,end=parent.starts_at,parent.starts_at+timedelta(hours=1)
        session.commit()
    calls=[]
    def compose(**kwargs):
        calls.append(kwargs)
        return SimpleNamespace(output_text=json.dumps({'start':start.isoformat(),'end':end.isoformat()}))
    application.state.gloo=SimpleNamespace(create_response=compose,calls=calls,settings=application.state.settings)
    return application


def test_prepare_and_exact_partition_apply_are_idempotent_and_book_nobody(split_app):
    with TestClient(split_app) as client:
        initial=client.get('/api/split-coverage').json()
        assert len(initial['evidence'])==1
        prepared=client.post('/api/split-coverage/partitions',json=split_app.test_source)
        assert prepared.status_code==200
        review=prepared.json()
        assert client.post('/api/split-coverage/partitions',json=split_app.test_source).json()['id']==review['id']
        assert len(split_app.state.gloo.calls)==1
        applied=client.post(f"/api/split-coverage/reviews/{review['id']}/approve",json={'content_hash':review['content_hash']})
        assert applied.status_code==200 and applied.json()['sent']==0
        again=client.post(f"/api/split-coverage/reviews/{review['id']}/approve",json={'content_hash':review['content_hash']})
        assert again.status_code==200
        coverage=client.get('/api/split-coverage').json()['coverage'][0]
        assert coverage['initial_pending'] and not coverage['fully_covered'] and len(coverage['children'])==2
    with split_app.state.session_factory() as session:
        assert len(session.scalars(select(m.Event)).all())==1
        assert session.scalar(select(m.Assignment)) is None
    assert not split_app.state.provider.sent


def test_auth_owner_hash_and_explicit_role_opt_in_are_required(split_app):
    with TestClient(split_app) as client:
        split_app.dependency_overrides.clear()
        assert client.get('/api/split-coverage').status_code in (401,503)  # Unconfigured auth also fails closed.
        split_app.dependency_overrides[admin]=lambda:USER
        review=client.post('/api/split-coverage/partitions',json=split_app.test_source).json()
        assert client.post(f"/api/split-coverage/reviews/{review['id']}/approve",json={'content_hash':'different'}).status_code==409
        split_app.dependency_overrides[admin]=lambda:{**USER,'id':'00000000-0000-4000-8000-000000000002'}
        assert client.post(f"/api/split-coverage/reviews/{review['id']}/approve",json={'content_hash':review['content_hash']}).status_code==409
        split_app.dependency_overrides[admin]=lambda:USER
        assert client.post(f'/api/split-coverage/roles/{split_app.test_role}',json={'allowed':False}).status_code==200
        assert client.post(f"/api/split-coverage/reviews/{review['id']}/approve",json={'content_hash':review['content_hash']}).status_code==409
        assert client.post(f'/api/split-coverage/roles/{split_app.test_role}',json={'allowed':'yes'}).status_code==422
    with split_app.state.session_factory() as session:
        assert not session.scalars(select(m.Shift).where(m.Shift.parent_shift_id.is_not(None))).all()


def test_gloo_unavailable_holds_without_review_or_fallback(split_app):
    def unavailable(**kwargs):raise GlooUnavailableError('fictional outage')
    split_app.state.gloo.create_response=unavailable
    with TestClient(split_app) as client:
        assert client.post('/api/split-coverage/partitions',json=split_app.test_source).status_code==503
    with split_app.state.session_factory() as session:
        assert session.scalar(select(m.Approval).where(m.Approval.kind=='confirm_split_partition')) is None
        assert not session.scalars(select(m.Shift).where(m.Shift.parent_shift_id.is_not(None))).all()


def test_actual_child_acceptances_require_exact_final_api_review_before_booking(split_app,tmp_path):
    from tests.test_reviewed_split_coverage import delivered_child_offer
    with TestClient(split_app) as client:
        proposal=client.post('/api/split-coverage/partitions',json=split_app.test_source).json()
        assert client.post(f"/api/split-coverage/reviews/{proposal['id']}/approve",json={'content_hash':proposal['content_hash']}).status_code==200
        parent=split_app.test_source['parent_id']
        assert client.post(f'/api/split-coverage/{parent}/outreach',json={'content_hash':'different'}).status_code==409
        assert client.post(f'/api/split-coverage/{parent}/outreach',json={'content_hash':proposal['content_hash']}).json()['sent']==0
        assert client.post(f'/api/split-coverage/{parent}/booking-review',json={}).status_code==409
        with split_app.state.session_factory() as session:
            session.info['record_authorized']=True
            children=session.scalars(select(m.Shift).where(m.Shift.parent_shift_id==parent)).all()
            for index,child in enumerate(children):
                person=m.Volunteer(name=f'Fictional Accepted Interval {index}',phone=f'+1555020989{index}',sms_opt_in=True,
                    status='active',preferences={},created_at=split_app.state.clock.now())
                session.add(person);session.flush()
                fill=session.scalar(select(m.FillRequest).where(m.FillRequest.shift_id==child.id))
                delivered_child_offer(session,split_app.state.clock,person,fill)
                ctx=FillContext(session,split_app.state.clock,split_app.state.provider,ScriptedAgentGloo(),log_dir=tmp_path)
                result=handle_inbound(session,split_app.state.clock,split_app.state.provider,person.phone,'Yes to that interval',
                    parser_returning(intent='accept'),ctx=ctx)
                assert 'awaiting_split_review' in result.notes
            assert session.scalar(select(m.Assignment)) is None
            session.commit()
        review=client.post(f'/api/split-coverage/{parent}/booking-review',json={}).json()
        assert review['kind']=='confirm_split_booking'
        assert client.post(f"/api/split-coverage/reviews/{review['id']}/approve",json={'content_hash':review['content_hash']}).status_code==200
        coverage=client.get('/api/split-coverage').json()['coverage'][0]
        assert coverage['fully_covered'] and len(coverage['children'])==2
        state=client.get('/api/state').json()
        assert len(state['shifts'])==len(state['assignments'])==2
        assert all(row['shift_id']!=str(parent) for row in state['assignments'])
        assert {row['starts_at'] for row in state['shifts']}=={row['start'] for row in coverage['children']}
    assert not split_app.state.provider.sent
