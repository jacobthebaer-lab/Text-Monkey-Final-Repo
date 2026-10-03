"""Exact manual preparation uses synthetic Gloo and never native delivery."""
import json
from types import SimpleNamespace
from uuid import uuid4
from datetime import timedelta

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select

from app.core import confirmations
from app.core.message_style import EM_DASH_CHARACTERS
from app.db import models as m
from app.integrations.mac_models import MacDeliveryClaim
from app.llm.gloo_client import GlooUnavailableError
from tests.test_admin_reply import mode_app, normal_mode, sign_in_fixture


class ExactGloo:
    def __init__(self, result=None, outage=False):
        self.calls=[];self.result=result;self.outage=outage
    def create_response(self, **kwargs):
        self.calls.append(kwargs)
        if self.outage:
            raise GlooUnavailableError('Synthetic unavailable')
        body=json.loads(kwargs['input'])['approved_message']
        return SimpleNamespace(output_text=body if self.result is None else self.result)


def request(volunteer, body='  Exact words.\n🐒  '):
    return {'volunteer_id':volunteer.id,'body':body,'request_id':str(uuid4())}


def no_outbound(app):
    with app.state.session_factory() as session:
        assert session.scalar(select(m.Approval)) is None
        assert session.scalar(select(m.Message).where(m.Message.direction=='out')) is None
        assert session.scalar(select(m.Notification).where(m.Notification.purpose=='admin_reply')) is None


@pytest.mark.parametrize('normal',[False,True])
@pytest.mark.parametrize('body',['  Exact words.\n🐒  ','x'*1600])
def test_success_preserves_full_body_hash_and_retry_without_second_model(mode_app,normal,body):
    app,volunteers,headers=mode_app
    if normal: normal_mode(app)
    else: sign_in_fixture(app)
    gloo=app.state.gloo=ExactGloo()
    payload=request(volunteers[0],body)
    with TestClient(app) as client:
        result=client.post('/api/reply',json=payload)
        assert result.status_code==200,result.text
        prepared=result.json()
        assert prepared['body']==body and prepared['delivery']=='awaiting_confirmation'
        assert client.post('/api/reply',json=payload).json()==prepared
        assert len(gloo.calls)==1
        facts=json.loads(gloo.calls[0]['input'])
        assert facts=={'approved_message':body,'exact_copy':True}
        with app.state.session_factory() as session:
            approval=session.get(m.Approval,prepared['approval_id'])
            assert approval.payload['body']==body and approval.payload['kind']=='ai'
            assert prepared['content_hash']==confirmations.digest(approval.payload)
            assert session.get(m.Notification,f'google-voice-gloo:{approval.id}').state=='composed'
            assert session.scalar(select(m.Message)) is None
        approved=client.post(f"/api/proposals/{prepared['approval_id']}/approve",json={'content_hash':prepared['content_hash']})
        assert approved.status_code==200,approved.text
        with app.state.session_factory() as session:
            message=session.scalar(select(m.Message))
            assert message.body==body and message.status=='queued'
        assert len(gloo.calls)==1
        assert client.post('/mac/outbound/pull',headers=headers,json={}).json()['messages'][0]['body']==body


@pytest.mark.parametrize('failure',['outage','empty','changed','whitespace','emdash','missing'])
def test_failure_has_no_echo_fallback_or_review_and_retry_can_prepare(mode_app,failure):
    app,volunteers,_=mode_app;normal_mode(app)
    original=' Exact words. '
    outcomes={'empty':'','changed':'Different words','whitespace':original.strip(),'emdash':'Exact—words.'}
    app.state.gloo=None if failure=='missing' else ExactGloo(outage=failure=='outage',result=outcomes.get(failure))
    payload=request(volunteers[0],original)
    with TestClient(app) as client:
        response=client.post('/api/reply',json=payload)
        assert response.status_code==503 and 'exact reply' in response.json()['detail']
        no_outbound(app)
        app.state.gloo=ExactGloo()
        assert client.post('/api/reply',json=payload).status_code==200


@pytest.mark.parametrize('dash',sorted(EM_DASH_CHARACTERS))
def test_bad_typed_punctuation_is_rejected_before_gloo(mode_app,dash):
    app,volunteers,_=mode_app;sign_in_fixture(app)
    gloo=app.state.gloo=ExactGloo()
    with TestClient(app) as client:
        assert client.post('/api/reply',json=request(volunteers[0],'Before'+dash+'after')).status_code==409
    assert not gloo.calls;no_outbound(app)


@pytest.mark.parametrize('guard',['auth','sensitive_body','closed_private_source','quiet','phone_opt_out','care'])
def test_prechecks_prevent_any_model_export_or_review(mode_app,guard):
    app,volunteers,_=mode_app;gloo=app.state.gloo=ExactGloo()
    if guard!='auth':sign_in_fixture(app)
    body='Exact words.'
    with app.state.session_factory() as session:
        session.info['record_authorized']=True
        volunteer=session.get(m.Volunteer,volunteers[0].id)
        if guard=='quiet':session.add(m.Policy(key='quiet_hours',value={'value':{'start':'09:00','end':'11:00'}}))
        if guard=='phone_opt_out':session.add(m.Policy(key='sms_opt_out:'+volunteer.phone,value={'value':True}))
        if guard=='care':session.add(m.Escalation(category='sensitive',severity='normal',status='open',summary='Internal care',related_ids={'volunteer_id':volunteer.id},created_at=app.state.clock.now()))
        if guard=='closed_private_source':
            body='A synthetic private family matter.'
            source=m.Message(direction='in',volunteer_id=volunteer.id,phone=volunteer.phone,body=body,status='received',kind='inbound',created_at=app.state.clock.now())
            session.add(source);session.flush()
            session.add(m.Escalation(category='sensitive',severity='normal',status='resolved',summary='Internal care',related_ids={'message_id':source.id},created_at=app.state.clock.now()))
        session.commit()
    if guard=='sensitive_body':body='I have cancer.'
    with TestClient(app) as client:
        response=client.post('/api/reply',json=request(volunteers[0],body))
        assert response.status_code in {401,409}
        if guard=='quiet':assert 'quiet hours' in response.json()['detail']
    assert not gloo.calls;no_outbound(app)


@pytest.mark.parametrize('state',['pending','queued','dispatching'])
def test_legacy_unprepared_manual_review_or_queue_cannot_deliver(mode_app,state):
    app,volunteers,headers=mode_app;sign_in_fixture(app)
    gloo=app.state.gloo=ExactGloo()
    with app.state.session_factory() as session:
        volunteer=session.get(m.Volunteer,volunteers[0].id)
        selected=app.state.provider.test_sessions[volunteer.phone]
        approval=confirmations.stage(session,app.state.clock.now(),{'action':'send_text','phone':volunteer.phone,'volunteer_id':volunteer.id,'body':'Legacy unprepared words.','purpose':'manual','kind':'template','transport':'mac_messages','session_id':selected.id,'session_starts_at':selected.starts_at.isoformat()})
        if state!='pending':
            approval.status='approved'
            row=m.Message(direction='out',volunteer_id=volunteer.id,phone=volunteer.phone,body=approval.payload['body'],purpose='manual',kind='template',status=state,provider_sid=selected.outbound_prefix+'legacy',created_at=app.state.clock.now())
            session.add(row);session.flush()
            approval.payload={**approval.payload,'message_id':row.id}
            session.add(m.Notification(key=f'confirmation:{row.id}',purpose='human_review',body='',state='sent',due_at=app.state.clock.now(),created_at=app.state.clock.now(),message_id=row.id,detail={'approval_id':approval.id,'content_hash':approval.payload['content_hash']}))
            if state=='dispatching':session.add(MacDeliveryClaim(message_id=row.id,token='c'*64))
            message_id=row.id
        approval_id,digest=approval.id,approval.payload['content_hash'];session.commit()
    with TestClient(app) as client:
        if state=='pending':
            response=client.post(f'/api/proposals/{approval_id}/approve',json={'content_hash':digest})
            assert response.status_code==200,response.text
            assert response.json()['delivery']!='queued_for_mac'
        elif state=='queued':assert client.post('/mac/outbound/pull',headers=headers,json={}).json()['messages']==[]
        else:assert client.post(f'/mac/outbound/{message_id}/verify',headers=headers,json={'token':'c'*64,'content_hash':digest}).status_code==409
    assert not gloo.calls
    with app.state.session_factory() as session:
        assert session.scalar(select(m.Message).where(m.Message.status.in_(('sent','submitted')))) is None


def test_changed_request_and_legacy_retry_receipt_cannot_recompose_or_return_unprepared_review(mode_app):
    app,volunteers,_=mode_app;normal_mode(app)
    gloo=app.state.gloo=ExactGloo()
    payload=request(volunteers[0])
    with TestClient(app) as client:
        assert client.post('/api/reply',json=payload).status_code==200
        assert client.post('/api/reply',json={**payload,'body':'Changed words.'}).status_code==409
        with app.state.session_factory() as session:
            receipt=session.get(m.Notification,f"admin-compose:{volunteers[0].id}:{payload['request_id']}")
            approval_id=receipt.detail['result']['approval_id']
            session.delete(session.get(m.Notification,f'google-voice-gloo:{approval_id}'))
            session.commit()
        assert client.post('/api/reply',json=payload).status_code==409
    assert len(gloo.calls)==1


def test_replayed_draft_from_different_recipient_session_is_held_without_model(mode_app):
    from tests.session_fixtures import session_specs
    from app.integrations.test_sessions import parse_sessions
    app,volunteers,_=mode_app;normal_mode(app)
    gloo=app.state.gloo=ExactGloo()
    payload=request(volunteers[0])
    with TestClient(app) as client:
        assert client.post('/api/reply',json=payload).status_code==200
        import json
        specs=session_specs([v.phone for v in volunteers],app.state.clock.now())
        specs[volunteers[0].phone]['id']=uuid4().hex
        app.state.provider.test_sessions=parse_sessions(json.dumps(specs),app.state.provider.phones)
        assert client.post('/api/reply',json=payload).status_code==409
    assert len(gloo.calls)==1


@pytest.mark.parametrize('change',['expired','rejected','stop','quiet','same_id_new_start'])
def test_cached_review_freshness_and_current_guards_hold_without_another_model(mode_app,change):
    from dataclasses import replace
    app,volunteers,_=mode_app;normal_mode(app)
    gloo=app.state.gloo=ExactGloo()
    payload=request(volunteers[0])
    with TestClient(app) as client:
        prepared=client.post('/api/reply',json=payload).json()
        with app.state.session_factory() as session:
            approval=session.get(m.Approval,prepared['approval_id'])
            if change=='expired':
                approval.payload={**approval.payload,'expires_at':(app.state.clock.now()-timedelta(seconds=1)).isoformat()}
                approval.payload={**approval.payload,'content_hash':confirmations.digest(approval.payload)}
            if change=='rejected':approval.status='rejected'
            if change=='stop':session.add(m.Policy(key='sms_opt_out:'+volunteers[0].phone,value={'value':True}))
            if change=='quiet':session.add(m.Policy(key='quiet_hours',value={'value':{'start':'09:00','end':'11:00'}}))
            session.commit()
        if change=='same_id_new_start':
            old=app.state.provider.test_sessions[volunteers[0].phone]
            app.state.provider.test_sessions[volunteers[0].phone]=replace(old,starts_at=old.starts_at+timedelta(seconds=30))
        assert client.post('/api/reply',json=payload).status_code==409
        if change=='same_id_new_start':
            approved=client.post(f"/api/proposals/{prepared['approval_id']}/approve",json={'content_hash':prepared['content_hash']})
            assert approved.status_code==200 and approved.json()['delivery']!='queued_for_mac'
    assert len(gloo.calls)==1
    with app.state.session_factory() as session:assert session.scalar(select(m.Message)) is None


def test_approved_request_replay_returns_existing_receipt_without_resend_or_model(mode_app):
    app,volunteers,_=mode_app;normal_mode(app)
    gloo=app.state.gloo=ExactGloo()
    payload=request(volunteers[0])
    with TestClient(app) as client:
        prepared=client.post('/api/reply',json=payload).json()
        assert client.post(f"/api/proposals/{prepared['approval_id']}/approve",json={'content_hash':prepared['content_hash']}).status_code==200
        replay=client.post('/api/reply',json=payload)
        assert replay.status_code==200 and replay.json()['delivery']=='queued_for_mac'
        assert replay.json()['body']==payload['body']
    assert len(gloo.calls)==1
    with app.state.session_factory() as session:assert len(session.scalars(select(m.Message)).all())==1
