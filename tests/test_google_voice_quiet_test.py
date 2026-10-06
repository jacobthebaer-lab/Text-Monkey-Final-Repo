"""Synthetic two-session quiet exception; no native or model requests."""
from dataclasses import replace
from datetime import datetime, timedelta, timezone
import hashlib
import json
import time

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select

from app.db import models as m
from app.core import confirmations
from app.core.policies import PolicyStore
from app.integrations.google_voice_quiet_test import KEY, EXPIRES_AT, grant, deadline
from app.integrations.google_voice_runtime import dispatch_outbound, _submission_deadline
from app.integrations.google_voice_signup import tick_signup
from app.sms.google_voice_provider import GoogleVoiceTestSession
from tests.test_google_voice_signup import signup, enable, PHONE
from tests.test_google_voice_demo import demo, dynamic_demo, register
from tests.test_google_voice import EMAIL, queued, status, PHONE as REVIEWED_PHONE

ADMIN_PHONE = '+12025550156'
START = datetime(2026, 10, 6, 3, 0, tzinfo=timezone.utc)


def setup(signup):
    state = signup.state
    state.clock.advance(START - state.clock.now())
    enable(signup)
    assert register(signup).status_code == 200
    state.provider.test_sessions[ADMIN_PHONE] = GoogleVoiceTestSession('c' * 32, START, EXPIRES_AT)
    state.settings = replace(state.settings, google_voice_demo_phones=ADMIN_PHONE,
        google_voice_test_sessions=json.dumps({ADMIN_PHONE:{'id':'c'*32,'starts_at':START.isoformat(),'expires_at':EXPIRES_AT.isoformat()}}))
    state.provider.settings = state.settings
    state.provider.phones = frozenset([*state.provider.phones, ADMIN_PHONE])
    state.google_voice_connector.sessions = dict(state.provider.test_sessions)
    return state


def create(signup):
    return TestClient(signup).post('/api/cloud-texting/demo/quiet-test',
        json={'signup_phone': PHONE, 'admin_phone': ADMIN_PHONE, 'confirmed': True})


def test_explicit_grant_runs_only_registered_signup_and_expiry_survives_restart(signup):
    state = setup(signup)
    assert register(signup, '+12025550157').status_code == 200
    tick_signup(state)
    assert state.gloo.calls == [] and state.google_voice_connector.calls == []
    response = create(signup)
    assert response.status_code == 200, response.text
    assert response.json()['quiet_hours_changed'] is False
    tick_signup(state)
    assert len(state.google_voice_connector.calls) == 1 and state.gloo.calls
    assert state.google_voice_connector.calls[0]['to'] == PHONE
    with state.session_factory() as session:
        saved = session.get(m.Policy, KEY).value
        assert saved['actor'] == EMAIL and saved['expires_at'] == EXPIRES_AT.isoformat()
        assert session.get(m.Notification, 'google-quiet-test:' + saved['id']).state == 'authorized'
        assert PolicyStore(session).get('quiet_hours') == {'start': '21:00', 'end': '07:00'}
        assert deadline(session, state.provider, PHONE, 'signup_reply', START) == EXPIRES_AT
    # Fresh sessions after process recreation still evaluate the absolute time.
    state.clock.advance(EXPIRES_AT - state.clock.now())
    with state.session_factory() as restarted:
        assert deadline(restarted, state.provider, PHONE, 'signup_reply', state.clock.now()) is None
    assert create(signup).status_code == 409


@pytest.mark.parametrize('change', ['phone','purpose','admin_as_signup','signup_as_admin','sender','session',
    'session_start','session_expiry','expired','future','bad_date','extended','revoked','altered_audit','transport'])
def test_grant_never_extends_to_wrong_or_changed_authority(signup, change):
    state = setup(signup)
    assert create(signup).status_code == 200
    phone, purpose, moment = PHONE, 'signup_reply', START
    with state.session_factory() as session:
        row = session.get(m.Policy, KEY)
        if change == 'phone': phone = '+12025550157'
        if change == 'purpose': purpose = 'outreach'
        if change == 'admin_as_signup': phone = ADMIN_PHONE
        if change == 'signup_as_admin': purpose = 'coordinator_notify'
        if change == 'sender': state.provider.settings = replace(state.provider.settings, google_voice_expected_number='+12025550158')
        if change == 'session': state.provider.test_sessions[PHONE] = replace(state.provider.test_sessions[PHONE], id='d'*32)
        if change == 'session_start': state.provider.test_sessions[PHONE] = replace(state.provider.test_sessions[PHONE], starts_at=START+timedelta(seconds=1))
        if change == 'session_expiry': state.provider.test_sessions[PHONE] = replace(state.provider.test_sessions[PHONE], expires_at=EXPIRES_AT-timedelta(seconds=1))
        if change == 'expired': moment = EXPIRES_AT
        if change == 'future': row.value = {**row.value,'at':(START+timedelta(seconds=1)).isoformat()}
        if change == 'bad_date': row.value = {**row.value,'at':'unparseable'}
        if change == 'extended': row.value = {**row.value,'expires_at':(EXPIRES_AT+timedelta(days=1)).isoformat()}
        if change == 'revoked': session.get(m.Notification, 'google-quiet-test:'+row.value['id']).state='revoked'
        if change == 'altered_audit': row.value = {**row.value,'actor':'different@example.test'}
        if change == 'transport': state.provider.transport_name='mock'
        session.flush()
        assert deadline(session, state.provider, phone, purpose, moment) is None


def reviewed_fixture(demo):
    state = demo.state
    # The historical synthetic fixture has a short session: bind a fresh exact
    # test session without touching the original sender/consent evidence.
    state.clock.advance(START-state.clock.now())
    selected = state.provider.test_sessions[REVIEWED_PHONE]
    state.provider.test_sessions[REVIEWED_PHONE] = replace(selected, starts_at=START-timedelta(minutes=1), expires_at=EXPIRES_AT)
    state.provider.test_sessions[ADMIN_PHONE] = GoogleVoiceTestSession('c'*32, START-timedelta(minutes=1), EXPIRES_AT)
    state.provider.phones = frozenset([REVIEWED_PHONE, ADMIN_PHONE])
    state.google_voice_connector.baseline='synthetic-baseline'
    with state.session_factory() as session:
        grant(state, session, EMAIL, ADMIN_PHONE, REVIEWED_PHONE, START)
        session.commit()
    # Fixture's reviewed queued helper uses coordinator notifications; this
    # phone is the explicit admin tester, with its historical consent intact.
    from app.core.send_gate import SendGate
    with state.session_factory() as session:
        session.info['record_authorized']=True
        person=session.scalar(select(m.Volunteer).where(m.Volunteer.phone==REVIEWED_PHONE))
        person.is_coordinator=True
        person.preferences={**person.preferences,'admin_text_owner':'synthetic-owner'}
        from app.core.admin_text_enrollment import record_consent
        record_consent(session,'synthetic-owner',person,state.provider,state.settings,START,mode='self_service')
        gate=SendGate(session,state.clock,state.provider);gate.gloo=state.gloo
        result=gate.send(body='Synthetic approved admin check',purpose='coordinator_notify',kind='ai',volunteer=person)
        assert result.approval_id,result
        approval=session.get(m.Approval,result.approval_id)
        confirmations.decide(session,gate,approval,approve=True,actor=EMAIL,
            expected=approval.payload['content_hash'],now=state.clock.now())
        assert approval.payload.get('message_id'),result
        identifier=approval.payload['message_id'];session.commit()
    state.google_voice_status = {'connected': True, 'checked_monotonic': time.monotonic()}
    return state, identifier


@pytest.mark.parametrize('boundary', ['expires','revoke','STOP','approval'])
def test_final_preclick_rechecks_exception_and_preserves_other_holds(demo, boundary):
    state, identifier = reviewed_fixture(demo)
    with state.session_factory() as session:
        row = session.get(m.Message, identifier)
        body_hash = hashlib.sha256(row.body.encode()).hexdigest()
        approval_id = confirmations.proof_for(session, row).id
    original_prepare = state.google_voice_connector.prepare
    def prepare(**request):
        outcome = original_prepare(**request)
        if boundary == 'expires':
            state.clock.advance(EXPIRES_AT-state.clock.now())
        else:
            with state.session_factory() as session:
                session.info['record_authorized']=True
                if boundary == 'revoke':
                    saved=session.get(m.Policy,KEY).value
                    session.get(m.Notification,'google-quiet-test:'+saved['id']).state='revoked'
                elif boundary == 'STOP':session.add(m.Policy(key='sms_opt_out:'+REVIEWED_PHONE,value={'value':True}))
                else:session.get(m.Approval,approval_id).status='rejected'
                session.commit()
        return outcome
    state.google_voice_connector.prepare=prepare
    dispatch_outbound(state,state.google_voice_connector,message_id=identifier,expected_body_hash=body_hash)
    assert state.google_voice_connector.preparations and not state.google_voice_connector.calls
    assert status(demo,identifier) != 'submitted'


def test_native_deadline_is_capped_at_absolute_exception_expiry(demo):
    state, identifier = reviewed_fixture(demo)
    state.clock.advance(EXPIRES_AT-state.clock.now()-timedelta(seconds=10))
    with state.session_factory() as session:
        session.info['record_authorized']=True
        row=session.get(m.Message,identifier)
        # Keep other independently enforced deadlines later for this focused
        # boundary test; exact production approvals are never rewritten.
        approval=confirmations.proof_for(session,row)
        row.created_at=state.clock.now()
        approval.payload={**approval.payload,'expires_at':(EXPIRES_AT+timedelta(minutes=1)).isoformat()}
        assert datetime.fromisoformat(_submission_deadline(session,state,row,state.clock.now())) == EXPIRES_AT


def test_event_exception_requires_exact_existing_acceptance_assignment(signup):
    state=setup(signup);assert create(signup).status_code==200
    with state.session_factory() as session:
        assert deadline(session,state.provider,PHONE,'reminder',START,source={'type':'assignment','purpose':'reminder','assignment_id':999}) is None
        assert deadline(session,state.provider,PHONE,'confirmation',START) is None
        session.info['record_authorized']=True
        person=m.Volunteer(name='Synthetic Participant',phone=PHONE,sms_opt_in=True,status='active',created_at=START)
        event=m.Event(title='Demo: synthetic acceptance',starts_at=START+timedelta(days=1),ends_at=START+timedelta(days=1,hours=1),status='scheduled')
        role=session.scalar(select(m.Role).where(m.Role.name=='Greeter'))
        session.add_all([person,event]);session.flush()
        shift=m.Shift(event_id=event.id,role_id=role.id,slot_index=0);session.add(shift);session.flush()
        assignment=m.Assignment(shift_id=shift.id,volunteer_id=person.id,status='approved',source='planner',created_at=START,updated_at=START)
        session.add(assignment);session.flush()
        source={'type':'assignment','purpose':'reminder','assignment_id':assignment.id,'shift_id':shift.id,'event_id':event.id}
        assert deadline(session,state.provider,PHONE,'reminder',START,source=source) is None
        session.add(m.Policy(key='acceptance:workflow:synthetic',value={'assignment_id':assignment.id,'shift_id':shift.id,'event_id':event.id}))
        session.flush()
        assert deadline(session,state.provider,PHONE,'reminder',START,source=source)==EXPIRES_AT
        assert deadline(session,state.provider,PHONE,'reminder',START,source={**source,'event_id':9999}) is None


def test_grant_requires_explicit_superadmin_and_cannot_supply_later_expiry(signup):
    state=setup(signup)
    client=TestClient(signup)
    assert client.post('/api/cloud-texting/demo/quiet-test',json={'signup_phone':PHONE,'admin_phone':ADMIN_PHONE,'confirmed':False}).status_code==400
    assert client.post('/api/cloud-texting/demo/quiet-test',json={'signup_phone':PHONE,'admin_phone':ADMIN_PHONE,'confirmed':True,'expires_at':'2099-01-01T00:00:00Z'}).status_code==400
    from app.web.texty import admin
    signup.dependency_overrides[admin]=lambda:{'email':'other@example.test','email_confirmed_at':'verified'}
    assert create(signup).status_code==403
