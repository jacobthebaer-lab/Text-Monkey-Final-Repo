"""Synthetic admin first-contact history, Gloo and immutable review checks."""
import hashlib
import json
from dataclasses import replace
from datetime import timedelta
from uuid import uuid4

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select

from app.core.admin_check_copy import BASE, NOTICE
from app.db import models as m
from app.web.texty import admin
from tests.test_admin_setup import OWNER_A, save
from tests.test_google_voice import PHONE
from tests.test_google_voice_demo import demo
from tests.test_pre_event_updates import SyntheticGloo


@pytest.fixture
def admin_cloud(demo, monkeypatch):
    demo.dependency_overrides[admin]=lambda:{'id':OWNER_A,'email':'owner@example.test','email_confirmed_at':'synthetic'}
    monkeypatch.setattr('app.web.admin_setup.now',demo.state.clock.now)
    demo.state.gloo=SyntheticGloo()
    with TestClient(demo) as client:
        assert save(client,complete=True).status_code==200
        proof=client.post('/api/setup/admin-texts/review',json={'phone':PHONE}).json()
        claimed=client.post('/api/setup/admin-texts',json={'phone':PHONE,'enabled':True,'consent':False,
            'operator_consent':True,**{key:proof[key] for key in ('review_id','record_hash','primary_hash')}})
        assert claimed.status_code==200,claimed.text
        yield client,demo,proof['recipient']['id']


def check(client):
    response=client.post('/api/setup/admin-texts/send-check',json={'request_id':str(uuid4())})
    assert response.status_code==200,response.text
    return response.json()


def approval(app):
    with app.state.session_factory() as session:
        row=session.scalar(select(m.Notification).where(m.Notification.key.startswith('admin-check:')).order_by(m.Notification.created_at.desc(),m.Notification.key.desc()))
        result=session.get(m.Approval,row.detail['approval_id'])
        return result.id,result.payload.copy()


def history(app, person_id, *, status='submitted', session_id=None, sender=None, phone=PHONE,
            native=True, owner=OWNER_A, body_hash=None, volunteer_id=None, claimed=True):
    from app.integrations.google_voice_demo import sender_fingerprint
    selected=app.state.provider.test_sessions[PHONE]
    with app.state.session_factory() as session:
        session.info['record_authorized']=True
        row=m.Message(direction='out',phone=phone,volunteer_id=person_id if volunteer_id is None else volunteer_id,
            body='Synthetic earlier contact',purpose='coordinator_notify',kind='ai',status=status,
            provider_sid='GV'+(session_id or selected.id)+':synthetic-'+uuid4().hex,
            created_at=app.state.clock.now()-timedelta(minutes=1))
        session.add(row);session.flush()
        if native:
            if claimed:
                from app.integrations.google_voice_models import GoogleVoiceDeliveryClaim
                session.add(GoogleVoiceDeliveryClaim(message_id=row.id,idempotency_key=row.provider_sid,created_at=row.created_at))
            session.add(m.Notification(key=f'google-demo-submission:{row.id}',purpose='human_review',state='submitted',
                body='',message_id=row.id,due_at=app.state.clock.now(),created_at=app.state.clock.now(),detail={
                    'session_id':session_id or selected.id,'sender_fingerprint':sender or sender_fingerprint(app.state.settings),
                    'body_hash':body_hash or hashlib.sha256(row.body.encode()).hexdigest()}))
            session.add(m.Notification(key=f'conversation-message:{row.id}',purpose='human_review',state='sent',
                body='',message_id=row.id,due_at=app.state.clock.now(),created_at=app.state.clock.now(),
                detail={'admin_check':{'owner_id':owner}}))
        session.commit()


@pytest.mark.parametrize('status',['draft','queued','dispatching','uncertain'])
def test_unsubmitted_rows_never_replace_initial_disclosure(admin_cloud,status):
    client,app,person=admin_cloud
    history(app,person,status=status)
    result=check(client)
    # Existing uncertainty guard blocks composition authority rather than resend.
    if status in {'dispatching','uncertain'}:
        assert result['status']=='blocked_policy'
        assert not app.state.google_voice_connector.calls
        return
    assert result['status']=='awaiting_approval'
    _,payload=approval(app)
    assert payload['body']==BASE+' '+NOTICE
    assert payload['conversation']['admin_check']['include_notice'] is True
    assert json.loads(app.state.gloo.calls[-1]['input'])['exact_copy'] is True
    assert not app.state.google_voice_connector.calls


@pytest.mark.parametrize('status',['submitted','sent','delivered'])
def test_reliable_same_sender_contact_survives_session_renewal_without_stop(admin_cloud,status):
    client,app,person=admin_cloud
    history(app,person,status=status,session_id='f'*32)
    assert check(client)['status']=='awaiting_approval'
    _,payload=approval(app)
    assert payload['body']==BASE and NOTICE not in payload['body']
    assert payload['conversation']['admin_check']['include_notice'] is False
    assert not app.state.google_voice_connector.calls


@pytest.mark.parametrize('change',['sender','phone','owner','unaudited','hash','record','unclaimed'])
def test_other_or_unreliable_history_cannot_suppress_first_stop(admin_cloud,change):
    client,app,person=admin_cloud
    options={
        'sender':{'sender':'0'*64},'phone':{'phone':'+12025550199'},'owner':{'owner':'different-owner'},
        'unaudited':{'native':False},'hash':{'body_hash':'0'*64},'record':{'volunteer_id':None},'unclaimed':{'claimed':False}}
    if change=='record':
        # An unrelated roster identity is not this recipient's church enrollment.
        with app.state.session_factory() as session:
            session.info['record_authorized']=True
            other=m.Volunteer(name='Other Person',phone='+12025550199',sms_opt_in=True,
                preferences={},created_at=app.state.clock.now());session.add(other);session.commit();other_id=other.id
        options['record']={'volunteer_id':other_id}
    history(app,person,**options[change])
    assert check(client)['status']=='awaiting_approval'
    _,payload=approval(app)
    assert payload['body']==BASE+' '+NOTICE


@pytest.mark.parametrize('invalid',['missing_stop','extra_stop','recurring_stop','em_dash','outage'])
def test_gloo_invalid_copy_and_outage_hold_without_fallback(admin_cloud,invalid):
    from types import SimpleNamespace
    from app.llm.gloo_client import GlooUnavailableError
    client,app,person=admin_cloud
    if invalid=='recurring_stop':history(app,person)
    class Invalid(SyntheticGloo):
        def create_response(self,**kwargs):
            self.calls.append(kwargs)
            if invalid=='outage':raise GlooUnavailableError('Synthetic outage')
            source=json.loads(kwargs['input'])['approved_message']
            return SimpleNamespace(output_text={'missing_stop':BASE,'extra_stop':source+' '+NOTICE,
                'recurring_stop':source+' '+NOTICE,
                'em_dash':source+' \u2014 invalid'}[invalid])
    app.state.gloo=Invalid()
    assert check(client)['status']=='pending'
    assert not app.state.google_voice_connector.calls
    with app.state.session_factory() as session:
        assert not session.scalar(select(m.Approval).where(m.Approval.kind=='confirm_text'))


def test_first_contact_change_after_review_holds_original_without_rewrite(admin_cloud):
    from app.core import confirmations
    client,app,person=admin_cloud
    assert check(client)['status']=='awaiting_approval'
    approval_id,original=approval(app)
    history(app,person)
    with app.state.session_factory() as session:
        selected=app.state.provider.test_sessions[PHONE]
        session.info['mac_test_session']=selected
        reviewed=session.get(m.Approval,approval_id);reviewed.status='approved'
        error=confirmations.delivery_problem(session,app.state.provider,reviewed,app.state.clock.now())
        assert 'fresh Gloo composition' in error
        assert reviewed.payload==original and reviewed.payload['body']==BASE+' '+NOTICE
    assert not app.state.google_voice_connector.calls
