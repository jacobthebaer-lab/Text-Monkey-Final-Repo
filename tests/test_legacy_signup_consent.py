"""Legacy opt-in requires real recorded delivery and sender action; synthetic only."""
import json
from datetime import datetime, timedelta
from types import SimpleNamespace

import pytest
from sqlalchemy import select

from app.agents.fill_agent import FillContext
from app.config import Settings
from app.core.inbound import handle_inbound
from app.core.send_gate import SendGate
from app.core.signup import finish_signup
from app.core.signup_copy import LEGACY_WELCOME
from app.db import models as m
from app.integrations.test_sessions import TestSession as SessionScope
from app.llm.parser import ParsedMessage

PHONE = '+12025550190'


class NoGloo:
    def create_response(self, **kwargs):
        pytest.fail('Consent validation and silent completion must not call a model')


class IdentityGloo:
    settings = Settings(gloo_signup_replies=True)
    def __init__(self):
        self.calls = []
    def create_response(self, **kwargs):
        facts = json.loads(kwargs['input'])
        self.calls.append(facts)
        if isinstance(facts, dict):
            return SimpleNamespace(output_text=facts['approved_message'])
        return SimpleNamespace(output_text=json.dumps({'signup': facts[-1]['body'] != 'Hello',
            'first_name': 'Alex', 'last_name': 'Example', 'identity_reply': True,
            'consent': True, 'sms_opt_in': True}))


def inbound(session, clock, provider, phone, body, gloo=None):
    return handle_inbound(session, clock, provider, phone, body,
        lambda _: ParsedMessage(intent='other', confidence=1),
        ctx=FillContext(session, clock, provider, gloo or NoGloo()), allow_signup=True)


def pending(make_volunteer):
    return make_volunteer('Alex Example', opt_in=False, status='inactive',
                          prefs={'signup_source': 'sms', 'consent_pending': True})


def disclosure(session, clock, phone, *, volunteer_id=None, status='sent', body=LEGACY_WELCOME):
    selected = session.info.get('mac_test_session')
    row = m.Message(phone=phone, volunteer_id=volunteer_id, direction='out', body=body,
        purpose='signup_reply', kind='ai', status=status, created_at=clock.now()-timedelta(seconds=30),
        provider_sid=selected.outbound_prefix+'legacy' if selected else 'MOCK-LEGACY')
    session.add(row); session.flush()
    return row


def assert_inactive(session, person, provider):
    assert not person.sms_opt_in and person.status == 'inactive' and person.preferences['consent_pending']
    assert 'consent_source' not in person.preferences and not provider.sent
    assert not person.is_coordinator and not person.is_pastor
    assert session.scalar(select(m.Assignment)) is None and session.scalar(select(m.Qualification)) is None


@pytest.mark.parametrize('body', ['YES', 'Y', 'START', 'UNSTOP'])
def test_imported_pending_flags_are_not_consent(session, clock, provider, make_volunteer, body):
    person = pending(make_volunteer)
    result = inbound(session, clock, provider, person.phone, body)
    assert result.routed_to == ('consent_required' if body in {'START', 'UNSTOP'} else 'signup_consent_pending')
    assert_inactive(session, person, provider)


@pytest.mark.parametrize('body', ['YES', 'START'])
def test_real_name_with_blocked_transport_cannot_activate_later(session, clock, provider, body):
    provider.allows = lambda phone: False
    gloo = IdentityGloo()
    assert inbound(session, clock, provider, PHONE, 'Alex Example', gloo).routed_to == 'signup_consent_pending'
    person = session.scalar(select(m.Volunteer))
    assert session.scalar(select(m.Message).where(m.Message.direction == 'out')) is None
    calls = len(gloo.calls)
    result = inbound(session, clock, provider, PHONE, body, gloo)
    assert result.routed_to in {'signup_consent_pending', 'consent_required'}
    assert len(gloo.calls) == calls
    assert_inactive(session, person, provider)


@pytest.mark.parametrize('body', ['YES', 'Y'])
@pytest.mark.parametrize('status', ['sent', 'submitted'])
@pytest.mark.parametrize('copy', ['welcome', 'personalized', 'short_after_disclosure'])
def test_delivered_disclosure_and_real_yes_activate_silently(session, clock, provider, make_volunteer, body, status, copy):
    person = pending(make_volunteer)
    first = disclosure(session, clock, person.phone, volunteer_id=person.id, status=status)
    if copy != 'welcome':
        text = 'Thanks, Alex! Reply YES to receive volunteer scheduling texts from Text Monkey.'
        if copy == 'personalized':
            text += ' Message frequency varies; message/data rates may apply.'
            first.body = 'An unrelated earlier signup prompt.'
        text += ' Reply STOP to stop or HELP for help.'
        followup = disclosure(session, clock, person.phone, volunteer_id=person.id, status=status, body=text+' 🐵')
        if copy == 'personalized':
            first = followup
    result = inbound(session, clock, provider, person.phone, body)
    assert result.routed_to == 'signup_complete' and person.sms_opt_in and person.status == 'active'
    reply = session.scalar(select(m.Message).where(m.Message.direction == 'in').order_by(m.Message.id.desc()))
    assert person.preferences['consent_source'] == 'sms_reply'
    assert person.preferences['consent_disclosure_message_id'] == first.id
    assert person.preferences['consent_reply_message_id'] == reply.id
    assert datetime.fromisoformat(person.preferences['consent_at']) == reply.created_at
    assert not provider.sent and not person.is_coordinator and not person.is_pastor
    assert session.scalar(select(m.Assignment)) is None and session.scalar(select(m.Qualification)) is None


@pytest.mark.parametrize('fault', ['missing', 'queued', 'failed', 'uncertain', 'foreign_phone',
    'foreign_volunteer', 'wrong_purpose', 'wrong_source', 'stale', 'future', 'missing_rates',
    'short_without_disclosure', 'withdrawal'])
def test_pending_yes_rejects_invalid_disclosure(session, clock, provider, make_volunteer, fault):
    person = pending(make_volunteer)
    invitation = None if fault == 'missing' else disclosure(session, clock, person.phone, volunteer_id=person.id)
    if fault in {'queued', 'failed', 'uncertain'}: invitation.status = fault
    elif fault == 'foreign_phone': invitation.phone = '+12025550199'
    elif fault == 'foreign_volunteer': invitation.volunteer_id = make_volunteer().id
    elif fault == 'wrong_purpose': invitation.purpose = 'manual'
    elif fault == 'wrong_source': invitation.kind = 'imported'
    elif fault == 'stale': invitation.created_at = clock.now()-timedelta(hours=25)
    elif fault == 'future': invitation.created_at = clock.now()+timedelta(seconds=1)
    elif fault == 'missing_rates': invitation.body = LEGACY_WELCOME.replace('message/data rates may apply.', '')
    elif fault == 'short_without_disclosure':
        invitation.body = 'Thanks, Alex! Reply YES to receive volunteer scheduling texts from Text Monkey. Reply STOP to stop or HELP for help.'
    elif fault == 'withdrawal':
        session.add(m.Message(phone=person.phone, volunteer_id=person.id, direction='in',
            body='Please stop texting me', kind='inbound', status='received', created_at=clock.now()-timedelta(seconds=15)))
    session.flush()
    assert inbound(session, clock, provider, person.phone, 'YES').routed_to == 'signup_consent_pending'
    assert_inactive(session, person, provider)


@pytest.mark.parametrize('body', ['Alex Example YES', 'YES Alex Example', 'JOIN Alex Example, YES'])
def test_actual_legacy_welcome_then_name_yes_uses_recorded_sources(session, clock, provider, body):
    gloo = IdentityGloo()
    assert inbound(session, clock, provider, PHONE, 'Hello', gloo).routed_to == 'signup_invitation'
    invitation = session.scalar(select(m.Message).where(m.Message.direction == 'out'))
    assert invitation.status == 'sent'
    sent = len(provider.sent)
    assert inbound(session, clock, provider, PHONE, body, gloo).routed_to == 'signup_complete'
    person = session.scalar(select(m.Volunteer))
    assert person.sms_opt_in and person.preferences['consent_source'] == 'sms_name_and_yes'
    assert person.preferences['consent_disclosure_message_id'] == invitation.id
    assert len(provider.sent) == sent == 1 and len(gloo.calls) == 3


@pytest.mark.parametrize('fault', ['missing', 'queued', 'failed', 'foreign_phone', 'foreign_contact',
    'foreign_session', 'stale', 'wrong_purpose'])
def test_model_claim_and_actual_name_yes_cannot_replace_delivery(session, clock, provider, make_volunteer, fault):
    if fault == 'foreign_session':
        session.info['mac_test_session'] = SessionScope('a'*32, clock.now()-timedelta(minutes=1), clock.now()+timedelta(minutes=59))
    if fault != 'missing':
        invitation = disclosure(session, clock, PHONE)
        if fault in {'queued', 'failed'}: invitation.status = fault
        elif fault == 'foreign_phone': invitation.phone = '+12025550199'
        elif fault == 'foreign_contact': invitation.volunteer_id = make_volunteer().id
        elif fault == 'foreign_session': invitation.provider_sid = 'MAC'+'b'*32+':legacy'
        elif fault == 'stale': invitation.created_at = clock.now()-timedelta(hours=25)
        elif fault == 'wrong_purpose': invitation.purpose = 'manual'
        session.flush()
    gloo = IdentityGloo()
    assert inbound(session, clock, provider, PHONE, 'Alex Example YES', gloo).routed_to == 'signup_consent_pending'
    assert len(gloo.calls) == 1 and isinstance(gloo.calls[0], list)
    assert_inactive(session, session.scalar(select(m.Volunteer).where(m.Volunteer.phone == PHONE)), provider)


@pytest.mark.parametrize('fault', ['missing_id', 'different_body', 'different_phone', 'different_volunteer',
    'wrong_direction', 'wrong_status', 'wrong_source', 'old_reply', 'future_reply', 'reversed_ids'])
def test_finish_requires_the_actual_current_inbound_record(session, clock, provider, make_volunteer, fault):
    person = pending(make_volunteer)
    if fault != 'reversed_ids':
        disclosure(session, clock, person.phone, volunteer_id=person.id)
    reply = m.Message(phone=person.phone, volunteer_id=person.id, direction='in', body='YES',
                      kind='inbound', status='received', created_at=clock.now())
    session.add(reply); session.flush()
    if fault == 'reversed_ids': disclosure(session, clock, person.phone, volunteer_id=person.id)
    elif fault == 'different_body': reply.body = 'not sure'
    elif fault == 'different_phone': reply.phone = '+12025550199'
    elif fault == 'different_volunteer': reply.volunteer_id = make_volunteer().id
    elif fault == 'wrong_direction': reply.direction = 'out'
    elif fault == 'wrong_status': reply.status = 'queued'
    elif fault == 'wrong_source': reply.kind = 'imported'
    elif fault == 'old_reply': reply.created_at = clock.now()-timedelta(minutes=11)
    elif fault == 'future_reply': reply.created_at = clock.now()+timedelta(seconds=1)
    session.flush()
    gate = SendGate(session, clock, provider)
    gate.reply_to_message_id = None if fault == 'missing_id' else reply.id
    assert finish_signup(session, clock, gate, person, 'YES', gloo=NoGloo()) == 'signup_consent_pending'
    assert_inactive(session, person, provider)


@pytest.mark.parametrize('fault', [None, 'foreign_invitation', 'expired_session', 'foreign_reply'])
def test_native_legacy_consent_is_bound_to_current_session(session, clock, provider, make_volunteer, fault):
    person = pending(make_volunteer)
    selected = SessionScope('a'*32, clock.now()-timedelta(minutes=1), clock.now()+timedelta(minutes=59))
    session.info['mac_test_session'] = selected
    invitation = disclosure(session, clock, person.phone, volunteer_id=person.id, status='submitted')
    if fault == 'foreign_invitation': invitation.provider_sid = 'MAC'+'b'*32+':legacy'
    elif fault == 'expired_session':
        session.info['mac_test_session'] = SessionScope(selected.id, clock.now()-timedelta(hours=1), clock.now())
    session.flush()
    if fault == 'foreign_reply':
        reply = m.Message(phone=person.phone, volunteer_id=person.id, direction='in', body='YES', kind='mac_test_in',
            purpose='test:'+'b'*32, provider_sid=selected.outbound_prefix+'forged', status='received', created_at=clock.now())
        session.add(reply); session.flush()
        gate = SendGate(session, clock, provider); gate.reply_to_message_id = reply.id
        route = finish_signup(session, clock, gate, person, 'YES', gloo=NoGloo())
    else:
        route = inbound(session, clock, provider, person.phone, 'YES').routed_to
    assert route == ('signup_complete' if fault is None else 'signup_consent_pending')
    if fault is None:
        assert person.sms_opt_in and person.preferences['consent_session_id'] == selected.id
    else:
        assert_inactive(session, person, provider)
    assert not provider.sent


def test_pending_start_keeps_established_consent_restart_guard(session, clock, provider, make_volunteer):
    person = pending(make_volunteer)
    invitation = disclosure(session, clock, person.phone, volunteer_id=person.id)
    yes = m.Message(phone=person.phone, volunteer_id=person.id, direction='in', body='YES', kind='inbound',
                    status='received', created_at=clock.now()-timedelta(seconds=1))
    session.add(yes); session.flush()
    person.preferences = {**person.preferences, 'consent_source': 'sms_reply', 'consent_at': yes.created_at.isoformat()}
    session.add(m.Policy(key='sms_opt_out:'+person.phone, value={'value': True})); session.flush()
    assert inbound(session, clock, provider, person.phone, 'START').routed_to == 'start'
    assert person.sms_opt_in and session.get(m.Policy, 'sms_opt_out:'+person.phone) is None
    # START is a restart, not new signup provenance or silent role/record grants.
    assert person.preferences['consent_at'] == yes.created_at.isoformat()
    assert person.preferences['consent_pending'] and person.status == 'inactive'
    assert not provider.sent
