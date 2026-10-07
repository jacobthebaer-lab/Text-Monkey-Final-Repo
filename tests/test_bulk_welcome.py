"""Bounded explicit welcomes through real gates, fabricated recipients/Gloo only."""
import json
from dataclasses import replace
from types import SimpleNamespace
from uuid import uuid4
import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select
from app.db import models as m
from app.web.texty import admin
from app.llm.gloo_client import GlooUnavailableError
from app.integrations.test_sessions import parse_sessions
from tests.session_fixtures import session_specs
from tests.test_mac_messages import mac_app


@pytest.fixture
def welcome_app(mac_app):
    app=mac_app;phones=[f'+13035552{i:03}' for i in range(40)]
    app.state.provider.phones=frozenset(phones)
    app.state.provider.test_sessions=parse_sessions(session_specs(phones,app.state.clock.now()),set(phones))
    app.state.settings=replace(app.state.settings,mac_demo_phones=','.join(phones),
        mac_test_sessions=json.dumps(session_specs(phones,app.state.clock.now())),gloo_signup_replies=True)
    calls=[];faults=set();identities={}
    def compose(**kwargs):
        facts=json.loads(kwargs['input']);phone=identities[facts['sender']['volunteer_id']];calls.append(phone)
        if phone in faults: raise GlooUnavailableError('Synthetic Gloo outage')
        return SimpleNamespace(output_text='What roles would you like to help with? Reply ANY, or STOP to stop.')
    app.state.gloo=SimpleNamespace(settings=app.state.settings,create_response=compose)
    app.dependency_overrides[admin]=lambda:{'email':'coordinator@example.test'}
    with app.state.session_factory() as session:
        session.add(m.Policy(key='full_text_onboarding',value={'value':True}))
        people=[]
        for i,phone in enumerate(phones):
            person=m.Volunteer(name=f'Example Volunteer {i}',phone=phone,sms_opt_in=True,status='active',
                preferences={'onboarding_stage':'complete'},created_at=app.state.clock.now())
            session.add(person);session.flush();people.append(person.id);identities[person.id]=phone
        session.commit()
    yield SimpleNamespace(app=app,phones=phones,people=people,calls=calls,faults=faults)
    app.dependency_overrides.clear()


def run(client,ids,request_id=None):
    request_id=request_id or str(uuid4());body={'request_id':request_id,'volunteer_ids':ids}
    while True:
        response=client.post('/api/welcome-batches',json=body)
        assert response.status_code==200,response.text
        result=response.json()
        if result['done']:return result,body


def test_40_people_progress_one_per_request_and_never_repeat_successes(welcome_app):
    f=welcome_app;ids=f.people;body={'request_id':str(uuid4()),'volunteer_ids':ids}
    with TestClient(f.app) as client:
        first=client.post('/api/welcome-batches',json=body)
        assert first.status_code==200,first.text
        assert first.json()['completed']==1 and not first.json()['done']
        assert len(f.calls)==1
        result,_=run(client,ids,body['request_id'])
        assert len(result['results'])==40 and all(r['status']=='prepared' for r in result['results'])
        assert len(f.calls)==40
        # An HTTP response can be lost after commit. The same completed request
        # and a newly selected batch both preserve per-person queue identity.
        assert client.post('/api/welcome-batches',json=body).json()==result
        duplicate,_=run(client,ids)
        assert all(r['status']=='already_prepared' for r in duplicate['results'])
        assert len(f.calls)==40
        assert client.post('/api/welcome-batches',json={**body,'volunteer_ids':ids[:1]}).status_code==409
    with f.app.state.session_factory() as session:
        assert len(session.scalars(select(m.Message).where(m.Message.direction=='out')).all())==40
        assert len(session.scalars(select(m.Notification).where(m.Notification.purpose=='volunteer_welcome')).all())==40
        assert session.scalar(select(m.Approval)) is None


def test_partial_gloo_failure_and_consent_change_retry_only_unsuccessful_recipients(welcome_app):
    f=welcome_app;f.faults.add(f.phones[1])
    with TestClient(f.app) as client:
        request_id=str(uuid4());body={'request_id':request_id,'volunteer_ids':f.people[:3]}
        first=client.post('/api/welcome-batches',json=body).json();assert first['results'][0]['status']=='prepared'
        with f.app.state.session_factory() as session:
            session.get(m.Volunteer,f.people[2]).sms_opt_in=False;session.commit()
        result,_=run(client,f.people[:3],request_id)
        assert [r['status'] for r in result['results']]==['prepared','failed','held']
        assert len(f.calls)==2
        with f.app.state.session_factory() as session:
            assert session.get(m.Volunteer,f.people[1]).preferences['onboarding_stage']=='complete'
            assert session.get(m.Volunteer,f.people[2]).preferences['onboarding_stage']=='complete'
        f.faults.clear()
        retry,_=run(client,f.people[:3])
        assert [r['status'] for r in retry['results']]==['already_prepared','prepared','held']
        assert len(f.calls)==3
        assert client.post(f'/api/volunteers/{f.people[0]}/text-setup').json()['duplicate']
        assert len(f.calls)==3


def test_staged_welcome_review_deduplicates_across_new_batch_ids_even_with_stage_complete(welcome_app):
    from app.core import confirmations
    f=welcome_app
    f.app.state.settings=replace(f.app.state.settings,competition_confirmation_required=True)
    f.app.state.session_factory.configure(info={confirmations.MODE_KEY:True})
    with TestClient(f.app) as client:
        result,_=run(client,f.people[:1])
        assert result['results'][0]['delivery']=='awaiting_confirmation'
        with f.app.state.session_factory() as session:
            # Explicitly reproduce a stored stage that has not yet applied the
            # reviewed preference change. The receipt/review must still dedupe.
            person=session.get(m.Volunteer,f.people[0]);person.preferences={'onboarding_stage':'complete'}
            session.info['record_authorized']=True;session.commit()
        second,_=run(client,f.people[:1]);assert second['results'][0]['status']=='already_prepared'
        assert second['results'][0]['approval_id']==result['results'][0]['approval_id']
        assert len(f.calls)==1
    with f.app.state.session_factory() as session:
        assert len(session.scalars(select(m.Approval).where(m.Approval.kind=='confirm_text')).all())==1
        assert session.scalar(select(m.Message)) is None


def test_fictional_opted_out_and_unconnected_people_are_held_without_gloo(welcome_app):
    f=welcome_app
    with f.app.state.session_factory() as session:
        session.get(m.Volunteer,f.people[0]).preferences={'synthetic':True,'fictional_seed':True}
        session.get(m.Volunteer,f.people[1]).phone='+12025550123'
        session.add(m.Policy(key='sms_opt_out:'+f.phones[2],value={'value':True}))
        session.get(m.Volunteer,f.people[3]).status='inactive';session.commit()
    f.app.state.provider.test_sessions.pop(f.phones[4])
    with TestClient(f.app) as client:
        result,_=run(client,f.people[:5]);assert all(r['status']=='held' for r in result['results'])
        assert f.calls==[]
        assert client.post('/api/welcome-batches',json={'request_id':str(uuid4()),'volunteer_ids':[True]}).status_code==400
        assert client.post('/api/welcome-batches',json={'request_id':str(uuid4()),'volunteer_ids':[f.people[0]]*2}).status_code==400
        assert client.post('/api/welcome-batches',json={'request_id':'bad','volunteer_ids':f.people[:1]}).status_code==400
        f.app.dependency_overrides.clear()
        assert client.post('/api/welcome-batches',json={'request_id':str(uuid4()),'volunteer_ids':f.people[:1]}).status_code!=200


@pytest.mark.parametrize('terminal',['rejected','expired','cancelled'])
def test_terminal_review_can_deliberately_retry_with_fresh_proof_and_audit(welcome_app,terminal):
    from app.core import confirmations
    f=welcome_app
    f.app.state.settings=replace(f.app.state.settings,competition_confirmation_required=True)
    f.app.state.session_factory.configure(info={confirmations.MODE_KEY:True})
    with TestClient(f.app) as client:
        first,body=run(client,f.people[:1]);old_id=first['results'][0]['approval_id']
        with f.app.state.session_factory() as session:
            old=session.get(m.Approval,old_id);old.status=terminal
            session.add(m.Notification(key=f'review:{old_id}:reject',purpose='human_review',state='sent',
                due_at=f.app.state.clock.now(),created_at=f.app.state.clock.now(),detail={'approval_id':old_id,'action':'reject'}))
            session.commit()
        roster=client.get('/api/state').json()['volunteers']
        assert next(p for p in roster if p['id']==str(f.people[0]))['can_start_text_setup']
        assert client.post('/api/welcome-batches',json=body).json()==first
        assert len(f.calls)==1
        retried,_=run(client,f.people[:1]);result=retried['results'][0]
        assert result['status']=='prepared' and result['approval_id']!=old_id
        assert result['delivery']=='awaiting_confirmation' and len(f.calls)==2
        repeated,_=run(client,f.people[:1]);assert repeated['results'][0]['status']=='already_prepared'
        assert len(f.calls)==2
    with f.app.state.session_factory() as session:
        assert session.get(m.Approval,old_id).status==terminal
        assert len(session.scalars(select(m.Notification).where(m.Notification.purpose=='volunteer_welcome_attempt')).all())==1
        assert session.get(m.Approval,result['approval_id']).payload['conversation']['welcome_retry']
        assert session.scalar(select(m.Message)) is None


def test_queue_guard_proves_no_native_attempt_before_deliberate_retry(welcome_app):
    from tests.test_mac_messages import post
    f=welcome_app
    with TestClient(f.app) as client:
        first,_=run(client,f.people[:1]);old_id=first['results'][0]['message_id']
        with f.app.state.session_factory() as session:
            session.get(m.Volunteer,f.people[0]).sms_opt_in=False;session.commit()
        assert post(client,'/mac/outbound/pull').json()['messages']==[]
        with f.app.state.session_factory() as session:
            assert session.get(m.Message,old_id).status=='blocked_opt_out'
            assert session.get(m.Notification,f'welcome-presend:{old_id}')
            session.get(m.Volunteer,f.people[0]).sms_opt_in=True;session.commit()
        retried,_=run(client,f.people[:1]);new_id=retried['results'][0]['message_id']
        assert retried['results'][0]['status']=='prepared' and new_id!=old_id
        assert len(f.calls)==2
        queued=post(client,'/mac/outbound/pull').json()['messages']
        assert [row['id'] for row in queued]==[new_id]
        assert post(client,f'/mac/outbound/{new_id}/verify',{'token':queued[0]['token']}).status_code==200
    with f.app.state.session_factory() as session:
        assert session.get(m.Message,old_id).status=='blocked_opt_out'
        assert session.get(m.Notification,f'conversation-message:{old_id}')
        assert session.get(m.Notification,'volunteer-welcome:'+str(f.people[0])).message_id==new_id


@pytest.mark.parametrize('status',['queued','dispatching','submitted','uncertain','blocked_native_route','blocked_opt_out'])
def test_terminal_label_or_missing_claim_alone_never_authorizes_retry(welcome_app,status):
    f=welcome_app
    with TestClient(f.app) as client:
        first,_=run(client,f.people[:1]);message_id=first['results'][0]['message_id']
        with f.app.state.session_factory() as session:
            session.get(m.Message,message_id).status=status;session.commit()
        again,_=run(client,f.people[:1]);assert again['results'][0]['status']=='already_prepared'
        assert len(f.calls)==1


def test_real_inbound_prevents_restarting_a_terminal_welcome(welcome_app):
    f=welcome_app
    with TestClient(f.app) as client:
        first,_=run(client,f.people[:1]);message_id=first['results'][0]['message_id']
        with f.app.state.session_factory() as session:
            message=session.get(m.Message,message_id);message.status='blocked_opt_out'
            session.add(m.Notification(key=f'welcome-presend:{message_id}',purpose='welcome_presend',state='blocked',
                created_at=f.app.state.clock.now(),due_at=f.app.state.clock.now(),detail={
                    'phone':message.phone,'provider_sid':message.provider_sid,'status':message.status,'phase':'queued_before_claim'}))
            session.add(m.Message(volunteer_id=f.people[0],phone=message.phone,direction='in',kind='mac_test_in',
                purpose='test:fixture',status='received',body='Synthetic existing conversation',created_at=f.app.state.clock.now()))
            session.commit()
        again,_=run(client,f.people[:1]);assert again['results'][0]['status']=='already_prepared'
        assert len(f.calls)==1


@pytest.mark.parametrize('review',[False,True])
@pytest.mark.parametrize('legacy',[False,True])
def test_explicit_native_route_no_attempt_receipt_permits_fresh_welcome_and_keeps_claim(welcome_app,review,legacy):
    from copy import deepcopy
    from app.core import confirmations
    from app.core.send_gate import SendGate
    from tests.test_mac_messages import post
    from app.integrations.mac_models import MacDeliveryClaim
    f=welcome_app
    if review:
        f.app.state.settings=replace(f.app.state.settings,competition_confirmation_required=True)
        f.app.state.session_factory.configure(info={confirmations.MODE_KEY:True})
    def approve(approval_id):
        with f.app.state.session_factory() as session:
            approval=session.get(m.Approval,approval_id)
            gate=SendGate(session,f.app.state.clock,f.app.state.provider)
            confirmations.decide(session,gate,approval,approve=True,actor='coordinator@example.test',
                expected=approval.payload['content_hash'],now=f.app.state.clock.now())
            session.commit()
            return approval.payload['message_id']
    with TestClient(f.app) as client:
        first,body=run(client,f.people[:1]);old_result=first['results'][0]
        old_id=approve(old_result['approval_id']) if review else old_result['message_id']
        item=post(client,'/mac/outbound/pull').json()['messages'][0]
        with f.app.state.session_factory() as session:
            old_body=session.get(m.Message,old_id).body
            old_conversation=deepcopy(session.get(m.Notification,f'conversation-message:{old_id}').detail)
            old_review=deepcopy(session.get(m.Approval,old_result['approval_id']).payload) if review else None
            if legacy:
                session.delete(session.get(m.Notification,'volunteer-welcome:'+str(f.people[0])));session.commit()
        held=post(client,f'/mac/outbound/{old_id}/route-hold',{'token':item['token']})
        assert held.status_code==200,held.text
        assert client.post('/api/welcome-batches',json=body).json()==first
        assert len(f.calls)==1
        again,_=run(client,f.people[:1]);assert again['results'][0]['status']=='prepared'
        new=again['results'][0]
        assert len(f.calls)==2
        if review:
            assert new['approval_id']!=old_result['approval_id'] and new['delivery']=='awaiting_confirmation'
            assert post(client,'/mac/outbound/pull').json()['messages']==[]
            new_id=approve(new['approval_id'])
        else:new_id=new['message_id']
        assert new_id!=old_id
        queued=post(client,'/mac/outbound/pull').json()['messages']
        assert [row['id'] for row in queued]==[new_id]
        verified=post(client,f'/mac/outbound/{new_id}/verify',{'token':queued[0]['token'],
            **({'content_hash':queued[0]['content_hash']} if review else {})})
        assert verified.status_code==200,verified.text
    with f.app.state.session_factory() as session:
        assert session.get(MacDeliveryClaim,old_id).token==item['token']
        assert session.get(m.Message,old_id).status=='blocked_native_route'
        assert session.get(m.Message,old_id).body==old_body
        assert session.get(m.Notification,f'conversation-message:{old_id}').detail==old_conversation
        if review:assert session.get(m.Approval,old_result['approval_id']).payload==old_review
        assert len(session.scalars(select(m.Notification).where(m.Notification.purpose=='volunteer_welcome_attempt')).all())==1


def test_concurrent_batches_for_same_person_queue_only_once(welcome_app):
    from concurrent.futures import ThreadPoolExecutor
    f=welcome_app
    def step():
        with TestClient(f.app) as client:return run(client,f.people[:1])[0]
    with ThreadPoolExecutor(max_workers=2) as pool:
        results=list(pool.map(lambda _:step(),range(2)))
    assert sorted(result['results'][0]['status'] for result in results)==['already_prepared','prepared']
    assert len(f.calls)==1
    with f.app.state.session_factory() as session:
        assert len(session.scalars(select(m.Message).where(m.Message.volunteer_id==f.people[0])).all())==1
        assert session.scalar(select(m.Notification).where(m.Notification.state=='pending',m.Notification.purpose=='welcome_batch')) is None


@pytest.mark.parametrize('fault',['missing','state','token','body'])
@pytest.mark.parametrize('legacy',[False,True])
def test_changed_native_no_attempt_proof_never_authorizes_welcome_retry(welcome_app,fault,legacy):
    from tests.test_mac_messages import post
    f=welcome_app
    with TestClient(f.app) as client:
        first,_=run(client,f.people[:1]);old_id=first['results'][0]['message_id']
        claim=post(client,'/mac/outbound/pull').json()['messages'][0]
        assert post(client,f'/mac/outbound/{old_id}/route-hold',{'token':claim['token']}).status_code==200
        with f.app.state.session_factory() as session:
            if legacy:session.delete(session.get(m.Notification,'volunteer-welcome:'+str(f.people[0])))
            proof=session.get(m.Notification,f'mac-native-route:{old_id}')
            if fault=='missing':session.delete(proof)
            elif fault=='state':proof.state='changed'
            elif fault=='token':proof.detail={**proof.detail,'token_hash':'0'*64}
            else:session.get(m.Message,old_id).body='Changed unreviewed synthetic body'
            session.commit()
        again,_=run(client,f.people[:1])
        assert again['results'][0]['status'] in {'already_prepared','held'}
        assert len(f.calls)==1
    with f.app.state.session_factory() as session:
        assert len(session.scalars(select(m.Message).where(m.Message.volunteer_id==f.people[0])).all())==1
        assert session.scalar(select(m.Notification).where(m.Notification.purpose=='welcome_retry')) is None
