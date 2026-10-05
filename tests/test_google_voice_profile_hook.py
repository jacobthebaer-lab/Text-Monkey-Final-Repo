"""Offline transactional Google capture hook, with a synthetic local capture sink."""
from dataclasses import replace
from sqlalchemy import select

from app.db import models as m
from app.integrations.google_voice_models import GoogleVoiceInboundReceipt
from app.integrations.google_voice_signup import tick_signup
from app.llm.gloo_client import GlooUnavailableError
from tests.test_google_voice_signup import signup, inbound, PHONE
from tests.test_google_voice_demo import demo, dynamic_demo
from tests.test_google_voice_expected_name import begin


def capture_fixture(signup, monkeypatch):
    begin(signup)
    state = signup.state
    state.settings = replace(state.settings, google_voice_profile_sync_enabled=True)
    calls = []
    def capture(session, settings, *, phone, guid, route, before, effective_at):
        receipt = session.get(GoogleVoiceInboundReceipt, guid)
        incoming = session.get(m.Message, receipt.result['source_message_id'])
        volunteer = session.scalar(select(m.Volunteer).where(m.Volunteer.phone == phone))
        assert incoming.direction == 'in' and incoming.status == 'received'
        assert incoming.phone == phone and incoming.purpose == 'test:' + receipt.result['session_id']
        assert receipt.result['intent'] == route
        calls.append({'body': incoming.body, 'before': before,
                      'opted_in': bool(volunteer and volunteer.sms_opt_in), 'route': route})
        # A durable local marker represents the hook's transaction, not a cloud write.
        session.add(m.Policy(key='synthetic-capture:' + guid, value={'source_message_id':incoming.id}))
    monkeypatch.setattr('app.integrations.google_voice_profile_sync.capture_google_profile', capture)
    return state, calls


def test_capture_hook_binds_exact_accepted_reply_and_commit_then_dedupes(signup, monkeypatch):
    state, calls = capture_fixture(signup, monkeypatch)
    inbound(signup, 'Judge Example', 'accepted-name')
    tick_signup(state)
    assert len(calls) == 1 and calls[0]['opted_in'] and calls[0]['before'] is None
    with state.session_factory() as session:
        receipt = session.get(GoogleVoiceInboundReceipt, 'accepted-name')
        marker = session.get(m.Policy, 'synthetic-capture:accepted-name')
        assert marker.value['source_message_id'] == receipt.result['source_message_id']
        assert session.get(m.Message, marker.value['source_message_id']).body == 'Judge Example'
    inbound(signup, 'Judge Example', 'accepted-name')
    tick_signup(state)
    assert len(calls) == 1
    inbound(signup, 'STOP', 'withdrawal')
    tick_signup(state)
    assert calls[-1]['route'] == 'stop' and not calls[-1]['opted_in']
    assert calls[-1]['before']['sms_opt_in'] is True
    with state.session_factory() as session:
        assert session.get(m.Policy, 'sms_opt_out:' + PHONE).value['value']
        marker = session.get(m.Policy, 'synthetic-capture:withdrawal')
        assert session.get(m.Message, marker.value['source_message_id']).body == 'STOP'


def test_gloo_failure_never_commits_a_capture_or_partial_incoming_identity(signup, monkeypatch):
    state, calls = capture_fixture(signup, monkeypatch)
    def unavailable(**_kwargs):
        raise GlooUnavailableError('Synthetic interpretation outage')
    state.gloo.create_response = unavailable
    inbound(signup, 'Judge Example', 'unavailable-name')
    tick_signup(state)
    assert not calls
    with state.session_factory() as session:
        assert session.get(m.Policy, 'synthetic-capture:unavailable-name') is None
        assert session.scalar(select(m.Volunteer).where(m.Volunteer.phone == PHONE)) is None
        assert session.get(GoogleVoiceInboundReceipt, 'unavailable-name').result['state'] == 'held_gloo'
        assert session.scalar(select(m.Message).where(m.Message.phone == PHONE, m.Message.direction == 'in')) is None
