"""Welcome/name/roles sequence, fabricated transport receipts and Gloo only."""
import json
from datetime import timedelta
from types import SimpleNamespace
import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select
from app.db import models as m
from app.core.onboarding import handle
from app.core.onboarding_copy import DEFAULTS
from app.core.send_gate import SendGate
from app.integrations.mac_models import MacDeliveryClaim
from tests.test_bulk_welcome import welcome_app
from tests.test_mac_messages import mac_app


def prepare(f):
    with f.app.state.session_factory() as session:
        person=session.get(m.Volunteer,f.people[0]);person.name='Alex Example';session.commit()
    with TestClient(f.app) as client:
        result=client.post(f'/api/volunteers/{f.people[0]}/text-setup')
        assert result.status_code==200,result.text
        assert client.post(f'/api/volunteers/{f.people[0]}/text-setup').json()['duplicate']
        return result.json()['message_id']


def name_reply(f, message_id, *, status='submitted', claim=True, body='Alex Example', extraction=None,
               wrong_session=False, tamper=False):
    calls=[]
    def response(**kwargs):
        facts=json.loads(kwargs['input']);calls.append(facts)
        text=json.dumps(extraction or {'first_name':'Alex','last_name':'Example','sensitive':False}) if facts.get('stage')=='welcome_name' else facts['approved_message']
        return SimpleNamespace(output_text=text)
    gloo=SimpleNamespace(settings=f.app.state.settings,create_response=response)
    with f.app.state.session_factory() as session:
        session.info['record_authorized']=True
        selected=f.app.state.provider.test_sessions[f.phones[0]];session.info['mac_test_session']=selected
        person=session.get(m.Volunteer,f.people[0]);welcome=session.get(m.Message,message_id)
        assert welcome.body==DEFAULTS['welcome']
        assert person.preferences['onboarding_stage']=='welcome_name'
        welcome.status=status
        if claim: session.add(MacDeliveryClaim(message_id=welcome.id,token='synthetic-claim-only'))
        if tamper:welcome.body='A different unreviewed welcome'
        f.app.state.clock.advance(timedelta(seconds=1))
        incoming=m.Message(phone=person.phone,volunteer_id=person.id,direction='in',kind='mac_test_in',
            purpose='test:'+('f'*32 if wrong_session else selected.id),status='received',body=body,created_at=f.app.state.clock.now())
        session.add(incoming);session.flush()
        gate=SendGate(session,f.app.state.clock,f.app.state.provider);gate.reply_to_message_id=incoming.id
        outcome=handle(session,f.app.state.clock,gate,person,body,gloo)
        rows=session.scalars(select(m.Message).where(m.Message.direction=='out').order_by(m.Message.id)).all()
        assert person.sms_opt_in and person.status=='active' and person.qualifications==[]
        return outcome,person.preferences,[(row.body,row.status) for row in rows],calls


def test_new_profile_receives_literal_welcome_then_name_reply_starts_roles(welcome_app):
    f=welcome_app;message_id=prepare(f)
    result,prefs,rows,calls=name_reply(f,message_id)
    assert result=='onboarding_interests' and prefs['onboarding_stage']=='interests'
    assert len(rows)==2 and rows[0][0]==DEFAULTS['welcome']
    assert rows[1][1]=='queued' and 'What would you like to help with?' in rows[1][0]
    assert prefs['welcome_name_evidence']['welcome_id']==message_id
    assert [call.get('stage') for call in calls]==['welcome_name',None]


@pytest.mark.parametrize('status',['queued','dispatching','uncertain','blocked_native_route'])
def test_name_never_advances_before_submitted_welcome(welcome_app,status):
    f=welcome_app;message_id=prepare(f)
    result,prefs,rows,calls=name_reply(f,message_id,status=status)
    assert result=='onboarding_review' and prefs['onboarding_stage']=='welcome_name'
    assert len(rows)==1 and calls==[]


@pytest.mark.parametrize('defect',['no_claim','wrong_session','changed_welcome','made_up_name','partial_name','invalid_json'])
def test_current_sender_and_original_welcome_are_required(welcome_app,defect):
    f=welcome_app;message_id=prepare(f)
    kwargs={}
    if defect=='no_claim':kwargs['claim']=False
    elif defect=='wrong_session':kwargs['wrong_session']=True
    elif defect=='changed_welcome':kwargs['tamper']=True
    elif defect=='made_up_name':kwargs['body']='Greeter'
    elif defect=='partial_name':kwargs['body']='Alex'
    else:kwargs['extraction']={'understood':False}
    result,prefs,rows,calls=name_reply(f,message_id,**kwargs)
    assert result=='onboarding_review' and prefs['onboarding_stage']=='welcome_name'
    assert len(rows)==1 and 'welcome_name_evidence' not in prefs
    if defect in {'no_claim','wrong_session','changed_welcome'}:assert calls==[]


def test_existing_legacy_role_prompt_is_not_rewritten_or_resent(welcome_app):
    from app.core.onboarding import start
    f=welcome_app
    with f.app.state.session_factory() as session:
        person=session.get(m.Volunteer,f.people[0]);session.info['record_authorized']=True
        selected=f.app.state.provider.test_sessions[person.phone];session.info['mac_test_session']=selected
        result=start(session,f.app.state.clock,SendGate(session,f.app.state.clock,f.app.state.provider),person,f.app.state.gloo)
        assert result.sent
        legacy_id=result.message_id;session.commit()
    with TestClient(f.app) as client:
        result=client.post(f'/api/volunteers/{f.people[0]}/text-setup')
        assert result.status_code==200 and result.json()['duplicate']
        assert result.json()['message_id']==legacy_id and len(f.calls)==1
    with f.app.state.session_factory() as session:
        assert session.get(m.Volunteer,f.people[0]).preferences['onboarding_stage']=='interests'
        assert session.get(m.Message,legacy_id).body!=DEFAULTS['welcome']
        assert len(session.scalars(select(m.Message)).all())==1
