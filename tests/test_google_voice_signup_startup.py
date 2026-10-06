"""Offline bounded startup probes, never a real account/model/native request."""
from contextlib import contextmanager
from copy import deepcopy
from datetime import timedelta

import httpx
import pytest

from app.db import models as m
from app.integrations.google_voice_client import GoogleVoiceConnector, ConnectorUnavailable, ConnectorConnectionUnavailable
from app.integrations.google_voice_demo import RECIPIENT_KEY, scope_fingerprint
from app.integrations.google_voice_signup import KEY, tick_signup, signup_status, start_service, stop_service
from app.sms.google_voice_provider import GoogleVoiceProvider
from tests.test_google_voice_signup import signup, enable, inbound, PHONE
from tests.test_google_voice_demo import demo, dynamic_demo, register


def prepared(signup):
    state = signup.state
    enable(signup)
    assert register(signup).status_code == 200
    health = state.google_voice_connector.health
    def starting():
        return {'demo_mode': True, 'scope_fingerprint': scope_fingerprint(state.provider.test_sessions),
            'state': 'reconnect_required', 'reason_code': 'session_not_verified', 'ready': False,
            'identity_verified': False, 'expected_identity_match': False, 'identity_fingerprint': None}
    state.google_voice_connector.health = starting
    with state.session_factory() as session:
        original = deepcopy(session.get(m.Policy, KEY).value)
    return state, health, original


def test_cold_restart_waits_then_resumes_same_grant_only_after_ready_intake(signup):
    state, ready, original = prepared(signup)
    tick_signup(state)
    assert signup_status(state)['state'] == 'waiting_connection'
    assert state.gloo.calls == state.google_voice_connector.calls == []
    assert state.google_voice_connector.scoped_checks == []
    # A recreated provider/scheduler uses the same durable retry and scope.
    stop_service(state)
    state.provider = GoogleVoiceProvider(state.settings)
    start_service(state)
    assert state.google_voice_signup_scheduler
    tick_signup(state)  # Backoff does not navigate or process input.
    assert state.google_voice_connector.scoped_checks == []
    state.clock.advance(timedelta(seconds=15))
    state.google_voice_connector.health = ready
    tick_signup(state)
    with state.session_factory() as session:
        saved = session.get(m.Policy, KEY).value
        assert saved['id'] == original['id'] and saved['actor'] == original['actor']
        assert saved['state'] == 'enabled' and 'connection_retry' not in saved
    assert state.google_voice_connector.scoped_checks == [[PHONE]]
    assert len(state.google_voice_connector.calls) == 1  # Original bounded invitation, once.


def test_constructor_pending_after_backoff_allows_one_verified_scoped_intake(signup):
    state, ready, _ = prepared(signup)
    tick_signup(state)
    original_intake = state.google_voice_connector.intake
    def intake(**kwargs):
        state.google_voice_connector.health = ready
        return original_intake(**kwargs)
    state.google_voice_connector.intake = intake
    state.clock.advance(timedelta(seconds=15))
    tick_signup(state)
    assert signup_status(state)['state'] == 'enabled'
    assert state.google_voice_connector.scoped_checks == [[PHONE]]


def test_retry_after_connection_timeout_is_read_only_and_stop_wins_before_invitation(signup):
    state, ready, original = prepared(signup)
    state.google_voice_connector.health = ready
    real_intake = state.google_voice_connector.intake
    state.google_voice_connector.intake = lambda **_: (_ for _ in ()).throw(ConnectorConnectionUnavailable())
    tick_signup(state)
    assert signup_status(state)['state'] == 'waiting_connection'
    assert not state.gloo.calls and not state.google_voice_connector.calls
    inbound(signup, 'STOP', 'stop-during-startup')
    state.google_voice_connector.intake = real_intake
    state.clock.advance(timedelta(seconds=15))
    tick_signup(state)
    assert not state.gloo.calls and not state.google_voice_connector.calls
    with state.session_factory() as session:
        assert session.get(m.Policy, 'sms_opt_out:' + PHONE).value['value']
        assert session.get(m.Policy, KEY).value['id'] == original['id']


@pytest.mark.parametrize('change', ['disable', 'epoch', 'sender', 'session', 'malformed', 'deadline', 'uncertain'])
def test_waiting_cannot_resume_changed_or_unsafe_authority(signup, change):
    state, ready, _ = prepared(signup)
    tick_signup(state)
    with state.session_factory() as session:
        row = session.get(m.Policy, KEY)
        if change == 'disable': row.value = {**row.value, 'enabled': False, 'state': 'off'}
        if change == 'epoch': row.value = {**row.value, 'id': 'e' * 32}
        if change == 'sender': row.value = {**row.value, 'sender_fingerprint': '0' * 64}
        if change == 'session':
            registration = session.get(m.Policy, RECIPIENT_KEY + PHONE)
            registration.value = {**registration.value, 'session': {**registration.value['session'], 'id': 'b' * 32}}
        if change == 'malformed': row.value = {**row.value, 'connection_retry': {'reason_code': 'connector_starting'}}
        if change == 'uncertain':
            message = m.Message(direction='out', phone=PHONE, body='Synthetic unknown submission', kind='ai',
                purpose='signup_reply', provider_sid=state.provider.test_sessions[PHONE].outbound_prefix+'unknown',
                status='uncertain', created_at=state.clock.now())
            session.add(message)
        session.commit()
    state.clock.advance(timedelta(minutes=11) if change == 'deadline' else timedelta(seconds=15))
    state.google_voice_connector.health = ready
    tick_signup(state)
    assert not state.gloo.calls and not state.google_voice_connector.calls
    assert state.google_voice_connector.scoped_checks == []


@pytest.mark.parametrize('reason', ['account_mismatch', 'state_account_mismatch', 'reconnect_required',
    'browser_unavailable', 'message_format_changed', 'message_timestamp_ambiguous'])
def test_untyped_or_security_readiness_failure_remains_permanent(signup, reason):
    state, _, _ = prepared(signup)
    starting = state.google_voice_connector.health
    state.google_voice_connector.health = lambda: {**starting(), 'reason_code': reason}
    tick_signup(state)
    assert signup_status(state)['state'] == 'held'
    start_service(state)
    assert not state.google_voice_signup_scheduler
    assert not state.google_voice_connector.scoped_checks
    assert not state.gloo.calls and not state.google_voice_connector.calls


def test_probe_that_becomes_security_hold_does_not_retry_intake(signup):
    state, ready, _ = prepared(signup)
    tick_signup(state)
    state.clock.advance(timedelta(seconds=15))
    state.google_voice_connector.health = ready
    state.google_voice_connector.intake = lambda **_: {**ready(), 'ready': False, 'reason_code': 'message_format_changed'}
    tick_signup(state)
    assert signup_status(state)['state'] == 'held'
    assert not state.gloo.calls and not state.google_voice_connector.calls


def test_retry_window_and_attempt_budget_survive_restart_without_extension(signup):
    state, _, _ = prepared(signup)
    state.google_voice_connector.health = lambda: (_ for _ in ()).throw(ConnectorConnectionUnavailable())
    for expected in range(1, 6):
        tick_signup(state)
        with state.session_factory() as session:
            retry = session.get(m.Policy, KEY).value['connection_retry']
            assert retry['attempts'] == expected
            if expected == 1: until = retry['until']
            assert retry['until'] == until
        stop_service(state)
        start_service(state)
        state.clock.advance(timedelta(seconds=15 * 2 ** (expected - 1)))
    tick_signup(state)
    assert signup_status(state)['state'] == 'held'
    assert not state.gloo.calls and not state.google_voice_connector.calls


def test_historic_generic_held_record_is_never_auto_resumed(signup):
    state, _, _ = prepared(signup)
    with state.session_factory() as session:
        row = session.get(m.Policy, KEY)
        row.value = {**row.value, 'state': 'held', 'reason': 'Cloud sign-in or participant conversation requires attention'}
        session.commit()
    stop_service(state)
    start_service(state)
    tick_signup(state)
    assert signup_status(state)['state'] == 'held' and not state.google_voice_signup_scheduler
    assert not state.google_voice_connector.scoped_checks


@pytest.mark.parametrize('path', ['/health', '/demo/intake', '/send'])
def test_client_types_transport_timeouts_only_on_read_only_operations(signup, monkeypatch, path):
    @contextmanager
    def failed(*_, **__):
        raise httpx.ConnectTimeout('Synthetic timeout')
        yield
    monkeypatch.setattr(httpx.Client, 'stream', failed)
    client = GoogleVoiceConnector(signup.state.settings)
    with pytest.raises(ConnectorUnavailable) as failure:
        client._request('GET' if path == '/health' else 'POST', path)
    assert isinstance(failure.value, ConnectorConnectionUnavailable) is (path != '/send')


@pytest.mark.parametrize('status, body', [(503, {'error': 'connector_unavailable'}), (401, {}), (200, ['wrong shape'])])
def test_http_and_protocol_failure_never_invents_transient_classification(signup, monkeypatch, status, body):
    @contextmanager
    def response(*_, **__):
        yield httpx.Response(status, json=body, request=httpx.Request('GET', 'http://google-voice/health'))
    monkeypatch.setattr(httpx.Client, 'stream', response)
    with pytest.raises(ConnectorUnavailable) as failure:
        GoogleVoiceConnector(signup.state.settings).health()
    assert not isinstance(failure.value, ConnectorConnectionUnavailable)
