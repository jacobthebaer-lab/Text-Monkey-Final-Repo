"""Synthetic exact admin enrollment; no accounts, network, Gloo or delivery."""
from datetime import timedelta
from dataclasses import replace
from sqlalchemy import select
import pytest

from app.admin_setup.models import Workspace
from app.db import models as m
from app.core.admin_text_enrollment import google_consent_problem
from app.integrations.google_voice_demo import demo_text_problem
from app.sms.google_voice_provider import GoogleVoiceProvider, GoogleVoiceTestSession
from tests.test_admin_setup import setup_client, save, OWNER_A, OWNER_B
from tests.test_admin_text_settings import enable

PHONE = '+12025550198'


@pytest.fixture
def enrolled_fixture(setup_client):
    client, app, user = setup_client
    assert save(client, complete=True).status_code == 200
    assert enable(client).status_code == 200
    with app.state.session_factory() as session:
        session.info['record_authorized'] = True
        target = m.Volunteer(name='Casey Contact', phone=PHONE, status='active', sms_opt_in=True,
            preferences={'interested_roles':['Camera'],'unrelated':'keep'}, created_at=app.state.clock.now())
        unrelated = m.Volunteer(name='Jordan Volunteer', phone='+12025550197', status='active', sms_opt_in=True,
            preferences={'interested_roles':['Greeter'],'max_per_month':2}, created_at=app.state.clock.now())
        session.add_all([target, unrelated]); session.flush()
        session.add(m.Qualification(volunteer_id=target.id, type='training', status='verified'))
        session.commit()
    return client, app, user


def review(client):
    response = client.post('/api/setup/admin-texts/review', json={'phone':PHONE})
    assert response.status_code == 200, response.text
    return response.json()


def claim(client, proof, **changes):
    return client.post('/api/setup/admin-texts', json={'phone':PHONE,'enabled':True,'consent':False,
        'operator_consent':True, **{key:proof[key] for key in ('review_id','record_hash','primary_hash')}, **changes})


def test_reviewed_replacement_preserves_existing_record_and_other_volunteer(enrolled_fixture):
    client, app, _ = enrolled_fixture
    proof = review(client)
    assert proof['recipient']['name'] == 'Casey Contact'
    assert proof['replacing'][0]['phone'] == '+12025550199'
    assert claim(client, proof).status_code == 200
    with app.state.session_factory() as session:
        target = session.get(m.Volunteer, proof['recipient']['id'])
        primary = session.scalar(select(m.Volunteer).where(m.Volunteer.phone == '+12025550199'))
        unrelated = session.scalar(select(m.Volunteer).where(m.Volunteer.phone == '+12025550197'))
        assert target.name == 'Casey Contact' and target.is_coordinator and target.sms_opt_in
        assert target.preferences['interested_roles'] == ['Camera'] and target.preferences['unrelated'] == 'keep'
        assert target.preferences['admin_text_owner'] == OWNER_A
        assert target.preferences['admin_text_consent_mode'] == 'operator_attested'
        consent = session.get(m.Notification, target.preferences['admin_text_consent_key'])
        assert consent.detail['mode'] == 'operator_attested' and consent.detail['review_id'] == proof['review_id']
        assert target.qualifications[0].status == 'verified'
        assert primary.status == 'inactive' and primary.sms_opt_in
        assert unrelated.status == 'active' and not unrelated.is_coordinator
        assert unrelated.preferences == {'interested_roles':['Greeter'],'max_per_month':2}
        workspace = session.scalar(select(Workspace).where(Workspace.owner_id == OWNER_A))
        assert workspace.details['coordinator_phone'] == PHONE and workspace.details['coordinator_name'] == target.name
        assert session.scalars(select(m.Message)).all() == []
        assert session.scalars(select(m.Policy)).all() == []
    assert claim(client, proof).status_code == 409  # Consumed review cannot silently reenroll.


@pytest.mark.parametrize('change', ['name','preferences','qualification','primary','owner','STOP','hash','consent','mixed_consent'])
def test_changed_or_unconsented_review_never_claims_a_record(enrolled_fixture, change):
    client, app, _ = enrolled_fixture
    proof = review(client)
    overrides = {}
    with app.state.session_factory() as session:
        session.info['record_authorized'] = True
        target = session.get(m.Volunteer, proof['recipient']['id'])
        if change == 'name': target.name = 'Changed Person'
        elif change == 'preferences': target.preferences = {**target.preferences,'unrelated':'changed'}
        elif change == 'qualification': target.qualifications[0].status = 'expired'
        elif change == 'owner': target.preferences = {**target.preferences,'admin_text_owner':OWNER_B}
        elif change == 'STOP': session.add(m.Policy(key='sms_opt_out:'+PHONE,value={'value':True}))
        elif change == 'primary':
            workspace = session.scalar(select(Workspace).where(Workspace.owner_id == OWNER_A)); workspace.revision += 1
        elif change == 'hash': overrides['record_hash'] = 'f'*64
        elif change == 'consent': overrides['operator_consent'] = False
        elif change == 'mixed_consent': overrides['consent'] = True
        session.commit()
    assert claim(client, proof, **overrides).status_code == 409
    with app.state.session_factory() as session:
        target = session.get(m.Volunteer, proof['recipient']['id'])
        assert not target.preferences.get('admin_text_consent_key') and not target.is_coordinator
        assert session.scalar(select(m.Volunteer).where(m.Volunteer.phone == '+12025550199')).status == 'active'
        assert session.get(m.Notification, proof['review_id']).state == 'awaiting_review'


def test_expired_foreign_owner_and_unreviewed_claims_are_held(enrolled_fixture, monkeypatch):
    client, app, user = enrolled_fixture
    assert enable(client,phone=PHONE).status_code == 409
    proof = review(client)
    with app.state.session_factory() as session:
        expires = session.get(m.Notification, proof['review_id']).expires_at
    from app.web import admin_setup
    monkeypatch.setattr(admin_setup,'now',lambda:expires)
    assert claim(client, proof).status_code == 409
    monkeypatch.undo()
    user['id'] = OWNER_B
    assert save(client,complete=True).status_code == 200
    assert claim(client,proof).status_code == 409
    user['id'] = OWNER_A
    assert claim(client,proof,owner_id=OWNER_B).status_code == 422
    with app.state.session_factory() as session:
        session.info['record_authorized'] = True
        target = session.get(m.Volunteer,proof['recipient']['id'])
        target.preferences={**target.preferences,'admin_text_owner':OWNER_B};session.commit()
    assert client.post('/api/setup/admin-texts/review',json={'phone':PHONE}).status_code == 409


def test_google_admin_consent_is_narrow_and_current_not_volunteer_signup(enrolled_fixture, monkeypatch):
    client, app, _ = enrolled_fixture
    # Initialize provider without demo scope config validation; no connector is called.
    from app.config import Settings
    provider = GoogleVoiceProvider(Settings(sms_provider='google_voice',google_voice_enabled=False))
    start=app.state.clock.now();selected=GoogleVoiceTestSession('a'*32,start,start+timedelta(hours=2))
    provider.phones=frozenset([PHONE]);provider.test_sessions={PHONE:selected}
    provider.settings=replace(provider.settings,google_voice_demo_mode=True,
        google_voice_expected_email='sender@example.test',google_voice_expected_number='+12025550101')
    app.state.provider=provider;app.state.settings=provider.settings
    monkeypatch.setattr('app.web.admin_setup.now', lambda:start)
    proof=review(client)
    assert claim(client,proof).status_code==200
    with app.state.session_factory() as session:
        target=session.get(m.Volunteer,proof['recipient']['id'])
        assert google_consent_problem(session,provider,target,start) is None
        for purpose in ('coordinator_notify','escalation_notify','admin_reply'):
            assert demo_text_problem(session,provider,PHONE,'Synthetic reviewed admin body',purpose,start) is None
        for purpose in ('signup_reply','manual','reminder','confirmation','outreach'):
            assert demo_text_problem(session,provider,PHONE,'Synthetic body',purpose,start)
        provider.test_sessions[PHONE]=replace(selected,id='b'*32)
        assert google_consent_problem(session,provider,target,start)
        renewed=review(client)
        assert renewed['replacing']==[]
        assert claim(client,renewed).status_code==200
        session.expire_all()
        target=session.get(m.Volunteer,proof['recipient']['id'])
        assert google_consent_problem(session,provider,target,start) is None
        proof=renewed
        audit = session.get(m.Notification,target.preferences['admin_text_consent_key'])
        original = dict(audit.detail)
        for field in ('owner_id','recipient_id','phone','name','mode','consent_at','sender_fingerprint','session','purposes','review_hash'):
            audit.detail = {**original,field:None}
            assert google_consent_problem(session,provider,target,start), field
        audit.detail = original
        reviewed = session.get(m.Notification,proof['review_id'])
        old_review = dict(reviewed.detail)
        reviewed.detail = {**old_review,'record_hash':'f'*64}
        assert google_consent_problem(session,provider,target,start)
        reviewed.detail=old_review
        session.add(m.Message(direction='out',phone=PHONE,body='Synthetic previous body',kind='ai',
            purpose='coordinator_notify',status='uncertain',provider_sid='GVsynthetic-prior',created_at=start))
        session.info['record_authorized']=True
        session.flush()
        assert 'uncertain' in demo_text_problem(session,provider,PHONE,'Synthetic body','coordinator_notify',start)
        session.add(m.Policy(key='sms_opt_out:'+PHONE,value={'value':True}))
        assert google_consent_problem(session,provider,target,start)


def test_review_receipt_never_enters_notification_delivery(enrolled_fixture):
    from app.agents.fill_agent import FillContext
    from app.clock import FakeClock
    from app.core.notifications import flush_due
    client,app,_=enrolled_fixture
    proof=review(client)
    with app.state.session_factory() as session:
        receipt=session.get(m.Notification,proof['review_id'])
        clock=FakeClock(receipt.created_at+timedelta(minutes=1))
        assert flush_due(FillContext(session,clock,app.state.provider,app.state.gloo))==0
        assert receipt.state=='awaiting_review' and not session.scalar(select(m.Message))


def test_review_requires_real_authenticated_admin(enrolled_fixture):
    from app.web.texty import admin
    client,app,_=enrolled_fixture
    app.dependency_overrides.pop(admin)
    app.state.settings=replace(app.state.settings,supabase_url='https://auth.example.test',
        supabase_publishable_key='synthetic-public-key',admin_email_allowlist='admin@example.test')
    assert client.post('/api/setup/admin-texts/review',json={'phone':PHONE}).status_code==401


from tests.test_google_voice_demo import demo


def test_google_check_keeps_gloo_review_and_no_send_or_signup_side_effects(demo, monkeypatch):
    from fastapi.testclient import TestClient
    from uuid import uuid4
    from app.web.texty import admin
    from tests.test_google_voice import PHONE as scoped_phone
    from app.llm.gloo_client import GlooUnavailableError
    from tests.test_pre_event_updates import SyntheticGloo
    demo.dependency_overrides[admin]=lambda:{'id':OWNER_A,'email':'owner@example.test','email_confirmed_at':'synthetic'}
    monkeypatch.setattr('app.web.admin_setup.now',demo.state.clock.now)
    demo.state.gloo=SyntheticGloo()
    with demo.state.session_factory() as session:
        session.info['record_authorized']=True
        person=session.scalar(select(m.Volunteer).where(m.Volunteer.phone==scoped_phone))
        person.preferences={'unrelated':'preserve'};session.commit()
    with TestClient(demo) as client:
        assert save(client,complete=True).status_code==200
        proof=client.post('/api/setup/admin-texts/review',json={'phone':scoped_phone}).json()
        response=client.post('/api/setup/admin-texts',json={'phone':scoped_phone,'enabled':True,'consent':False,
            'operator_consent':True,**{key:proof[key] for key in ('review_id','record_hash','primary_hash')}})
        assert response.status_code==200,response.text
        request={'request_id':str(uuid4())}
        result=client.post('/api/setup/admin-texts/send-check',json=request)
        assert result.status_code==200,result.text
        assert result.json()['status']=='awaiting_approval',result.text
        assert demo.state.gloo.calls and not demo.state.google_voice_connector.calls
        with demo.state.session_factory() as session:
            notice=session.scalar(select(m.Notification).where(m.Notification.key.startswith('admin-check:')))
            approval=session.get(m.Approval,notice.detail['approval_id'])
            assert approval.payload['purpose']=='coordinator_notify' and approval.payload['phone']==scoped_phone
            from app.integrations.google_voice_demo import RECIPIENT_KEY
            assert not session.get(m.Policy,RECIPIENT_KEY+scoped_phone)
            assert session.scalar(select(m.Volunteer).where(m.Volunteer.phone==scoped_phone)).preferences['unrelated']=='preserve'
        class Unavailable(SyntheticGloo):
            def create_response(self,**kwargs):raise GlooUnavailableError('synthetic outage')
        demo.state.gloo=Unavailable()
        held=client.post('/api/setup/admin-texts/send-check',json={'request_id':str(uuid4())})
        assert held.status_code==200 and held.json()['status']=='pending'
        assert not demo.state.google_voice_connector.calls
