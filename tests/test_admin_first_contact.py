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


from tests.test_google_voice_demo import dynamic_demo, register
from tests.test_google_voice_signup import SignupGloo, PHONE as SIGNUP_PHONE


@pytest.fixture
def linked_invitation_admin(dynamic_demo,monkeypatch):
    """Run actual bounded signup paths with synthetic Gloo/native services."""
    app=dynamic_demo
    app.dependency_overrides[admin]=lambda:{'id':OWNER_A,'email':'owner@example.test','email_confirmed_at':'synthetic'}
    monkeypatch.setattr('app.web.admin_setup.now',app.state.clock.now)
    app.state.gloo=SignupGloo(app.state.settings)
    with TestClient(app) as client:
        assert save(client,complete=True).status_code==200
        assert register(app,SIGNUP_PHONE).status_code==200
        composed=client.post('/api/cloud-texting/demo/compose',json={'phone':SIGNUP_PHONE,'instruction':'Invite them to join.'})
        assert composed.status_code==200,composed.text
        pending=next(row for row in composed.json()['pending_reviews'] if row['phone']==SIGNUP_PHONE)
        assert pending['body'].endswith(NOTICE)
        approved=client.post('/api/proposals/'+str(pending['id'])+'/approve',json={'content_hash':pending['content_hash']})
        assert approved.status_code==200,approved.text
        assert client.post('/api/cloud-texting/demo/intake',json={}).status_code==200
        queued=next(row for row in client.get('/api/cloud-texting').json()['reviewed_messages'] if row['phone']==SIGNUP_PHONE)
        sent=client.post('/api/cloud-texting/demo/dispatch',json={'message_id':queued['id'],'body_hash':queued['body_hash']})
        assert sent.status_code==200 and sent.json()['step_result']['status']=='submitted',sent.text
        with app.state.session_factory() as session:
            invitation=session.get(m.Message,queued['id'])
            assert invitation.volunteer_id is None
            assert not session.scalar(select(m.Volunteer).where(m.Volunteer.phone==SIGNUP_PHONE))
        app.state.clock.advance(timedelta(seconds=1))
        app.state.google_voice_connector.messages=[{'id':'new-admin-name','phone':SIGNUP_PHONE,
            'body':'Judge Example','received_at':app.state.clock.now().isoformat()}]
        app.state.google_voice_connector.cursor=1
        incoming=client.post('/api/cloud-texting/demo/intake',json={})
        assert incoming.status_code==200,incoming.text
        with app.state.session_factory() as session:
            from app.integrations.google_voice_demo import registered_consent_provenance
            person=session.scalar(select(m.Volunteer).where(m.Volunteer.phone==SIGNUP_PHONE))
            assert person and registered_consent_provenance(session,person)
            assert person.preferences['consent_disclosure_message_id']==queued['id']
            person_id=person.id
        proof=client.post('/api/setup/admin-texts/review',json={'phone':SIGNUP_PHONE}).json()
        claimed=client.post('/api/setup/admin-texts',json={'phone':SIGNUP_PHONE,'enabled':True,'consent':False,
            'operator_consent':True,**{key:proof[key] for key in ('review_id','record_hash','primary_hash')}})
        assert claimed.status_code==200,claimed.text
        yield client,app,person_id,queued['id']


def test_genuine_null_roster_invitation_suppresses_repeated_admin_notice(linked_invitation_admin):
    client,app,person_id,invitation_id=linked_invitation_admin
    before=len(app.state.google_voice_connector.calls)
    assert check(client)['status']=='awaiting_approval'
    _,payload=approval(app)
    assert payload['body']==BASE and payload['conversation']['admin_check']['include_notice'] is False
    assert len(app.state.google_voice_connector.calls)==before==1
    with app.state.session_factory() as session:
        assert session.get(m.Message,invitation_id).volunteer_id is None
        assert session.get(m.Volunteer,person_id).name=='Judge Example'


@pytest.mark.parametrize('change',['unlinked','wrong_link','name_receipt','original_reply','sender','claim','uncertain','kind','purpose'])
def test_null_roster_invitation_needs_exact_consent_name_and_native_link(linked_invitation_admin,change):
    from app.integrations.google_voice_demo import RECIPIENT_KEY
    from app.integrations.google_voice_models import GoogleVoiceDeliveryClaim
    client,app,person_id,invitation_id=linked_invitation_admin
    before=len(app.state.google_voice_connector.calls)
    with app.state.session_factory() as session:
        session.info['record_authorized']=True
        person=session.get(m.Volunteer,person_id)
        proof=session.get(m.Policy,RECIPIENT_KEY+SIGNUP_PHONE).value['consent']
        if change=='unlinked':person.preferences={**person.preferences,'consent_disclosure_message_id':None}
        elif change=='wrong_link':person.preferences={**person.preferences,'consent_disclosure_message_id':invitation_id+1}
        elif change=='name_receipt':
            key='google-demo-name:'+proof['session_id']+':'+str(proof['reply_message_id'])+':first_name'
            session.get(m.Notification,key).state='revoked'
        elif change=='original_reply':session.get(m.Message,proof['reply_message_id']).body='Different Name'
        elif change=='sender':
            receipt=session.get(m.Notification,'google-demo-submission:'+str(invitation_id))
            receipt.detail={**receipt.detail,'sender_fingerprint':'0'*64}
        elif change=='claim':session.get(GoogleVoiceDeliveryClaim,invitation_id).idempotency_key='GVwrong-original-key'
        elif change=='uncertain':session.get(m.Message,invitation_id).status='uncertain'
        elif change=='kind':session.get(m.Message,invitation_id).kind='template'
        elif change=='purpose':session.get(m.Message,invitation_id).purpose='coordinator_notify'
        session.commit()
    result=check(client)
    if change=='uncertain':assert result['status']=='blocked_policy'
    else:
        assert result['status']=='awaiting_approval'
        _,payload=approval(app)
        assert payload['body']==BASE+' '+NOTICE and payload['conversation']['admin_check']['include_notice'] is True
    assert len(app.state.google_voice_connector.calls)==before==1
