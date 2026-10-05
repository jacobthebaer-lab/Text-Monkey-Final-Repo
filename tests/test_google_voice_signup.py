"""Offline continuous signup proofs; no real Gloo, profile or transport."""
import json
import hashlib
from dataclasses import replace
from datetime import timedelta
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select

from app.db import models as m
from app.core import confirmations
from app.core.signup_copy import WELCOME
from app.integrations.google_voice_demo import RECIPIENT_KEY, restore_demo_scope
from app.integrations.google_voice_signup import (KEY, tick_signup, signup_status, authorize_pending,
    start_service, stop_service)
from app.integrations.google_voice_runtime import dispatch_outbound
from app.llm.gloo_client import GlooUnavailableError
from app.sms.google_voice_provider import GoogleVoiceProvider
from app.web.google_voice import compose_demo_text
from tests.test_google_voice_demo import demo, dynamic_demo, RegisteredDemoConnector, register
from tests.test_google_voice import EMAIL, ExactGloo

PHONE = '+12025550155'


class SignupGloo(ExactGloo):
    def create_response(self, **kwargs):
        self.calls.append(kwargs)
        data = json.loads(kwargs['input'])
        if isinstance(data, dict) and 'operator_request' in data:
            return SimpleNamespace(output_text='An individually reviewed manual text.',usage=None)
        if isinstance(data, list):
            body = data[-1]['body']
            first, last = ('Judge', None) if body == 'My name is Judge' else (None, 'Example') if body == 'My last name is Example' else ('Judge', 'Example')
            return SimpleNamespace(output_text=json.dumps({'signup': True, 'first_name': first,
                'last_name': last, 'identity_reply': True}), usage=None)
        if 'recovery' in data:
            return SimpleNamespace(output_text=json.dumps({'stage': data['recovery']['stage'],
                'missing': data['recovery']['missing'], 'acknowledgment': '', 'question': data['approved_message']}), usage=None)
        if data.get('stage') == 'interests':
            return SimpleNamespace(output_text=json.dumps({'understood':True,'role_ids':[next(role['id'] for role in data['roles'] if role['name']=='Greeter')],'any_role':False}),usage=None)
        if data.get('stage') == 'availability':
            return SimpleNamespace(output_text=json.dumps({'understood':True,'availability_known':True,'frequency_known':True,
                'weekdays':[6],'preferred_services':['sun_9'],'all_day':False,'max_per_month':2,
                'available_dates':[],'unavailable_dates':[]}),usage=None)
        return SimpleNamespace(output_text=data['approved_message'], usage=None)


@pytest.fixture
def signup(dynamic_demo, monkeypatch):
    state = dynamic_demo.state
    state.settings = replace(state.settings, google_voice_signup_enabled=True)
    state.provider = GoogleVoiceProvider(state.settings)
    state.google_voice_connector = RegisteredDemoConnector(state)
    state.gloo = SignupGloo(state.settings)
    with state.session_factory() as session:
        session.info['record_authorized'] = True
        from app.core.signup_copy import ensure_exact_role_menu
        ensure_exact_role_menu(session)
        session.commit()
    state.google_voice_connector.verify_profile = state.google_voice_connector.health
    state.google_voice_connector.scoped_checks = []
    original = state.google_voice_connector.intake
    def intake(phone=None, *, phones=None):
        state.google_voice_connector.scoped_checks.append(phones)
        return original(phone)
    state.google_voice_connector.intake = intake
    monkeypatch.setattr('apscheduler.schedulers.background.BackgroundScheduler.start', lambda _s: None)
    yield dynamic_demo
    stop_service(state)


def enable(signup):
    response = TestClient(signup).post('/api/cloud-texting/signup/enable', json={'enabled': True})
    assert response.status_code == 200, response.text


def inbound(signup, body, guid):
    state = signup.state
    state.clock.advance(timedelta(seconds=1))
    state.google_voice_connector.messages = [{'id': guid, 'phone': PHONE, 'body': body,
        'received_at': state.clock.now().isoformat()}]
    state.google_voice_connector.cursor += 1


def test_continuous_registration_and_gloo_signup_survive_days_without_review_clicks(signup):
    state = signup.state
    start_service(state)
    assert not signup_status(state)['enabled']
    assert not getattr(state, 'google_voice_signup_scheduler', None)
    enable(signup)
    assert register(signup, PHONE).status_code == 200
    selected = state.provider.test_sessions[PHONE]
    assert selected.continuous and selected.active(state.clock.now() + timedelta(days=365))
    tick_signup(state)
    assert len(state.google_voice_connector.calls) == 1
    assert state.google_voice_connector.calls[0]['body'] == WELCOME + ' Text STOP to stop.'
    assert state.google_voice_connector.scoped_checks == [[PHONE]]
    with state.session_factory() as session:
        initial = session.scalar(select(m.Message).where(m.Message.phone == PHONE, m.Message.direction == 'out'))
        approval = confirmations.proof_for(session, initial)
        assert initial.status == 'submitted' and approval.via == 'signup_authorization'
        assert session.get(m.Notification, 'google-signup-authority:' + str(approval.id)).detail['operator'] == EMAIL
        assert session.scalar(select(m.Volunteer).where(m.Volunteer.phone == PHONE)) is None
    state.clock.advance(timedelta(days=3))
    inbound(signup, 'Judge Example', 'name-after-three-days')
    tick_signup(state)
    assert len(state.google_voice_connector.calls) == 2
    assert 'STOP' not in state.google_voice_connector.calls[-1]['body']
    assert 'Greeter' in state.google_voice_connector.calls[-1]['body']
    with state.session_factory() as session:
        person = session.scalar(select(m.Volunteer).where(m.Volunteer.phone == PHONE))
        assert person.sms_opt_in and person.name == 'Judge Example'
        assert session.get(m.Policy, RECIPIENT_KEY + PHONE).value['consent_state'] == 'name_reply_opted_in'
    before = len(state.google_voice_connector.calls)
    tick_signup(state)
    assert len(state.google_voice_connector.calls) == before
    state.provider = GoogleVoiceProvider(state.settings)
    restore_demo_scope(state)
    assert state.provider.test_sessions[PHONE].id == selected.id
    stop_service(state)
    start_service(state)
    assert signup_status(state)['active']  # Persisted operator authority resumes, no fresh window.
    assert state.gloo.calls


@pytest.mark.parametrize('replies', [('My name is Judge', 'My last name is Example'),
                                   ('My last name is Example', 'My name is Judge')])
def test_continuous_name_recovery_uses_actual_both_order_replies_and_first_only_disclosure(signup, replies):
    enable(signup)
    assert register(signup).status_code == 200
    tick_signup(signup.state)
    for index, body in enumerate(replies):
        signup.state.clock.advance(timedelta(days=1))
        inbound(signup, body, 'part-' + str(index))
        tick_signup(signup.state)
    assert len(signup.state.google_voice_connector.calls) == 3
    assert all('STOP' not in call['body'] for call in signup.state.google_voice_connector.calls[1:])
    with signup.state.session_factory() as session:
        person = session.scalar(select(m.Volunteer).where(m.Volunteer.phone == PHONE))
        assert person.name == 'Judge Example' and person.sms_opt_in
        proof = session.get(m.Policy, RECIPIENT_KEY + PHONE).value['consent']['name_evidence']
        assert proof['first_name']['reply_message_id'] != proof['last_name']['reply_message_id']


def test_continuous_stop_persists_and_registration_cannot_restart_invitation(signup):
    enable(signup)
    assert register(signup).status_code == 200
    tick_signup(signup.state)
    signup.state.clock.advance(timedelta(days=4))
    inbound(signup, 'STOP', 'continuous-stop')
    tick_signup(signup.state)
    assert register(signup).status_code == 409
    assert len(signup.state.google_voice_connector.calls) == 1
    with signup.state.session_factory() as session:
        assert session.get(m.Policy, 'sms_opt_out:' + PHONE).value['value']


@pytest.mark.parametrize('failure', ['gloo', 'unknown'])
def test_continuous_failures_hold_durably_without_fallback_or_restart_retry(signup, failure):
    enable(signup)
    assert register(signup).status_code == 200
    if failure == 'gloo':
        def unavailable(**_k):
            raise GlooUnavailableError('Synthetic private failure')
        signup.state.gloo.create_response = unavailable
    else:
        signup.state.google_voice_connector.outcome = RuntimeError('Synthetic unconfirmed submission')
    tick_signup(signup.state)
    assert signup_status(signup.state)['state'] == 'held'
    expected = 0 if failure == 'gloo' else 1
    assert len(signup.state.google_voice_connector.calls) == expected
    tick_signup(signup.state)
    start_service(signup.state)
    assert not signup_status(signup.state)['active']
    assert len(signup.state.google_voice_connector.calls) == expected
    with signup.state.session_factory() as session:
        assert session.scalar(select(m.Volunteer).where(m.Volunteer.phone == PHONE)) is None


def test_automatic_signup_authority_rechecks_before_browser_click(signup):
    enable(signup)
    assert register(signup).status_code == 200
    compose_demo_text(signup.state, EMAIL, PHONE, 'Initial invitation')
    with signup.state.session_factory() as session:
        authorize_pending(session, signup.state)
        session.commit()
        row = session.scalar(select(m.Message).where(m.Message.phone == PHONE, m.Message.direction == 'out'))
        message_id, body_hash = row.id, hashlib.sha256(row.body.encode()).hexdigest()
    original = signup.state.google_voice_connector.prepare
    def revoke(**kwargs):
        result = original(**kwargs)
        with signup.state.session_factory() as session:
            policy = session.get(m.Policy, KEY)
            policy.value = {**policy.value, 'enabled': False}
            session.commit()
        return result
    signup.state.google_voice_connector.prepare = revoke
    assert TestClient(signup).post('/api/cloud-texting/demo/intake', json={}).status_code == 200
    dispatch_outbound(signup.state, signup.state.google_voice_connector, message_id=message_id, expected_body_hash=body_hash)
    assert signup.state.google_voice_connector.calls == []
    with signup.state.session_factory() as session:
        assert session.get(m.Message, message_id).status == 'blocked_signup_authorization'


def test_continuous_signup_retains_quiet_hours(signup):
    enable(signup)
    assert register(signup).status_code == 200
    with signup.state.session_factory() as session:
        session.add(m.Policy(key='quiet_hours', value={'value': {'start': '00:00', 'end': '23:59'}}))
        session.commit()
    tick_signup(signup.state)
    assert signup.state.google_voice_connector.calls == []
    assert signup.state.gloo.calls == []
    assert signup_status(signup.state)['state'] == 'enabled'


def test_continuous_full_signup_and_manual_drafts_keep_separate_authority(signup):
    enable(signup)
    assert register(signup).status_code == 200
    tick_signup(signup.state)
    for body,guid in [('Judge Example','full-name'),('Greeter','role'),('Sundays at9am twice a month','availability')]:
        inbound(signup,body,guid);tick_signup(signup.state)
    assert len(signup.state.google_voice_connector.calls) == 3
    with signup.state.session_factory() as session:
        person=session.scalar(select(m.Volunteer).where(m.Volunteer.phone==PHONE))
        assert person.sms_opt_in and person.preferences['onboarding_stage']=='complete'
        assert person.preferences['interested_roles']==['Greeter'] and person.preferences['max_per_month']==2
    client=TestClient(signup)
    response=client.post('/api/cloud-texting/demo/compose',json={'phone':PHONE,'instruction':'Manual tailored text'})
    assert response.status_code==200,response.text
    manual=next(row for row in response.json()['pending_reviews'] if row['body']=='An individually reviewed manual text.')
    assert client.post('/api/proposals/'+str(manual['id'])+'/approve',json={'content_hash':manual['content_hash']}).status_code==200
    tick_signup(signup.state)
    assert len(signup.state.google_voice_connector.calls)==3
    with signup.state.session_factory() as session:
        row=session.scalar(select(m.Message).where(m.Message.phone==PHONE,m.Message.purpose=='manual'))
        assert row.status=='queued'
    inbound(signup,'My name is Other Person','unregistered')
    signup.state.google_voice_connector.messages[0]['phone']='+12025550188'
    tick_signup(signup.state)
    assert len(signup.state.google_voice_connector.calls)==3


def test_continuous_restart_holds_crash_claim_before_any_new_participant_scan(signup):
    from app.integrations.google_voice_models import GoogleVoiceDeliveryClaim
    enable(signup);assert register(signup).status_code==200
    compose_demo_text(signup.state,EMAIL,PHONE,'Initial invitation')
    with signup.state.session_factory() as session:
        authorize_pending(session,signup.state);session.commit()
        row=session.scalar(select(m.Message).where(m.Message.phone==PHONE,m.Message.direction=='out'))
        row.status='dispatching'
        session.add(GoogleVoiceDeliveryClaim(message_id=row.id,idempotency_key=row.provider_sid,created_at=signup.state.clock.now()))
        session.commit()
    assert register(signup,'+12025550156').status_code==200
    stop_service(signup.state);start_service(signup.state);tick_signup(signup.state)
    assert signup_status(signup.state)['state']=='held'
    assert signup.state.google_voice_connector.scoped_checks==[]
    assert signup.state.google_voice_connector.calls==[]


def test_continuous_stop_wins_before_pending_initial_authorization(signup):
    enable(signup);assert register(signup).status_code==200
    compose_demo_text(signup.state,EMAIL,PHONE,'Initial invitation')
    inbound(signup,'STOP','stop-before-invitation')
    before=len(signup.state.gloo.calls)
    tick_signup(signup.state)
    assert len(signup.state.gloo.calls)==before
    assert signup.state.google_voice_connector.calls==[]
    with signup.state.session_factory() as session:
        approval=session.scalar(select(m.Approval).where(m.Approval.payload['phone'].as_string()==PHONE))
        assert approval.status=='expired'
        assert session.get(m.Policy,'sms_opt_out:'+PHONE).value['value']


@pytest.mark.parametrize('operation', ['pause', 'verify-profile'])
def test_continuous_operator_pause_or_verification_holds_durable_worker(signup, operation):
    enable(signup);assert register(signup).status_code==200
    client=TestClient(signup)
    path='/api/cloud-texting/pause' if operation=='pause' else '/api/cloud-texting/demo/verify-profile'
    response=client.post(path,json={'paused':True} if operation=='pause' else {})
    assert response.status_code==200,response.text
    stop_service(signup.state);start_service(signup.state);tick_signup(signup.state)
    assert signup_status(signup.state)['state']=='held'
    assert not signup_status(signup.state)['active']
    assert signup.state.google_voice_connector.calls==[]


def test_new_enable_epoch_cannot_authorize_old_queued_signup_copy(signup):
    enable(signup);assert register(signup).status_code==200
    compose_demo_text(signup.state,EMAIL,PHONE,'Initial invitation')
    with signup.state.session_factory() as session:
        authorize_pending(session,signup.state);session.commit()
        row=session.scalar(select(m.Message).where(m.Message.phone==PHONE,m.Message.direction=='out'))
        message_id,body_hash=row.id,hashlib.sha256(row.body.encode()).hexdigest()
    assert TestClient(signup).post('/api/cloud-texting/signup/enable',json={'enabled':False}).status_code==200
    enable(signup)
    assert TestClient(signup).post('/api/cloud-texting/demo/intake',json={}).status_code==200
    dispatch_outbound(signup.state,signup.state.google_voice_connector,message_id=message_id,expected_body_hash=body_hash)
    assert signup.state.google_voice_connector.calls==[]
    with signup.state.session_factory() as session:
        assert session.get(m.Message,message_id).status=='blocked_signup_authorization'


def test_continuous_actual_reply_gloo_outage_holds_without_profile_or_text_fallback(signup):
    enable(signup);assert register(signup).status_code==200
    tick_signup(signup.state)
    def unavailable(**_kwargs):
        raise GlooUnavailableError('Synthetic unavailable reply interpretation')
    signup.state.gloo.create_response=unavailable
    inbound(signup,'Judge Example','unavailable-name')
    tick_signup(signup.state)
    assert signup_status(signup.state)['state']=='held'
    assert len(signup.state.google_voice_connector.calls)==1
    with signup.state.session_factory() as session:
        assert session.scalar(select(m.Volunteer).where(m.Volunteer.phone==PHONE)) is None
