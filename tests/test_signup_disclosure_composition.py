"""Consent copy and provenance regressions, synthetic model and delivery only."""
import json
from datetime import datetime
from dataclasses import replace
from types import SimpleNamespace

import pytest
from sqlalchemy import select
from fastapi.testclient import TestClient

from app.core import confirmations
from app.core.send_gate import SendGate
from app.core.signup_copy import LEGACY_WELCOME, LEGACY_WELCOME_REQUIRED, compose_welcome
from app.core.signup_responder import compose_signup_reply
from app.db import models as m
from app.llm.gloo_client import GlooUnavailableError
from app.integrations.mac_models import MacInboundReceipt
from tests.test_legacy_signup_consent import IdentityGloo, disclosure, inbound, pending, PHONE
from tests.test_mac_messages import mac_app, incoming, post


PERSONALIZED = ('Thanks, Alex! Reply YES to receive volunteer scheduling texts from Text Monkey.'
                ' Message frequency varies; message/data rates may apply.'
                ' Reply STOP to stop or HELP for help.')
SHORT = PERSONALIZED.replace(' Message frequency varies; message/data rates may apply.', '')


class PunctuationGloo(IdentityGloo):
    def __init__(self, *, always_change=False):
        super().__init__()
        self.always_change = always_change

    def create_response(self, **kwargs):
        facts = json.loads(kwargs['input'])
        if isinstance(facts, dict) and 'approved_message' in facts:
            self.calls.append(facts)
            text = facts['approved_message']
            if self.always_change or not facts['exact_copy']:
                text = text.replace('varies;', 'varies,').replace('Thanks, Alex!', 'Thanks Alex!')
            return SimpleNamespace(output_text=text)
        return super().create_response(**kwargs)


@pytest.mark.parametrize('text', [LEGACY_WELCOME, PERSONALIZED, SHORT])
def test_code_owned_disclosures_request_exact_gloo_copy_before_history_filters(session, clock, text):
    # An earlier message must not strip commands or weaken the consent copy.
    session.add(m.Message(phone=PHONE, direction='out', purpose='signup_reply', body='Earlier prompt.',
                         kind='ai', status='sent', created_at=clock.now()))
    session.flush()
    gloo = PunctuationGloo()
    rendered = compose_signup_reply(session, clock, gloo, text, phone=PHONE,
                                    signup_conversation=True, require_gloo=True)
    assert rendered == text
    assert gloo.calls[-1]['exact_copy'] is True
    assert gloo.calls[-1]['approved_message'] == text
    assert gloo.calls[-1]['allowed_emojis'] == gloo.calls[-1]['allowed_monkey_emojis'] == []


@pytest.mark.parametrize('text', [LEGACY_WELCOME, PERSONALIZED])
def test_changed_disclosure_is_held_before_delivery_or_review(session, clock, provider, text):
    session.info[confirmations.MODE_KEY] = True
    gloo = PunctuationGloo(always_change=True)
    gate = SendGate(session, clock, provider)
    required = LEGACY_WELCOME_REQUIRED if text == LEGACY_WELCOME else (
        'Reply YES', 'Message frequency varies', 'message/data rates may apply', 'STOP', 'HELP')
    with pytest.raises(GlooUnavailableError):
        gate.send(body=compose_signup_reply(session, clock, gloo, text, required,
                                           phone=PHONE, signup_conversation=True, require_gloo=True),
                  purpose='signup_reply', phone=PHONE)
    assert not provider.sent and session.scalar(select(m.Approval)) is None
    assert session.scalar(select(m.Message).where(m.Message.direction == 'out')) is None


@pytest.mark.parametrize('exact_review', [False, True])
def test_new_person_yes_uses_exact_composed_disclosure_and_actual_sender_proof(
    session, clock, provider, exact_review
):
    session.info[confirmations.MODE_KEY] = exact_review
    gloo = PunctuationGloo()
    assert inbound(session, clock, provider, PHONE, 'Alex Example', gloo).routed_to == 'signup_consent_pending'
    person = session.scalar(select(m.Volunteer))
    assert person and not person.sms_opt_in
    if exact_review:
        review = session.scalar(select(m.Approval).where(m.Approval.kind == 'confirm_text'))
        assert review.payload['body'] == PERSONALIZED and not provider.sent
        body, content_hash = review.payload['body'], review.payload['content_hash']
        confirmations.decide(session, SendGate(session, clock, provider), review, approve=True,
                             actor='fictional-admin@example.test', expected=content_hash, now=clock.now())
        assert review.payload['body'] == body and review.payload['content_hash'] == content_hash
    invitation = session.scalar(select(m.Message).where(m.Message.direction == 'out'))
    assert invitation.body == PERSONALIZED and invitation.status == 'sent'
    assert provider.sent[0].body == invitation.body
    assert inbound(session, clock, provider, PHONE, 'YES', gloo).routed_to == 'signup_complete'
    reply = session.scalar(select(m.Message).where(m.Message.direction == 'in').order_by(m.Message.id.desc()))
    assert person.sms_opt_in and person.preferences['consent_disclosure_message_id'] == invitation.id
    assert person.preferences['consent_reply_message_id'] == reply.id
    assert datetime.fromisoformat(person.preferences['consent_at']) == reply.created_at
    assert len(provider.sent) == 1 and len([x for x in gloo.calls if isinstance(x, dict)]) == 1


def test_previously_delivered_malformed_disclosure_stays_unchanged_and_cannot_grant_consent(
    session, clock, provider, make_volunteer
):
    person = pending(make_volunteer)
    malformed = PERSONALIZED.replace('varies;', 'varies,')
    invitation = disclosure(session, clock, person.phone, volunteer_id=person.id, body=malformed)
    assert inbound(session, clock, provider, person.phone, 'YES').routed_to == 'signup_consent_pending'
    assert not person.sms_opt_in and person.preferences['consent_pending']
    assert invitation.body == malformed and invitation.status == 'sent'
    assert 'consent_disclosure_message_id' not in person.preferences and not provider.sent


def test_welcome_requests_exact_disclosure_without_alternate_provider_or_template(session, clock):
    gloo = PunctuationGloo()
    assert compose_welcome(session, clock, gloo, PHONE) == LEGACY_WELCOME
    assert len(gloo.calls) == 1 and gloo.calls[0]['exact_copy'] is True
    with pytest.raises(GlooUnavailableError):
        compose_welcome(session, clock, None, PHONE)


def test_mac_malformed_disclosure_rolls_back_and_same_inbound_can_retry(mac_app):
    app = mac_app
    data = incoming(guid='synthetic-disclosure-retry', body='Alex Example')
    app.state.settings = replace(app.state.settings, allow_text_signup=True, gloo_signup_replies=True)
    gloo = PunctuationGloo(always_change=True)
    gloo.settings = app.state.settings
    app.state.gloo = gloo
    with app.state.session_factory() as session:
        session.delete(session.scalar(select(m.Volunteer).where(m.Volunteer.phone == data['phone'])))
        session.commit()
    with TestClient(app, raise_server_exceptions=False) as client:
        assert post(client, '/mac/inbound', data).status_code == 500
        with app.state.session_factory() as session:
            assert session.get(MacInboundReceipt, data['guid']) is None
            assert session.scalar(select(m.Message)) is None
            assert session.scalar(select(m.Volunteer).where(m.Volunteer.phone == data['phone'])) is None
            assert session.scalar(select(m.Approval)) is None
        gloo.always_change = False
        retry = post(client, '/mac/inbound', data)
        assert retry.status_code == 200, retry.text
        assert retry.json()['intent'] == 'signup_consent_pending' and not retry.json()['duplicate']
        assert post(client, '/mac/inbound', data).json()['duplicate'] is True
    with app.state.session_factory() as session:
        person = session.scalar(select(m.Volunteer).where(m.Volunteer.phone == data['phone']))
        assert person.preferences['consent_pending'] and not person.sms_opt_in
        messages = session.scalars(select(m.Message).where(m.Message.direction == 'out')).all()
        assert len(messages) == 1 and messages[0].body == PERSONALIZED and messages[0].status == 'queued'
        assert session.get(MacInboundReceipt, data['guid']) is not None
