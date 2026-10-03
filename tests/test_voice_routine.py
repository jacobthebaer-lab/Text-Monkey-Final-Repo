"""Routine mode safety: real backend policy, fabricated data, headed local DOM."""
from datetime import timedelta
import json

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select

from app.clock import FakeClock
from app.config import Settings
from app.core import notifications, voice_policy
from app.core.send_gate import SendGate, SendStatus
from app.db import models as m
from app.integrations.voice_browser import VoiceConfig, BrowserBlocked
from app.integrations.voice_journal import Journal
from app.integrations.voice_worker import Backend, VoiceWorker
from app.agents.fill_agent import FillContext
from app.main import create_app
from tests.test_voice_bridge import harness, spec, PHONE, SID, NOW, incoming, item, FakeBackend
from tests.test_offer_transport import queue_offer

HEADERS={'Authorization':'Bearer '+'x'*40,'X-TextMonkey-Transport':'google_voice'}


@pytest.fixture
def routine_app(tmp_path,monkeypatch):
    app=create_app(Settings(database_url=f'sqlite:///{tmp_path}/routine.db',sms_provider='google_voice',
        demo_mode=False,automation_enabled=True,mac_bridge_enabled=True,mac_bridge_token='x'*40,
        mac_demo_phones=PHONE,mac_message_services='SMS',admin_password='x'*20,
        mac_test_sessions=json.dumps(spec()['test_sessions']),competition_confirmation_required=False))
    app.state.clock=app.state.mac_delivery_clock=FakeClock(NOW,timezone='UTC')
    with app.state.session_factory() as session:
        session.add(m.Volunteer(name='Synthetic Volunteer',phone=PHONE,sms_opt_in=True,status='active',preferences={},created_at=NOW))
        for key in ('quiet_hours','urgent_quiet_hours'):
            session.add(m.Policy(key=key,value={'value':{'start':'00:00','end':'00:00'}}))
        session.commit()
    monkeypatch.setattr('app.core.notifications.compose_signup_reply',lambda *a,**kw:a[3])
    return app


def post(client,path,data=None):
    return client.post(path,json=data or {},headers=HEADERS)


def booking(client,guid='booking-1'):
    return post(client,'/mac/inbound',{'guid':guid,'phone':PHONE,'body':'Am I booked for anything?',
        'service':'SMS','session_id':SID})


def claim(client):
    return post(client,'/mac/outbound/pull').json()['messages'][0]


def verify(client,claim):
    return post(client,f"/mac/outbound/{claim['id']}/verify",{'token':claim['token'],'policy_hash':claim['policy_hash']})


def add_assignment(app,*,status='confirmed'):
    with app.state.session_factory() as session:
        volunteer=session.scalar(select(m.Volunteer))
        role=m.Role(name='Synthetic greeter',ministry='Welcome',required_qualifications=[],criticality='standard',fill_policy='auto')
        event=m.Event(title='Synthetic service',starts_at=NOW+timedelta(hours=6),ends_at=NOW+timedelta(hours=7),status='scheduled')
        session.add_all([role,event]);session.flush()
        shift=m.Shift(role_id=role.id,event_id=event.id,slot_index=0);session.add(shift);session.flush()
        assignment=m.Assignment(shift_id=shift.id,volunteer_id=volunteer.id,status=status,source='coordinator',created_at=NOW,updated_at=NOW)
        session.add(assignment);session.commit()
        return assignment.id,event.id


def test_natural_booking_reply_is_gate_bound_and_needs_no_text_approval(routine_app):
    with TestClient(routine_app) as client:
        assert booking(client).status_code==200
        row=claim(client)
        assert row['delivery_mode']=='routine' and row['confirmation_required'] is False
        assert 'content_hash' not in row and row['purpose']=='booking_status'
        assert verify(client,row).status_code==200
        assert post(client,f"/mac/outbound/{row['id']}/ack",{'token':row['token'],'outcome':'submitted'}).status_code==200
        assert post(client,'/mac/outbound/pull').json()['messages']==[]
    with routine_app.state.session_factory() as session:
        assert not session.scalar(select(m.Approval))
        proof=voice_policy.proof_for(session,session.get(m.Message,row['id']))
        assert proof.detail['payload']['context']['reply_to']
        assert session.scalar(select(m.Message).where(m.Message.direction=='in')).purpose=='test:'+SID


def test_arbitrary_and_unchecked_queue_rows_cannot_claim(routine_app):
    with routine_app.state.session_factory() as session:
        volunteer=session.scalar(select(m.Volunteer))
        gate=SendGate(session,routine_app.state.clock,routine_app.state.provider)
        for purpose in ('admin_reply','escalation_notify','coordinator_notify','booking_status','reminder','confirmation'):
            outcome=gate.send(body='Arbitrary text',purpose=purpose,volunteer=volunteer)
            assert outcome.status==SendStatus.BLOCKED_TRANSPORT
        assert not session.scalar(select(m.Message))
        session.add(m.Message(direction='out',volunteer_id=volunteer.id,phone=PHONE,body='Unchecked queue',
            purpose='booking_status',provider_sid='MAC'+SID+':forged',status='queued',kind='ai',created_at=NOW))
        session.commit()
    with TestClient(routine_app) as client:
        assert post(client,'/mac/outbound/pull').json()['messages']==[]
    with routine_app.state.session_factory() as session:
        assert session.scalar(select(m.Message)).status=='blocked_policy'


@pytest.mark.parametrize('restriction',['opt_out','phone','care','quiet','schedule','expired','proof','newer_input','disabled_mode'])
def test_mutable_routine_restrictions_rechecked_after_claim(routine_app,restriction):
    aid,_=add_assignment(routine_app)
    with TestClient(routine_app) as client:
        booking(client);row=claim(client)
        with routine_app.state.session_factory() as session:
            volunteer=session.scalar(select(m.Volunteer))
            if restriction=='opt_out':volunteer.sms_opt_in=False
            elif restriction=='phone':volunteer.phone='+12025550999'
            elif restriction=='care':session.add(m.Escalation(category='sensitive',severity='normal',summary='Synthetic care hold',related_ids={'phone':PHONE},status='open',created_at=NOW))
            elif restriction=='quiet':session.get(m.Policy,'quiet_hours').value={'value':{'start':'00:00','end':'23:59'}}
            elif restriction=='schedule':session.get(m.Assignment,aid).status='cancelled'
            elif restriction=='proof':session.get(m.Message,row['id']).body='Changed unchecked text'
            elif restriction=='newer_input':session.add(m.Message(direction='in',volunteer_id=volunteer.id,phone=PHONE,body='Another input',purpose='test:'+SID,kind='mac_test_in',status='received',created_at=NOW))
            session.commit()
        if restriction=='expired':routine_app.state.clock.advance(timedelta(minutes=11))
        if restriction=='disabled_mode':routine_app.state.session_factory.configure(info={'competition_confirmation_required':True})
        assert verify(client,row).status_code==409
        assert post(client,'/mac/outbound/pull').json()['messages']==[]


def test_reminder_requires_saved_notification_and_current_assignment(routine_app):
    _,eid=add_assignment(routine_app)
    with routine_app.state.session_factory() as session:
        volunteer=session.scalar(select(m.Volunteer))
        notification=notifications.deliver(FillContext(session,routine_app.state.clock,routine_app.state.provider,None),
            key='reminder:synthetic',purpose='reminder',body='Your upcoming assigned shift',volunteer=volunteer,event_id=eid)
        assert notification.state=='sent'
        session.commit()
    with TestClient(routine_app) as client:
        row=claim(client)
        assert row['purpose']=='reminder'
        assert verify(client,row).status_code==200
        routine_app.state.provider.automation_enabled=False
        assert verify(client,row).status_code==409


def test_latest_offer_deadline_and_proof_refresh_at_preflight(routine_app):
    clock=routine_app.state.clock
    oid,_=queue_offer(routine_app,clock)
    with TestClient(routine_app) as client:
        row=claim(client)
        clock.advance(timedelta(minutes=2))
        proof=verify(client,row)
        assert proof.status_code==200,proof.text
        assert proof.json()['body']!=row['body'] and proof.json()['policy_hash']!=row['policy_hash']
        updated={**row,**proof.json()}
        again=verify(client,updated)
        assert again.status_code==200
        assert again.json()['body']==proof.json()['body']
        with routine_app.state.session_factory() as session:
            meta=__import__('app.core.offer_windows',fromlist=['metadata']).metadata(session,session.get(m.Outreach,oid))
            deadline=meta.expires_at
            assert meta.state=='offer_active'
        clock.advance(timedelta(minutes=1))
        assert verify(client,updated).json()['policy_hash']==proof.json()['policy_hash']
        with routine_app.state.session_factory() as session:
            assert __import__('app.core.offer_windows',fromlist=['metadata']).metadata(session,session.get(m.Outreach,oid)).expires_at==deadline


def test_expired_offer_and_restricted_role_are_not_unchecked_routine(routine_app):
    clock=routine_app.state.clock
    oid,_=queue_offer(routine_app,clock,lead=timedelta(minutes=15))
    with TestClient(routine_app) as client:
        row=claim(client);clock.advance(timedelta(minutes=6))
        assert verify(client,row).status_code==409
    with routine_app.state.session_factory() as session:
        assert session.get(m.Message,row['id']).status=='blocked_policy'


def test_unknown_delivery_holds_all_later_output(routine_app):
    with TestClient(routine_app) as client:
        booking(client);row=claim(client)
        assert verify(client,row).status_code==200
        assert post(client,f"/mac/outbound/{row['id']}/ack",{'token':row['token'],'outcome':'uncertain'}).status_code==200
        booking(client,'booking-2')
        assert post(client,'/mac/outbound/pull').json()['messages']==[]
        assert post(client,f"/mac/outbound/{row['id']}/ack",{'token':row['token'],'outcome':'submitted'}).status_code==409


def test_stop_after_claim_rejects_preflight_and_has_no_unchecked_ack(routine_app):
    with TestClient(routine_app) as client:
        booking(client);row=claim(client)
        stop=post(client,'/mac/inbound',{'guid':'stop-1','phone':PHONE,'body':'STOP','service':'SMS','session_id':SID})
        assert stop.status_code==200
        assert verify(client,row).status_code==409
        assert post(client,'/mac/outbound/pull').json()['messages']==[]
    with routine_app.state.session_factory() as session:
        assert session.scalar(select(m.Volunteer)).sms_opt_in is False
        assert not session.scalar(select(m.Message).where(m.Message.purpose=='stop_confirm'))


def test_headed_routine_flow_uses_normal_reply_and_same_history(harness,routine_app,tmp_path):
    previous,page,_,_=harness
    data=spec()|{'delivery_mode':'routine'}
    config=VoiceConfig(data,fixture=True)
    previous.browser.config=config
    journal=Journal(tmp_path/'routine-journal.db',config.identity())
    with TestClient(routine_app) as client:
        backend=Backend('http://localhost','x'*40,client=client)
        backend.check_configuration(config)
        worker=VoiceWorker(config,previous.browser,journal,backend,ingress=True,outbound=True,now=lambda:NOW)
        worker.once()
        incoming(page,body='Am I booked for anything?')
        worker.once();worker.once()
        assert page.evaluate('clicks')==1
        assert journal.operations()[0]['state']=='observed_sent'
        with routine_app.state.session_factory() as session:
            assert not session.scalar(select(m.Approval))
            assert session.scalar(select(m.Message).where(m.Message.direction=='out')).status=='submitted'
            assert session.scalar(select(m.Message).where(m.Message.direction=='in')).body=='Am I booked for anything?'
        incoming(page,'stop-after-send','STOP');worker.once()
        assert page.evaluate('clicks')==1
    journal.db.close()


def test_worker_modes_are_separate_and_missing_policy_never_clicks(harness,tmp_path):
    worker,page,backend,_=harness
    routine={'id':1,'token':'x'*64,'phone':PHONE,'body':'Synthetic reply','session_id':SID,
             'delivery_mode':'routine','confirmation_required':False,'purpose':'booking_status',
             'policy_hash':'b'*64,'policy_expires_at':(NOW+timedelta(minutes=10)).isoformat()}
    with pytest.raises(BrowserBlocked):worker.valid_item(routine)
    config=VoiceConfig(spec()|{'delivery_mode':'routine'},fixture=True)
    worker.config=config
    with pytest.raises(BrowserBlocked):worker.valid_item(item())
    with pytest.raises(BrowserBlocked):worker.valid_item({k:v for k,v in routine.items() if k!='policy_hash'})
    with pytest.raises(BrowserBlocked):worker.valid_item({**routine,'purpose':'admin_reply'})
    worker.valid_item(routine)
    assert page.evaluate('clicks')==0
    journal=Journal(tmp_path/'bound.db',config.identity());journal.db.close()
    with pytest.raises(ValueError):Journal(tmp_path/'bound.db',spec())


@pytest.mark.parametrize('restriction',['budget','cooldown','role_policy','automation'])
def test_offer_rechecks_budget_role_and_automation_at_click(routine_app,restriction):
    clock=routine_app.state.clock
    oid,_=queue_offer(routine_app,clock)
    with TestClient(routine_app) as client:
        row=claim(client)
        with routine_app.state.session_factory() as session:
            outreach=session.get(m.Outreach,oid)
            if restriction in {'budget','cooldown'}:
                for index in range(4 if restriction=='budget' else 1):
                    session.add(m.Message(direction='out',volunteer_id=outreach.volunteer_id,phone=PHONE,
                        body='Synthetic previous ask',purpose='outreach',kind='template',status='submitted',created_at=NOW-timedelta(minutes=1)))
            elif restriction=='role_policy':
                session.get(m.Shift,session.get(m.FillRequest,outreach.fill_request_id).shift_id).role.fill_policy='needs_approval'
            session.commit()
        if restriction=='automation':routine_app.state.provider.automation_enabled=False
        assert verify(client,row).status_code==409
        assert post(client,'/mac/outbound/pull').json()['messages']==[]


@pytest.mark.parametrize('purpose,key_prefix,status',[('confirmation','winner','confirmed'),('cancellation_ack','cancel','cancelled'),('filled_thanks','closed','confirmed')])
def test_application_booking_updates_have_notification_provenance(routine_app,purpose,key_prefix,status):
    aid,eid=add_assignment(routine_app,status=status)
    with routine_app.state.session_factory() as session:
        volunteer=session.scalar(select(m.Volunteer))
        assignment=session.get(m.Assignment,aid)
        fill=m.FillRequest(shift_id=assignment.shift_id,urgency='normal',state='filled',current_tranche=1,created_at=NOW)
        session.add(fill);session.flush()
        if purpose=='filled_thanks':
            prior=m.Message(direction='out',volunteer_id=volunteer.id,phone=PHONE,body='Earlier delivered invitation',
                purpose='outreach',kind='template',status='submitted',created_at=NOW-timedelta(minutes=20))
            session.add(prior);session.flush()
            session.add(m.Outreach(fill_request_id=fill.id,volunteer_id=volunteer.id,tranche=1,message_id=prior.id,response='no'))
            session.flush()
        key=f'cancel:{aid}' if key_prefix=='cancel' else f'{key_prefix}:{fill.id}:{volunteer.id}'
        notification=notifications.deliver(FillContext(session,routine_app.state.clock,routine_app.state.provider,None),
            key=key,purpose=purpose,body='Synthetic saved booking update',volunteer=volunteer,event_id=eid)
        assert notification.state=='sent'
        session.commit()
    with TestClient(routine_app) as client:
        row=claim(client)
        assert row['purpose']==purpose
        assert verify(client,row).status_code==200


@pytest.mark.parametrize('drop_send',[False,True])
def test_routine_worker_handles_preflight_deadline_refresh_before_click(harness,tmp_path,drop_send):
    old,page,_,_=harness
    config=VoiceConfig(spec()|{'delivery_mode':'routine'},fixture=True)
    old.browser.config=config
    journal=Journal(tmp_path/'refresh.db',config.identity())
    class RefreshBackend(FakeBackend):
        def verify(self,row):
            return {'verified':True,'phone':PHONE,'body':'Updated invitation and latest deadline',
                'delivery_mode':'routine','confirmation_required':False,'purpose':'outreach',
                'policy_hash':'c'*64,'policy_expires_at':(NOW+timedelta(minutes=10)).isoformat()}
    backend=RefreshBackend()
    page.evaluate('window.dropSend='+str(drop_send).lower())
    worker=VoiceWorker(config,old.browser,journal,backend,ingress=True,outbound=True,now=lambda:NOW)
    worker.once()
    backend.items=[{'id':1,'token':'x'*64,'phone':PHONE,'body':'Earlier draft deadline','session_id':SID,
        'delivery_mode':'routine','confirmation_required':False,'purpose':'outreach','policy_hash':'b'*64,
        'policy_expires_at':(NOW+timedelta(minutes=10)).isoformat()}]
    worker.once();worker.once()
    assert page.evaluate('clicks')==1
    assert journal.operations()[0]['state']==('unknown' if drop_send else 'observed_sent')
    assert json.loads(journal.operations()[0]['item'])['body']=='Updated invitation and latest deadline'
    if not drop_send:
        assert page.locator('[data-direction=out] [data-body]').inner_text()=='Updated invitation and latest deadline'
    else:
        backend.items=[{**json.loads(journal.operations()[0]['item']),'id':2}]
        with pytest.raises(BrowserBlocked):worker.once()
        assert page.evaluate('clicks')==1
    journal.db.close()


def test_policy_expiry_compares_instants_across_church_timezone(routine_app):
    from zoneinfo import ZoneInfo
    routine_app.state.clock=routine_app.state.mac_delivery_clock=FakeClock(NOW.astimezone(ZoneInfo('America/Denver')))
    with TestClient(routine_app) as client:
        assert booking(client).status_code==200
        row=claim(client)
        assert verify(client,row).status_code==200


def test_routine_role_approval_cannot_be_invented_with_private_flag(routine_app):
    oid,_=queue_offer(routine_app,routine_app.state.clock)
    with routine_app.state.session_factory() as session:
        outreach=session.get(m.Outreach,oid)
        shift=session.get(m.Shift,session.get(m.FillRequest,outreach.fill_request_id).shift_id)
        shift.role.fill_policy='needs_approval'
        proof=voice_policy.proof_for(session,session.get(m.Message,outreach.message_id))
        payload={**proof.detail['payload'],'restricted_approved':True,
            'context':{**proof.detail['payload']['context'],'shift':__import__('app.core.offer_windows',fromlist=['snapshot']).snapshot(shift)}}
        proof.detail={'payload':payload,'hash':voice_policy.digest(payload)}
        session.commit()
    with TestClient(routine_app) as client:
        assert post(client,'/mac/outbound/pull').json()['messages']==[]
