"""Website-origin invitation safety, with synthetic phones and no native worker."""
from datetime import timedelta
from uuid import uuid4

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select

from app.config import Settings
from app.db import models as m
from app.db.session import make_session_factory
from app.main import create_app
from app.web.texty import admin
from app.core.signup_copy import MAC_DEMO_WELCOME
from app.llm.gloo_client import GlooUnavailableError
from tests.test_exact_signup_copy import ExactGloo
from tests.session_fixtures import session_id, session_json

PHONE = '+12025550199'
OTHER = '+12025550198'


@pytest.fixture
def invitation_app(session, clock):
    app = create_app(Settings(database_url='sqlite://', demo_mode=True,
        sms_provider='mac_messages', mac_bridge_enabled=True,
        mac_bridge_token='synthetic-bridge-token-' + 'x' * 32,
        admin_password='synthetic-admin-password', mac_demo_phones=PHONE + ',' + OTHER,
        mac_test_sessions=session_json([PHONE, OTHER], clock.now()),
        gloo_signup_replies=True, automation_enabled=False))
    app.state.session_factory = make_session_factory(session.get_bind())
    app.state.clock = app.state.mac_delivery_clock = clock
    app.state.gloo = ExactGloo()
    app.dependency_overrides[admin] = lambda: {'email': 'operator@example.test'}
    session.add_all([
        m.Policy(key='full_text_onboarding', value={'value': True}),
        m.Policy(key='signup_exact_copy:' + PHONE, value={'value': True}),
        m.Policy(key='mac_demo_invitation:' + PHONE,
            value={'value': True, 'session_id': session_id(PHONE)}),
    ])
    session.commit()
    return app


def payload(**changes):
    return {'phone': PHONE, 'name': 'Synthetic Recipient', 'request_id': str(uuid4()), **changes}


def test_website_invites_once_without_creating_consent_or_profile(session, invitation_app):
    data = payload()
    with TestClient(invitation_app) as client:
        first = client.post('/api/signup-invitations', json=data)
        assert first.status_code == 200, first.text
        assert first.json()['delivery'] == 'queued_for_mac'
        assert first.json()['body'] == MAC_DEMO_WELCOME
        assert client.post('/api/signup-invitations', json=data).json() == first.json()
        assert client.post('/api/signup-invitations', json=payload()).status_code == 409
        assert client.post('/api/signup-invitations', json={**data, 'name': 'Changed'}).status_code == 409
    session.expire_all()
    messages = session.scalars(select(m.Message)).all()
    assert len(messages) == 1 and messages[0].kind == 'ai' and messages[0].status == 'queued'
    assert messages[0].provider_sid.startswith('MAC' + session_id(PHONE))
    assert session.scalar(select(m.Volunteer)) is None
    assert len(invitation_app.state.gloo.calls) == 1


@pytest.mark.parametrize('defect', ['wrong_phone', 'wrong_session', 'disabled', 'expired', 'existing_profile', 'stop', 'gloo', 'style', 'quiet'])
def test_invitation_holds_unavailable_or_unauthorized_recipients(session, clock, invitation_app, defect):
    data = payload()
    if defect == 'wrong_phone': data['phone'] = OTHER
    if defect == 'wrong_session':
        session.get(m.Policy, 'mac_demo_invitation:' + PHONE).value = {'value': True, 'session_id': 'f' * 32}
    if defect == 'disabled': session.get(m.Policy, 'full_text_onboarding').value = {'value': False}
    if defect == 'expired': clock.advance(timedelta(hours=2))
    if defect == 'existing_profile':
        session.add(m.Volunteer(name='Existing', phone=PHONE, sms_opt_in=True, status='active',
            is_coordinator=False, is_pastor=False, preferences={}, created_at=clock.now()))
    if defect == 'stop': session.add(m.Policy(key='sms_opt_out:' + PHONE, value={'value': True}))
    if defect == 'gloo':
        class Unavailable(ExactGloo):
            def create_response(self, **kwargs): raise GlooUnavailableError('synthetic unavailable')
        invitation_app.state.gloo = Unavailable()
    if defect == 'style':
        from types import SimpleNamespace
        class Invalid(ExactGloo):
            def create_response(self, **kwargs): return SimpleNamespace(output_text=MAC_DEMO_WELCOME + ' — invalid')
        invitation_app.state.gloo = Invalid()
    if defect == 'quiet':
        session.add(m.Policy(key='quiet_hours', value={'value': {'start': '09:00', 'end': '11:00'}}))
    session.commit()
    with TestClient(invitation_app) as client:
        result = client.post('/api/signup-invitations', json=data)
        assert result.status_code in {403, 409, 503}, result.text
    session.expire_all()
    assert session.scalar(select(m.Message)) is None
    assert session.scalar(select(m.Notification).where(m.Notification.purpose == 'signup_invitation')) is None


def test_invitation_requires_signed_in_account_and_valid_request(session, invitation_app):
    with TestClient(invitation_app) as client:
        assert client.post('/api/signup-invitations', json=payload(phone='2025550199')).status_code == 400
        assert client.post('/api/signup-invitations', json=payload(request_id='bad')).status_code == 400
        invitation_app.dependency_overrides.pop(admin)
        assert client.post('/api/signup-invitations', json=payload()).status_code in {401, 503}
    session.expire_all()
    assert session.scalar(select(m.Message)) is None


def test_invitation_refuses_disconnected_or_google_transport(session, invitation_app):
    from app.sms.mock_provider import MockSMSProvider
    provider = MockSMSProvider()
    invitation_app.state.provider = provider
    with TestClient(invitation_app) as client:
        assert client.post('/api/signup-invitations', json=payload()).status_code == 503
        provider.transport_name = 'google_voice'
        assert client.post('/api/signup-invitations', json=payload()).status_code == 503
    assert not provider.sent


def test_competition_mode_stages_exact_review_without_queueing(session, invitation_app):
    from app.core import confirmations
    from dataclasses import replace
    invitation_app.state.settings = replace(invitation_app.state.settings, competition_confirmation_required=True)
    invitation_app.state.session_factory.configure(info={confirmations.MODE_KEY: True})
    data = payload()
    with TestClient(invitation_app) as client:
        response = client.post('/api/signup-invitations', json=data)
        assert response.status_code == 200, response.text
        result = response.json()
        assert result['delivery'] == 'awaiting_confirmation' and result['approval_id']
        assert result['message_id'] is None
        assert client.post('/api/signup-invitations', json=data).json() == result
    session.expire_all()
    review = session.get(m.Approval, result['approval_id'])
    assert review.status == 'pending' and review.payload['body'] == MAC_DEMO_WELCOME
    assert review.payload['phone'] == PHONE and review.payload['session_id'] == session_id(PHONE)
    assert session.scalar(select(m.Message)) is None
    assert session.scalar(select(m.Volunteer)) is None


def test_gloo_failure_can_retry_same_request_after_recovery(session, invitation_app):
    class Unavailable(ExactGloo):
        def create_response(self, **kwargs): raise GlooUnavailableError('synthetic unavailable')
    invitation_app.state.gloo = Unavailable()
    data = payload()
    with TestClient(invitation_app) as client:
        assert client.post('/api/signup-invitations', json=data).status_code == 503
        invitation_app.state.gloo = ExactGloo()
        response = client.post('/api/signup-invitations', json=data)
        assert response.status_code == 200, response.text
        assert response.json()['delivery'] == 'queued_for_mac'
    session.expire_all()
    assert len(session.scalars(select(m.Message)).all()) == 1


def test_legacy_name_invitation_reports_route_hold_without_requeue_or_audit_change(session,invitation_app):
    from copy import deepcopy
    from app.integrations.mac_models import MacDeliveryClaim
    data=payload()
    headers={'Authorization':'Bearer '+invitation_app.state.settings.mac_bridge_token}
    with TestClient(invitation_app) as client:
        original=client.post('/api/signup-invitations',json=data).json()
        claim=client.post('/mac/outbound/pull',json={},headers=headers).json()['messages'][0]
        assert client.post(f"/mac/outbound/{claim['id']}/route-hold",json={'token':claim['token']},headers=headers).status_code==200
        session.expire_all()
        key='signup-invitation:'+data['request_id'];proof=deepcopy(session.get(m.Notification,key).detail)
        held=client.post('/api/signup-invitations',json=data).json()
        assert held['delivery']=='held_native_route' and 'Automatic retry is unavailable' in held['reason']
        assert held['body']==original['body'] and held['message_id']==original['message_id']
        assert client.post('/api/signup-invitations',json=payload()).status_code==409
        assert client.post('/mac/outbound/pull',json={},headers=headers).json()['messages']==[]
    session.expire_all()
    assert session.get(m.Notification,key).detail==proof
    assert session.get(MacDeliveryClaim,claim['id']).token==claim['token']
    assert len(session.scalars(select(m.Message)).all())==1
    assert session.scalar(select(m.Volunteer)) is None
    assert len(invitation_app.state.gloo.calls)==1
