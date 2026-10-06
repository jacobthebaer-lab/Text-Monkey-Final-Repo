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
    job = state.google_voice_signup_scheduler.get_job('registered_signup')
    assert job.trigger.interval == timedelta(seconds=5)
    assert job.max_instances == 1 and job.coalesce
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
        assert session.get(m.Policy,RECIPIENT_KEY+PHONE).value.get('invitation')
        assert session.get(m.Notification,'google-signup-unsent-renewal:'+str(approval.id)) is None


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


@pytest.mark.parametrize('status', ['dispatching', 'uncertain'])
@pytest.mark.parametrize('restart', [False, True])
def test_manual_google_uncertainty_holds_signup_before_any_intake_or_composition(signup, status, restart):
    from app.integrations.google_voice_models import GoogleVoiceDeliveryClaim
    enable(signup);assert register(signup).status_code==200
    assert register(signup,'+12025550156').status_code==200
    with signup.state.session_factory() as session:
        row=m.Message(direction='out',phone=PHONE,body='Exact reviewed manual text.',kind='ai',
            purpose='manual',provider_sid='GV'+signup.state.provider.test_sessions[PHONE].id+':manual-unknown',
            status=status,created_at=signup.state.clock.now())
        session.add(row);session.flush();message_id=row.id
        session.add(GoogleVoiceDeliveryClaim(message_id=row.id,idempotency_key=row.provider_sid,
            created_at=signup.state.clock.now()))
        session.commit()
    if restart:
        stop_service(signup.state);start_service(signup.state)
    tick_signup(signup.state)
    assert signup_status(signup.state)['state']=='held'
    assert not signup_status(signup.state)['active']
    assert signup.state.google_voice_connector.scoped_checks==[]
    assert signup.state.google_voice_connector.calls==[]
    assert signup.state.gloo.calls==[]
    with signup.state.session_factory() as session:
        assert session.get(m.Message,message_id).status==status
        assert session.get(GoogleVoiceDeliveryClaim,message_id) is not None


@pytest.mark.parametrize('operation', ['pause', 'verify-profile'])
@pytest.mark.parametrize('queued', [False, True])
def test_unsent_initial_invitation_recomposes_after_pause_and_new_enable(signup, operation, queued):
    enable(signup);assert register(signup).status_code==200
    compose_demo_text(signup.state,EMAIL,PHONE,'Initial invitation')
    with signup.state.session_factory() as session:
        if queued:
            authorize_pending(session,signup.state);session.commit()
        original=dict(session.get(m.Policy,RECIPIENT_KEY+PHONE).value['invitation'])
        approval=session.get(m.Approval,original['approval_id'])
        old_message_id=approval.payload.get('message_id')
        old_epoch=session.get(m.Policy,KEY).value['id']
    client=TestClient(signup)
    path='/api/cloud-texting/pause' if operation=='pause' else '/api/cloud-texting/demo/verify-profile'
    assert client.post(path,json={'paused':True} if operation=='pause' else {}).status_code==200
    if not queued:
        signup.state.clock.advance(timedelta(hours=3))  # Expired draft, still never submitted.
    enable(signup);tick_signup(signup.state)
    assert len(signup.state.google_voice_connector.calls)==1
    assert signup.state.google_voice_connector.calls[0]['body']==WELCOME+' Text STOP to stop.'
    with signup.state.session_factory() as session:
        current=session.get(m.Policy,RECIPIENT_KEY+PHONE).value['invitation']
        assert current['approval_id']!=original['approval_id']
        assert current['body_hash']==original['body_hash']
        assert session.get(m.Approval,original['approval_id']).status=='expired'
        audit=session.get(m.Notification,'google-signup-unsent-renewal:'+str(original['approval_id']))
        assert audit.detail['invitation']==original
        assert audit.detail['session_id']==signup.state.provider.test_sessions[PHONE].id
        assert audit.detail['new_authorization_id']!=old_epoch
        if queued:
            assert session.get(m.Message,old_message_id).status=='superseded'
    assert len(signup.state.gloo.calls)>=2  # New actual Gloo composition, no copy mutation/fallback.


def test_submitted_initial_invitation_is_never_recomposed_after_reenable(signup):
    enable(signup);assert register(signup).status_code==200
    tick_signup(signup.state)
    with signup.state.session_factory() as session:
        original=dict(session.get(m.Policy,RECIPIENT_KEY+PHONE).value['invitation'])
    assert TestClient(signup).post('/api/cloud-texting/pause',json={'paused':True}).status_code==200
    enable(signup);tick_signup(signup.state)
    assert len(signup.state.google_voice_connector.calls)==1
    assert len(signup.state.gloo.calls)==1
    with signup.state.session_factory() as session:
        assert session.get(m.Policy,RECIPIENT_KEY+PHONE).value['invitation']==original
        assert session.get(m.Notification,'google-signup-unsent-renewal:'+str(original['approval_id'])) is None


@pytest.mark.parametrize('status', ['dispatching', 'uncertain'])
def test_reserved_initial_invitation_never_recovers_after_new_enable(signup, status):
    from app.integrations.google_voice_models import GoogleVoiceDeliveryClaim
    enable(signup);assert register(signup).status_code==200
    compose_demo_text(signup.state,EMAIL,PHONE,'Initial invitation')
    with signup.state.session_factory() as session:
        authorize_pending(session,signup.state);session.commit()
        original=dict(session.get(m.Policy,RECIPIENT_KEY+PHONE).value['invitation'])
        approval=session.get(m.Approval,original['approval_id'])
        row=session.get(m.Message,approval.payload['message_id']);row.status=status
        session.add(GoogleVoiceDeliveryClaim(message_id=row.id,idempotency_key=row.provider_sid,created_at=signup.state.clock.now()))
        session.commit()
    assert TestClient(signup).post('/api/cloud-texting/pause',json={'paused':True}).status_code==200
    enable(signup);tick_signup(signup.state)
    assert signup_status(signup.state)['state']=='held'
    assert signup.state.google_voice_connector.scoped_checks==[]
    assert signup.state.google_voice_connector.calls==[]
    assert len(signup.state.gloo.calls)==1
    with signup.state.session_factory() as session:
        assert session.get(m.Policy,RECIPIENT_KEY+PHONE).value['invitation']==original
        assert session.get(m.Notification,'google-signup-unsent-renewal:'+str(original['approval_id'])) is None


@pytest.mark.parametrize('tamper', ['hash', 'claim'])
def test_recomposition_skip_requires_original_hash_and_absence_of_submission_claim(signup, tamper):
    from app.integrations.google_voice_demo import demo_text_problem
    from app.integrations.google_voice_models import GoogleVoiceDeliveryClaim
    enable(signup);assert register(signup).status_code==200
    compose_demo_text(signup.state,EMAIL,PHONE,'Initial invitation')
    with signup.state.session_factory() as session:
        authorize_pending(session,signup.state);session.commit()
        original=dict(session.get(m.Policy,RECIPIENT_KEY+PHONE).value['invitation'])
        old_message_id=session.get(m.Approval,original['approval_id']).payload['message_id']
    assert TestClient(signup).post('/api/cloud-texting/pause',json={'paused':True}).status_code==200
    enable(signup);tick_signup(signup.state)
    with signup.state.session_factory() as session:
        current=session.get(m.Policy,RECIPIENT_KEY+PHONE).value['invitation']
        message=session.get(m.Message,session.get(m.Approval,current['approval_id']).payload['message_id'])
        assert demo_text_problem(session,signup.state.provider,PHONE,message.body,'signup_reply',signup.state.clock.now(),message=message) is None
        if tamper=='hash':
            audit=session.get(m.Notification,'google-signup-unsent-renewal:'+str(original['approval_id']))
            audit.detail={**audit.detail,'invitation':{**audit.detail['invitation'],'body_hash':'0'*64}}
        else:
            old=session.get(m.Message,old_message_id)
            session.add(GoogleVoiceDeliveryClaim(message_id=old.id,idempotency_key=old.provider_sid,created_at=signup.state.clock.now()))
        session.flush()
        assert demo_text_problem(session,signup.state.provider,PHONE,message.body,'signup_reply',signup.state.clock.now(),message=message)=='Initial demo invitation already queued or submitted'



def queue_onboarding_reply(signup, stage):
    enable(signup);assert register(signup).status_code==200
    tick_signup(signup.state)
    inbound(signup,'Judge Example','queued-name')
    if stage=='availability':
        tick_signup(signup.state)
        inbound(signup,'Greeter','queued-interest')
    assert TestClient(signup).post('/api/cloud-texting/demo/intake',json={}).status_code==200
    with signup.state.session_factory() as session:
        authorize_pending(session,signup.state);session.commit()
        row=session.scalar(select(m.Message).where(m.Message.phone==PHONE,m.Message.status=='queued',m.Message.purpose=='signup_reply'))
        assert row is not None
        person=session.scalar(select(m.Volunteer).where(m.Volunteer.phone==PHONE))
        assert person.sms_opt_in and person.preferences['onboarding_stage']==stage
        approval=confirmations.proof_for(session,row)
        return row.id,row.body,approval.id,approval.payload['reply_to_message_id']


@pytest.mark.parametrize('stage', ['interests', 'availability'])
@pytest.mark.parametrize('operation', ['pause', 'verify-profile'])
def test_unsubmitted_onboarding_question_recovers_under_fresh_authority(signup, stage, operation):
    message_id,original_body,approval_id,source_id=queue_onboarding_reply(signup,stage)
    before=len(signup.state.google_voice_connector.calls)
    gloo_before=len(signup.state.gloo.calls)
    client=TestClient(signup)
    path='/api/cloud-texting/pause' if operation=='pause' else '/api/cloud-texting/demo/verify-profile'
    assert client.post(path,json={'paused':True} if operation=='pause' else {}).status_code==200
    enable(signup);tick_signup(signup.state);tick_signup(signup.state)
    assert len(signup.state.google_voice_connector.calls)==before+1
    assert signup.state.google_voice_connector.calls[-1]['body']==original_body
    compositions=[json.loads(call['input']) for call in signup.state.gloo.calls[gloo_before:]]
    assert len(compositions)==1 and compositions[0]['signup_source']['message_id']==source_id
    with signup.state.session_factory() as session:
        original=session.get(m.Message,message_id)
        assert original.status=='superseded' and original.body==original_body
        audit=session.get(m.Notification,'google-signup-unsent-renewal:'+str(approval_id))
        assert audit.detail['invitation']['reply_to_message_id']==source_id
        current=session.scalar(select(m.Message).where(m.Message.phone==PHONE,m.Message.direction=='out',m.Message.body==original_body,m.Message.status=='submitted'))
        fresh=confirmations.proof_for(session,current)
        assert fresh.id!=approval_id and fresh.payload['reply_to_message_id']==source_id
        assert session.get(m.Notification,'google-signup-authority:'+str(fresh.id)).detail['authorization_id']==session.get(m.Policy,KEY).value['id']
    if stage=='availability':
        inbound(signup,'Sundays at9am twice a month','after-recovered-question');tick_signup(signup.state)
        assert len(signup.state.google_voice_connector.calls)==before+1  # Existing completion remains silent.
        with signup.state.session_factory() as session:
            person=session.scalar(select(m.Volunteer).where(m.Volunteer.phone==PHONE))
            assert person.preferences['onboarding_stage']=='complete'


@pytest.mark.parametrize('stage', ['interests', 'availability'])
def test_stop_cancels_unsubmitted_onboarding_recovery_before_gloo(signup, stage):
    message_id,original_body,approval_id,_source=queue_onboarding_reply(signup,stage)
    before=len(signup.state.google_voice_connector.calls);gloo_before=len(signup.state.gloo.calls)
    assert TestClient(signup).post('/api/cloud-texting/pause',json={'paused':True}).status_code==200
    enable(signup);inbound(signup,'STOP','cancel-before-recovery');tick_signup(signup.state)
    assert len(signup.state.google_voice_connector.calls)==before
    assert len(signup.state.gloo.calls)==gloo_before
    with signup.state.session_factory() as session:
        row=session.get(m.Message,message_id)
        assert row.status=='blocked_opt_out' and row.body==original_body
        assert session.get(m.Notification,'google-signup-unsent-renewal:'+str(approval_id)) is None
        assert session.get(m.Policy,'sms_opt_out:'+PHONE).value['value']


@pytest.mark.parametrize('status', ['dispatching', 'uncertain', 'submitted'])
def test_claimed_onboarding_question_cannot_recompose_after_reenable(signup, status):
    from app.integrations.google_voice_models import GoogleVoiceDeliveryClaim
    message_id,original_body,approval_id,_source=queue_onboarding_reply(signup,'interests')
    with signup.state.session_factory() as session:
        row=session.get(m.Message,message_id);row.status=status
        session.add(GoogleVoiceDeliveryClaim(message_id=row.id,idempotency_key=row.provider_sid,created_at=signup.state.clock.now()))
        session.commit()
    before=len(signup.state.google_voice_connector.calls);gloo_before=len(signup.state.gloo.calls)
    assert TestClient(signup).post('/api/cloud-texting/pause',json={'paused':True}).status_code==200
    enable(signup);tick_signup(signup.state);tick_signup(signup.state)
    assert len(signup.state.google_voice_connector.calls)==before
    assert len(signup.state.gloo.calls)==gloo_before
    with signup.state.session_factory() as session:
        assert session.get(m.Message,message_id).body==original_body
        assert session.get(m.Notification,'google-signup-unsent-renewal:'+str(approval_id)) is None


def test_expired_partial_name_question_recovery_keeps_original_sender_evidence(signup):
    enable(signup);assert register(signup).status_code==200
    tick_signup(signup.state);inbound(signup,'My name is Judge','partial-before-pause')
    assert TestClient(signup).post('/api/cloud-texting/demo/intake',json={}).status_code==200
    with signup.state.session_factory() as session:
        authorize_pending(session,signup.state);session.commit()
        row=session.scalar(select(m.Message).where(m.Message.phone==PHONE,m.Message.status=='queued'))
        message_id=row.id;original_body=row.body
        source_id=confirmations.proof_for(session,row).payload['reply_to_message_id']
    assert TestClient(signup).post('/api/cloud-texting/pause',json={'paused':True}).status_code==200
    signup.state.clock.advance(timedelta(days=1));enable(signup);tick_signup(signup.state)
    assert len(signup.state.google_voice_connector.calls)==2
    assert signup.state.google_voice_connector.calls[-1]['body']==original_body and 'STOP' not in original_body
    recovered=json.loads(signup.state.gloo.calls[-1]['input'])
    assert recovered['signup_source']['message_id']==source_id
    assert recovered['signup_source']['body']=='My name is Judge'
    inbound(signup,'My last name is Example','last-after-recovery');tick_signup(signup.state)
    with signup.state.session_factory() as session:
        assert session.get(m.Message,message_id).status=='superseded'
        person=session.scalar(select(m.Volunteer).where(m.Volunteer.phone==PHONE))
        assert person.sms_opt_in and person.name=='Judge Example'


def test_changed_original_sender_scope_cannot_authorize_onboarding_recomposition(signup):
    _message,_body,approval_id,source_id=queue_onboarding_reply(signup,'interests')
    before=len(signup.state.google_voice_connector.calls);gloo_before=len(signup.state.gloo.calls)
    with signup.state.session_factory() as session:
        session.get(m.Message,source_id).phone='+12025550188';session.commit()
    assert TestClient(signup).post('/api/cloud-texting/pause',json={'paused':True}).status_code==200
    enable(signup);tick_signup(signup.state)
    assert len(signup.state.google_voice_connector.calls)==before and len(signup.state.gloo.calls)==gloo_before
    with signup.state.session_factory() as session:
        assert session.get(m.Notification,'google-signup-unsent-renewal:'+str(approval_id)) is None



def test_existing_signup_privacy_hold_prevents_recomposition_under_new_epoch(signup):
    _message,_body,approval_id,_source=queue_onboarding_reply(signup,'interests')
    before=len(signup.state.google_voice_connector.calls);gloo_before=len(signup.state.gloo.calls)
    with signup.state.session_factory() as session:
        session.add(m.Escalation(category='privacy',severity='normal',summary='Synthetic signup privacy hold',
            related_ids={'phone':PHONE},status='open',created_at=signup.state.clock.now()))
        session.commit()
    assert TestClient(signup).post('/api/cloud-texting/pause',json={'paused':True}).status_code==200
    enable(signup);tick_signup(signup.state)
    assert len(signup.state.google_voice_connector.calls)==before and len(signup.state.gloo.calls)==gloo_before
    with signup.state.session_factory() as session:
        assert session.get(m.Notification,'google-signup-unsent-renewal:'+str(approval_id)) is None
