"""Offline current-session Mac invitation and actual-name consent regression."""
from datetime import timedelta
from types import SimpleNamespace

import pytest
from sqlalchemy import select

from app.core.signup_copy import WELCOME, MAC_DEMO_WELCOME, compose_welcome
from app.db import models as m
from app.integrations.test_sessions import TestSession as RecipientSession
from app.sms.google_voice_provider import GoogleVoiceTestSession
from app.llm.gloo_client import GlooUnavailableError
from tests.test_concise_signup import PHONE, route
from tests.test_exact_signup_copy import ExactGloo, EXPECTED


@pytest.fixture
def mac_invitation(session, clock):
    selected = RecipientSession('a' * 32, clock.now()-timedelta(minutes=1),
                                clock.now()+timedelta(minutes=10))
    session.info['mac_test_session'] = selected
    session.add_all([
        m.Policy(key='signup_exact_copy:'+PHONE, value={'value':True}),
        m.Policy(key='mac_demo_invitation:'+PHONE, value={'value':True,'session_id':selected.id}),
    ])
    session.flush()
    return selected, ExactGloo()


def invitation(session, clock, selected, *, body=MAC_DEMO_WELCOME, status='submitted'):
    session.add(m.Message(phone=PHONE, direction='out', body=body, purpose='signup_reply',
        kind='ai', status=status, provider_sid=selected.outbound_prefix+'invitation', created_at=clock.now()))
    session.flush()


def test_new_invitation_composed_exactly_by_gloo_then_actual_name_opts_in(session, clock, provider, mac_invitation):
    selected, gloo = mac_invitation
    assert compose_welcome(session, clock, gloo, PHONE) == MAC_DEMO_WELCOME
    assert gloo.calls[-1]['exact_copy'] is True
    invitation(session, clock, selected)
    assert route(session, clock, provider, 'Alex Example', gloo).routed_to == 'onboarding_interests'
    person = session.scalar(select(m.Volunteer))
    assert person.sms_opt_in and person.preferences['consent_source'] == 'sms_name_reply_to_exact_invitation'
    assert provider.sent[-1].body == EXPECTED[1]
    assert 'STOP' not in provider.sent[-1].body
    assert route(session, clock, provider, 'Anything', gloo).routed_to == 'onboarding_availability'
    assert provider.sent[-1].body == EXPECTED[2] and 'STOP' not in provider.sent[-1].body
    assert session.scalar(select(m.Qualification)) is None


@pytest.mark.parametrize('status', ['queued','uncertain'])
def test_undelivered_invitation_cannot_grant_name_consent(session, clock, provider, mac_invitation, status):
    selected, gloo = mac_invitation
    invitation(session, clock, selected, status=status)
    route(session, clock, provider, 'Alex Example', gloo)
    assert not session.scalar(select(m.Volunteer)).sms_opt_in


def test_stop_revokes_new_invitation_before_name_reply(session, clock, provider, mac_invitation):
    selected, gloo = mac_invitation
    invitation(session, clock, selected)
    assert route(session, clock, provider, 'STOP', gloo).routed_to == 'stop'
    assert route(session, clock, provider, 'Alex Example', gloo).routed_to == 'stop'
    assert session.scalar(select(m.Volunteer)) is None and not provider.sent


def test_historical_welcome_still_proves_consent_with_new_option(session, clock, provider, mac_invitation):
    selected, gloo = mac_invitation
    invitation(session, clock, selected, body=WELCOME)
    assert route(session, clock, provider, 'Alex Example', gloo).routed_to == 'onboarding_interests'
    assert session.scalar(select(m.Volunteer)).sms_opt_in


@pytest.mark.parametrize('scope', ['off','wrong_session','other_phone','google'])
def test_new_copy_requires_exact_phone_and_current_mac_session(session, clock, mac_invitation, scope):
    selected, gloo = mac_invitation
    policy = session.get(m.Policy, 'mac_demo_invitation:'+PHONE)
    if scope == 'off': policy.value = {'value':False,'session_id':selected.id}
    if scope == 'wrong_session': policy.value = {'value':True,'session_id':'b'*32}
    if scope == 'other_phone': session.delete(policy)
    if scope == 'google': session.info['mac_test_session'] = GoogleVoiceTestSession(
        selected.id, selected.starts_at, selected.expires_at)
    session.flush()
    assert compose_welcome(session, clock, gloo, PHONE) == WELCOME


def test_new_invitation_without_current_scoped_policy_cannot_grant_consent(session, clock, provider, mac_invitation):
    selected, gloo = mac_invitation
    invitation(session, clock, selected)
    session.get(m.Policy, 'mac_demo_invitation:'+PHONE).value = {'value':True,'session_id':'b'*32}
    route(session, clock, provider, 'Alex Example', gloo)
    assert not session.scalar(select(m.Volunteer)).sms_opt_in


def test_changed_gloo_copy_cannot_silently_drop_stop(session, clock, mac_invitation):
    _, gloo = mac_invitation
    gloo.create_response = lambda **kwargs: SimpleNamespace(output_text=WELCOME)
    with pytest.raises(GlooUnavailableError): compose_welcome(session, clock, gloo, PHONE)
