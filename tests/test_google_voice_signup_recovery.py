"""Recover only the original stored unknown-number signup input, offline."""
from dataclasses import replace
from contextlib import contextmanager
from datetime import timedelta

import httpx
import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select

from app.db import models as m
from app.integrations.google_voice_models import GoogleVoiceInboundReceipt
from app.integrations.google_voice_runtime import _incoming
from app.integrations.google_voice_signup import KEY, tick_signup
from app.integrations.google_voice_demo import RECIPIENT_KEY, registered_consent_provenance
from app.llm.gloo_client import GlooUnavailableError
from tests.test_google_voice_signup import signup, enable, inbound, PHONE
from tests.test_google_voice_demo import demo, dynamic_demo, register


def initial(signup):
    state = signup.state
    state.settings = replace(state.settings, allow_text_signup=False)
    enable(signup)
    assert register(signup).status_code == 200
    tick_signup(state)
    return state


def original_unknown(signup):
    state = initial(signup)
    inbound(signup, 'Judge Example', 'original-name-receipt')
    item = dict(state.google_voice_connector.messages[0])
    # Reproduce the prior router without changing its original saved records.
    settings = state.settings
    state.settings = replace(settings, google_voice_signup_enabled=False)
    _incoming(state, item)
    state.settings = settings
    with state.session_factory() as session:
        receipt = session.get(GoogleVoiceInboundReceipt, item['id'])
        assert receipt.result['intent'] == 'unknown_number'
        source = session.get(m.Message, receipt.result['source_message_id'])
        original = {'id': source.id, 'created_at': source.created_at,
                    'body': source.body, 'result': dict(receipt.result)}
        assert session.scalar(select(m.Volunteer).where(m.Volunteer.phone == PHONE)) is None
    state.google_voice_connector.stored_reads = []
    def lookup(**request):
        state.google_voice_connector.stored_reads.append(request)
        assert request == {'id': item['id'], 'phone': PHONE, 'session_id': state.provider.test_sessions[PHONE].id}
        return {'message': dict(item), 'session_id': request['session_id']}
    state.google_voice_connector.stored_signup_input = lookup
    return state, item, original


def recover(signup, guid='original-name-receipt'):
    return TestClient(signup).post('/api/cloud-texting/signup/recover-input', json={'receipt_id': guid})


def test_registered_continuous_signup_ignores_global_unknown_number_signup_switch(signup):
    state = initial(signup)
    inbound(signup, 'Judge Example', 'new-name')
    # Intake only: this assertion does not run dispatch or make a native call.
    _incoming(state, state.google_voice_connector.messages[0])
    assert state.gloo.calls and len(state.google_voice_connector.calls) == 1
    with state.session_factory() as session:
        person = session.scalar(select(m.Volunteer).where(m.Volunteer.phone == PHONE))
        assert person.name == 'Judge Example' and person.sms_opt_in
        assert session.get(GoogleVoiceInboundReceipt, 'new-name').result['intent'] != 'unknown_number'


def test_recovery_reuses_original_message_receipt_and_time_then_is_idempotent(signup):
    state, item, original = original_unknown(signup)
    before_calls = len(state.gloo.calls)
    state.clock.advance(timedelta(minutes=5))
    result = recover(signup)
    assert result.status_code == 200, result.text
    assert result.json()['source_message_id'] == original['id']
    assert result.json()['native_submission_attempted'] is False
    assert len(state.google_voice_connector.calls) == 1
    with state.session_factory() as session:
        source = session.get(m.Message, original['id'])
        assert source.created_at == original['created_at'] and source.body == original['body']
        assert len(list(session.scalars(select(m.Message).where(m.Message.direction == 'in', m.Message.phone == PHONE)))) == 1
        receipt = session.get(GoogleVoiceInboundReceipt, item['id'])
        assert receipt.result['source_message_id'] == source.id and receipt.result['recovered_signup']
        person = session.scalar(select(m.Volunteer).where(m.Volunteer.phone == PHONE))
        assert person.sms_opt_in and person.preferences['consent_at'] == item['received_at']
        session.info['mac_test_session'] = state.provider.test_sessions[PHONE]
        assert registered_consent_provenance(session, person)
        approval = session.scalar(select(m.Approval).where(m.Approval.payload['reply_to_message_id'].as_integer() == source.id))
        assert approval and approval.payload['purpose'] == 'signup_reply' and approval.status == 'pending'
        audit = session.get(m.Notification, 'google-signup-input-recovery:' + item['id'])
        assert audit.detail['previous_result'] == original['result']
    calls = len(state.gloo.calls)
    assert calls > before_calls
    repeated = recover(signup)
    assert repeated.status_code == 200 and repeated.json()['already_processed']
    assert len(state.gloo.calls) == calls and len(state.google_voice_connector.stored_reads) == 1
    assert len(state.google_voice_connector.calls) == 1


@pytest.mark.parametrize('change', ['off', 'held', 'paused', 'sender', 'session', 'suppressed',
    'uncertain', 'receipt_fingerprint', 'receipt_source', 'source_body', 'source_kind', 'source_scope',
    'sidecar_body', 'sidecar_time', 'submitted_invitation', 'control'])
def test_recovery_rejects_changed_or_unauthorized_original_evidence_before_gloo(signup, change):
    state, item, original = original_unknown(signup)
    calls = len(state.gloo.calls)
    with state.session_factory() as session:
        session.info['record_authorized'] = True
        receipt = session.get(GoogleVoiceInboundReceipt, item['id'])
        source = session.get(m.Message, original['id'])
        policy = session.get(m.Policy, KEY)
        registration = session.get(m.Policy, RECIPIENT_KEY + PHONE)
        if change == 'off': policy.value = {**policy.value, 'enabled': False}
        if change == 'held': policy.value = {**policy.value, 'state': 'held'}
        if change == 'paused': session.get(m.Policy, 'google_voice:paused').value = {'value': True}
        if change == 'sender': registration.value = {**registration.value, 'sender_fingerprint': '0' * 64}
        if change == 'session': receipt.result = {**receipt.result, 'session_id': 'b' * 32}
        if change == 'suppressed': session.add(m.Policy(key='sms_opt_out:' + PHONE, value={'value': True}))
        if change in {'uncertain', 'submitted_invitation'}:
            outgoing = session.scalar(select(m.Message).where(m.Message.direction == 'out', m.Message.phone == PHONE))
            outgoing.status = 'uncertain' if change == 'uncertain' else 'rejected'
        if change == 'receipt_fingerprint': receipt.fingerprint = '0' * 64
        if change == 'receipt_source': receipt.result = {**receipt.result, 'source_message_id': 999999}
        if change == 'source_body': source.body = 'Different Name'
        if change == 'source_kind': source.kind = 'inbound'
        if change == 'source_scope': source.purpose = 'test:' + 'b' * 32
        if change == 'sidecar_body': item['body'] = 'Different Name'
        if change == 'sidecar_time': item['received_at'] = (original['created_at'] + timedelta(seconds=1)).isoformat()
        if change == 'control': item['body'] = 'START'
        session.commit()
    response = recover(signup)
    assert response.status_code == 409, response.text
    assert len(state.gloo.calls) == calls and len(state.google_voice_connector.calls) == 1
    with state.session_factory() as session:
        assert session.scalar(select(m.Volunteer).where(m.Volunteer.phone == PHONE)) is None
        assert session.get(m.Notification, 'google-signup-input-recovery:' + item['id']) is None


def test_recovery_gloo_failure_rolls_back_original_and_can_process_once_after_repair(signup):
    state, item, original = original_unknown(signup)
    method = state.gloo.create_response
    def fail(**_kwargs): raise GlooUnavailableError('Synthetic unavailable')
    state.gloo.create_response = fail
    assert recover(signup).status_code == 503
    with state.session_factory() as session:
        assert session.get(GoogleVoiceInboundReceipt, item['id']).result == original['result']
        assert session.scalar(select(m.Volunteer).where(m.Volunteer.phone == PHONE)) is None
        assert session.get(m.Notification, 'google-signup-input-recovery:' + item['id']) is None
        assert len(list(session.scalars(select(m.Message).where(m.Message.direction == 'in', m.Message.phone == PHONE)))) == 1
    state.gloo.create_response = method
    assert recover(signup).status_code == 200
    assert len(state.google_voice_connector.calls) == 1


def test_recovery_endpoint_requires_superadmin_and_accepts_no_supplied_body(signup):
    assert TestClient(signup).post('/api/cloud-texting/signup/recover-input',
        json={'receipt_id': 'original-name-receipt', 'body': 'An invented name'}).status_code == 400
    # The fixture's verified account is overridden at the admin dependency,
    # not at the superadmin boundary. A different verified actor is rejected.
    from app.web.texty import admin
    signup.dependency_overrides[admin] = lambda: {'email': 'other@example.test', 'email_confirmed_at': 'verified'}
    assert recover(signup).status_code == 403


def test_registered_gate_does_not_authorize_unregistered_or_not_yet_invited_input(signup):
    state = initial(signup)
    calls = len(state.gloo.calls)
    foreign = {'id': 'foreign-reply', 'phone': '+12025550156', 'body': 'Judge Example',
        'received_at': state.clock.now().isoformat()}
    _incoming(state, foreign)
    assert len(state.gloo.calls) == calls
    assert register(signup, foreign['phone']).status_code == 200
    _incoming(state, foreign)
    assert len(state.gloo.calls) == calls
    with state.session_factory() as session:
        assert session.get(GoogleVoiceInboundReceipt, foreign['id']).result['intent'] == 'unknown_number'
        assert session.scalar(select(m.Volunteer).where(m.Volunteer.phone == foreign['phone'])) is None


def test_recovery_stop_before_model_holds_even_after_enable(signup):
    state, item, original = original_unknown(signup)
    inbound(signup, 'STOP', 'withdrawal')
    _incoming(state, state.google_voice_connector.messages[0])
    calls = len(state.gloo.calls)
    assert recover(signup).status_code == 409
    assert len(state.gloo.calls) == calls
    with state.session_factory() as session:
        assert session.get(GoogleVoiceInboundReceipt, item['id']).result == original['result']
        assert session.get(m.Policy, 'sms_opt_out:' + PHONE).value['value']


def test_private_client_uses_exact_authenticated_stored_input_lookup(signup, monkeypatch):
    from app.integrations.google_voice_client import GoogleVoiceConnector
    seen = []
    @contextmanager
    def stream(_client, method, url, **kwargs):
        seen.append((method, url, kwargs))
        yield httpx.Response(200, json={'message': {'id': 'stored-original'}, 'session_id': 'a' * 32},
            request=httpx.Request(method, url))
    monkeypatch.setattr(httpx.Client, 'stream', stream)
    client = GoogleVoiceConnector(signup.state.settings)
    result = client.stored_signup_input(id='stored-original', phone=PHONE, session_id='a' * 32)
    assert result['message']['id'] == 'stored-original'
    assert seen[0][0] == 'POST' and seen[0][1].endswith('/demo/signup-input')
    assert seen[0][2]['json'] == {'id': 'stored-original', 'phone': PHONE, 'session_id': 'a' * 32}
    assert seen[0][2]['headers']['Authorization'] == 'Bearer ' + signup.state.settings.google_voice_connector_token
