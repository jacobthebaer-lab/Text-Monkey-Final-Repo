"""Exact quiet-hold successor review with synthetic Gloo and connector only."""
from copy import deepcopy
from dataclasses import replace
from datetime import datetime, timedelta, timezone
import json

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select, func

from app.core import confirmations
from app.db import models as m
from app.integrations.google_voice_models import GoogleVoiceDeliveryClaim
from app.integrations.google_voice_quiet_test import EXPIRES_AT
from app.integrations.google_voice_runtime import _incoming
from app.integrations.google_voice_signup import tick_signup
from app.sms.google_voice_provider import GoogleVoiceTestSession
from tests.test_google_voice_signup import signup, enable, inbound, PHONE
from tests.test_google_voice_demo import demo, dynamic_demo, register
from tests.test_google_voice import EMAIL

ADMIN_PHONE = '+12025550156'
START = datetime(2026, 10, 6, 3, 40, tzinfo=timezone.utc)


@pytest.fixture
def held(signup):
    state = signup.state
    enable(signup)
    assert register(signup).status_code == 200
    tick_signup(state)
    state.clock.advance(START - state.clock.now())
    inbound(signup, 'Judge Example', 'quiet-original-input')
    _incoming(state, state.google_voice_connector.messages[0])
    with state.session_factory() as session:
        approval = session.scalar(select(m.Approval).where(m.Approval.status == 'pending',
            m.Approval.payload['reply_to_message_id'].as_integer().is_not(None)))
        assert approval
        identifier, expected = approval.id, approval.payload['content_hash']
        payload = deepcopy(approval.payload)
    # Reproduce approval after the ordinary ten-minute immediate-reply window.
    state.clock.advance(timedelta(minutes=11))
    client = TestClient(signup)
    response = client.post(f'/api/proposals/{identifier}/approve', json={'content_hash': expected})
    assert response.status_code == 200, response.text
    with state.session_factory() as session:
        assert session.get(m.Approval, identifier).status == 'expired'
        assert session.get(m.Notification, f'review:{identifier}:blocked').detail['detail'] == 'inside quiet hours'
    state.provider.test_sessions[ADMIN_PHONE] = GoogleVoiceTestSession('c' * 32, START, EXPIRES_AT)
    state.provider.phones = frozenset([*state.provider.phones, ADMIN_PHONE])
    state.settings = replace(state.settings, google_voice_demo_phones=ADMIN_PHONE,
        google_voice_test_sessions=json.dumps({ADMIN_PHONE: {'id': 'c' * 32,
            'starts_at': START.isoformat(), 'expires_at': EXPIRES_AT.isoformat()}}))
    state.provider.settings = state.settings
    state.google_voice_connector.sessions = dict(state.provider.test_sessions)
    return signup, identifier, expected, payload


def allow(held):
    app, *_ = held
    response = TestClient(app).post('/api/cloud-texting/demo/quiet-test',
        json={'signup_phone': PHONE, 'admin_phone': ADMIN_PHONE, 'confirmed': True})
    assert response.status_code == 200, response.text


def stage(held):
    app, identifier, expected, _ = held
    return TestClient(app).post(f'/api/cloud-texting/demo/quiet-review/{identifier}',
        json={'content_hash': expected})


def test_one_exact_successor_preserves_original_and_requires_new_review(held):
    app, original_id, expected, payload = held
    allow(held)
    native, models = len(app.state.google_voice_connector.calls), len(app.state.gloo.calls)
    with app.state.session_factory() as session:
        message_count = session.scalar(select(func.count()).select_from(m.Message))
    first = stage(held)
    assert first.status_code == 200, first.text
    new_id = first.json()['approval_id']
    assert first.json()['native_submission_attempted'] is False
    assert stage(held).json() == {**first.json(), 'already_staged': True}
    with app.state.session_factory() as session:
        original, successor = session.get(m.Approval, original_id), session.get(m.Approval, new_id)
        assert original.status == 'expired' and original.payload == payload
        assert successor.status == 'pending' and successor.payload == payload
        assert successor.decided_at is None
        assert session.get(m.Notification, f'google-voice-gloo:{new_id}').detail['quiet_predecessor_id'] == original_id
        assert session.scalar(select(func.count()).select_from(GoogleVoiceDeliveryClaim)) == 1  # prior invitation only
        assert session.scalar(select(func.count()).select_from(m.Message)) == message_count
        from app.integrations.google_voice_signup import authorize_pending, refresh_unsent_signup_replies
        authorize_pending(session, app.state)
        assert successor.status == 'pending'  # Continuous signup cannot auto-approve it.
        session.commit()
    assert len(app.state.gloo.calls) == models and len(app.state.google_voice_connector.calls) == native
    response = TestClient(app).post(f'/api/proposals/{new_id}/approve', json={'content_hash': expected})
    assert response.status_code == 200, response.text
    with app.state.session_factory() as session:
        successor = session.get(m.Approval, new_id)
        assert successor.status == 'approved' and successor.payload['message_id']
        assert session.get(m.Message, successor.payload['message_id']).status == 'queued'
        assert successor.payload['expires_at'] == payload['expires_at']
        assert session.scalar(select(func.count()).select_from(GoogleVoiceDeliveryClaim)) == 1
    assert len(app.state.gloo.calls) == models and len(app.state.google_voice_connector.calls) == native
    assert stage(held).status_code == 409


@pytest.mark.parametrize('change', ['no_exception', 'expired', 'revoked', 'wrong_hash', 'blocked_reason', 'approve_audit',
    'gloo', 'STOP', 'session', 'sender', 'uncertain', 'source', 'body', 'initial_invitation', 'paused'])
def test_changed_or_unproven_quiet_hold_never_stages(held, change):
    app, identifier, expected, _ = held
    if change != 'no_exception': allow(held)
    state = app.state
    with state.session_factory() as session:
        session.info['record_authorized'] = True
        approval = session.get(m.Approval, identifier)
        if change == 'expired': state.clock.advance(timedelta(hours=3))
        if change == 'revoked': approval.status = 'revoked'
        if change == 'wrong_hash': held = (app, identifier, '0' * 64, held[3])
        if change == 'blocked_reason':
            note = session.get(m.Notification, f'review:{identifier}:blocked')
            note.detail = {**note.detail, 'detail': 'recipient opted out'}
        if change == 'approve_audit': session.delete(session.get(m.Notification, f'review:{identifier}:approve'))
        if change == 'gloo': session.delete(session.get(m.Notification, f'google-voice-gloo:{identifier}'))
        if change == 'STOP': session.add(m.Policy(key='sms_opt_out:' + PHONE, value={'value': True}))
        if change == 'session':
            from app.integrations.google_voice_demo import RECIPIENT_KEY
            registration = session.get(m.Policy, RECIPIENT_KEY + PHONE)
            registration.value = {**registration.value, 'session': {**registration.value['session'], 'id': 'b' * 32}}
        if change == 'sender':
            from app.integrations.google_voice_demo import RECIPIENT_KEY
            registration = session.get(m.Policy, RECIPIENT_KEY + PHONE)
            registration.value = {**registration.value, 'sender_fingerprint': '0' * 64}
        if change == 'uncertain':
            message = session.scalar(select(m.Message).where(m.Message.direction == 'out'))
            message.status = 'uncertain'
        if change == 'source':
            source = session.get(m.Message, approval.payload['reply_to_message_id'])
            source.body = 'Altered original input'
        if change == 'body': approval.payload = {**approval.payload, 'body': approval.payload['body'] + ' changed'}
        if change == 'initial_invitation': approval.payload = {**approval.payload, 'reply_to_message_id': None}
        if change == 'paused': session.get(m.Policy, 'google_voice:paused').value = {'value': True}
        session.commit()
    calls, models = len(state.google_voice_connector.calls), len(state.gloo.calls)
    assert stage(held).status_code == 409
    with state.session_factory() as session:
        assert session.get(m.Policy, f'google-quiet-review:{identifier}') is None
    assert len(state.google_voice_connector.calls) == calls and len(state.gloo.calls) == models


def test_successor_expiry_and_rejection_cannot_create_another(held):
    allow(held)
    first = stage(held)
    assert first.status_code == 200, first.text
    app, _, expected, _ = held
    new_id = first.json()['approval_id']
    assert TestClient(app).post(f'/api/proposals/{new_id}/reject', json={'content_hash': expected}).status_code == 200
    assert stage(held).status_code == 409


def test_expired_successor_chain_is_never_automatically_recomposed(held):
    allow(held)
    first = stage(held)
    assert first.status_code == 200, first.text
    app, _, _, _ = held
    models = len(app.state.gloo.calls)
    app.state.clock.advance(timedelta(hours=3))
    from app.integrations.google_voice_signup import refresh_unsent_signup_replies
    with app.state.session_factory() as session:
        count = session.scalar(select(func.count()).select_from(m.Approval))
        refresh_unsent_signup_replies(session, app.state)
        session.commit()
        assert session.scalar(select(func.count()).select_from(m.Approval)) == count
    assert len(app.state.gloo.calls) == models
    assert stage(held).status_code == 409


@pytest.mark.parametrize('change', ['original_gloo', 'original_audit', 'link'])
def test_successor_approval_rechecks_its_original_evidence(held, change):
    allow(held)
    result = stage(held)
    assert result.status_code == 200, result.text
    app, original_id, expected, _ = held
    new_id = result.json()['approval_id']
    with app.state.session_factory() as session:
        if change == 'original_gloo': session.delete(session.get(m.Notification, f'google-voice-gloo:{original_id}'))
        if change == 'original_audit':
            note = session.get(m.Notification, f'review:{original_id}:blocked')
            note.detail = {**note.detail, 'detail': 'recipient opted out'}
        if change == 'link':
            link = session.get(m.Policy, f'google-quiet-review:{original_id}')
            link.value = {**link.value, 'successor_id': 999999}
        session.commit()
    response = TestClient(app).post(f'/api/proposals/{new_id}/approve', json={'content_hash': expected})
    assert response.status_code == 200
    with app.state.session_factory() as session:
        assert session.get(m.Approval, new_id).payload.get('message_id') is None
        assert session.scalar(select(func.count()).select_from(GoogleVoiceDeliveryClaim)) == 1


def test_another_quiet_hold_on_successor_cannot_create_a_chain(held):
    allow(held)
    result = stage(held)
    assert result.status_code == 200, result.text
    app, _, expected, _ = held
    new_id = result.json()['approval_id']
    app.state.clock.advance(EXPIRES_AT-app.state.clock.now()+timedelta(seconds=1))
    assert TestClient(app).post(f'/api/proposals/{new_id}/approve', json={'content_hash': expected}).status_code == 200
    with app.state.session_factory() as session:
        assert session.get(m.Approval, new_id).status == 'expired'
    assert TestClient(app).post(f'/api/cloud-texting/demo/quiet-review/{new_id}',
        json={'content_hash': expected}).status_code == 409


def test_conflicting_runtime_step_holds_then_retry_returns_one_successor(held):
    allow(held)
    from app.integrations.google_voice_runtime import _tick_lock
    assert _tick_lock.acquire(blocking=False)
    try:
        assert stage(held).status_code == 409
    finally:
        _tick_lock.release()
    result = stage(held)
    assert result.status_code == 200, result.text
    assert stage(held).json()['approval_id'] == result.json()['approval_id']


def test_successor_endpoint_requires_exact_verified_operator_scope(held):
    allow(held)
    app, identifier, expected, _ = held
    client = TestClient(app)
    assert client.post(f'/api/cloud-texting/demo/quiet-review/{identifier}',
        json={'content_hash': expected, 'expires_at': '2099-01-01T00:00:00Z'}).status_code == 400
    from app.web.texty import admin
    app.dependency_overrides[admin] = lambda: {'email': 'other@example.test', 'email_confirmed_at': 'verified'}
    assert stage(held).status_code == 403
