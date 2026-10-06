"""Sender-reported names supply profiles; admin names never authenticate senders."""
import json
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select

from app.db import models as m
from app.integrations.google_voice_demo import RECIPIENT_KEY, registered_consent_provenance
from app.integrations.google_voice_signup import tick_signup
from app.llm.gloo_client import GlooUnavailableError
from tests.test_google_voice_signup import signup, enable, inbound, PHONE, SignupGloo
from tests.test_google_voice_demo import demo, dynamic_demo, register

EXPECTED = {'first_name': 'Judge', 'last_name': 'Example'}


class NameGloo(SignupGloo):
    def create_response(self, **kwargs):
        data = json.loads(kwargs['input'])
        if isinstance(data, list):
            self.calls.append(kwargs)
            body = ' '.join(data[-1]['body'].split())
            if body.startswith('My last name is '):
                first, last = None, body.removeprefix('My last name is ')
            elif body.startswith('My name is '):
                names = body.removeprefix('My name is ').split()
                first, last = names[0] if names else None, ' '.join(names[1:]) or None
            else:
                names = body.split()
                first, last = names[0] if names else None, ' '.join(names[1:]) or None
            return SimpleNamespace(output_text=json.dumps({'signup': True, 'identity_reply': True,
                'first_name': first, 'last_name': last}), usage=None)
        return super().create_response(**kwargs)


def begin(signup, legacy_expected=None):
    signup.state.gloo = NameGloo(signup.state.settings)
    enable(signup)
    response = TestClient(signup).post('/api/cloud-texting/demo/recipients', json={
        'phone': PHONE})
    assert response.status_code == 200, response.text
    if legacy_expected is not None:
        with signup.state.session_factory() as session:
            row = session.get(m.Policy, RECIPIENT_KEY + PHONE)
            row.value = {**row.value, 'expected_name': legacy_expected}
            session.commit()
    tick_signup(signup.state)


def person(signup):
    with signup.state.session_factory() as session:
        return session.scalar(select(m.Volunteer).where(m.Volunteer.phone == PHONE))


def recovery_calls(signup):
    return [json.loads(call['input'])['recovery'] for call in signup.state.gloo.calls
        if isinstance(json.loads(call['input']), dict) and 'recovery' in json.loads(call['input'])]


@pytest.mark.parametrize('reported', ['Other Example', 'Judge Different', 'Other Different'])
def test_self_reported_name_accepted_despite_legacy_admin_expectation(signup, reported):
    begin(signup, EXPECTED)
    inbound(signup, reported, 'reported')
    tick_signup(signup.state)
    volunteer = person(signup)
    assert volunteer.name == reported and volunteer.sms_opt_in
    assert volunteer.preferences['onboarding_stage'] == 'interests'
    with signup.state.session_factory() as session:
        assert registered_consent_provenance(session, session.get(m.Volunteer, volunteer.id))
        row = session.get(m.Policy, RECIPIENT_KEY + PHONE)
        assert row.value['name'] == reported
        row.value = {**row.value, 'expected_name': {'first_name':'Unrelated','last_name':'Historical'}}
        assert registered_consent_provenance(session, session.get(m.Volunteer, volunteer.id))
    before = len(signup.state.google_voice_connector.calls)
    inbound(signup, reported, 'reported')
    tick_signup(signup.state)
    assert len(signup.state.google_voice_connector.calls) == before == 2
    assert sum('Text STOP to stop.' in call['body'] for call in signup.state.google_voice_connector.calls) == 1
    assert all(chr(0x2014) not in call['body'] for call in signup.state.google_voice_connector.calls)


def test_partial_names_complete_from_actual_reply_parts(signup):
    begin(signup, EXPECTED)
    inbound(signup, 'My name is Different', 'first')
    tick_signup(signup.state)
    assert person(signup) is None
    with signup.state.session_factory() as session:
        assert session.get(m.Policy, 'signup_identity_draft:' + PHONE).value['first_name'] == 'Different'
    inbound(signup, 'My last name is Person', 'last')
    tick_signup(signup.state)
    assert person(signup).name == 'Different Person' and person(signup).sms_opt_in
    with signup.state.session_factory() as session:
        assert registered_consent_provenance(session, session.get(m.Volunteer, person(signup).id))


@pytest.mark.parametrize('malformed', ['123 456', 'Judge 123', '... !!!', 'My name is', 'I am'])
def test_malformed_or_missing_names_do_not_create_profile(signup, malformed):
    begin(signup)
    inbound(signup, malformed, 'malformed')
    tick_signup(signup.state)
    assert person(signup) is None
    assert len(signup.state.google_voice_connector.calls) == 2
    assert "What's your first and last name?" in signup.state.google_voice_connector.calls[-1]['body']
    assert recovery_calls(signup)


def test_malformed_clarification_dedupes_and_original_reply_is_rechecked(signup):
    begin(signup)
    inbound(signup, '123 456', 'malformed')
    tick_signup(signup.state)
    before = len(signup.state.google_voice_connector.calls)
    inbound(signup, '123 456', 'malformed')
    tick_signup(signup.state)
    assert len(signup.state.google_voice_connector.calls) == before == 2
    from app.core import outbound_conversation
    with signup.state.session_factory() as session:
        rows = session.scalars(select(m.Notification).where(m.Notification.key.like('conversation-message:%'))).all()
        notification = next(row for row in rows if row.detail.get('name_recovery'))
        proof = notification.detail['name_recovery']
        session.get(m.Message, proof['reply_id']).body = 'Changed input'
        assert outbound_conversation.problem(session, purpose='signup_reply', volunteer=None,
            phone=PHONE, body='', now=signup.state.clock.now(), meta=notification.detail)


def test_number_only_registration_rejects_retired_expected_api_field(signup):
    enable(signup)
    client = TestClient(signup)
    assert client.post('/api/cloud-texting/demo/recipients', json={'phone': PHONE}).status_code == 200
    with signup.state.session_factory() as session:
        assert 'expected_name' not in session.get(m.Policy, RECIPIENT_KEY + PHONE).value
        assert session.scalar(select(m.Volunteer).where(m.Volunteer.phone == PHONE)) is None
    assert client.post('/api/cloud-texting/demo/recipients', json={'phone':PHONE,'expected_name':EXPECTED}).status_code == 400


def test_gloo_cannot_invent_name_not_in_sender_reply(signup):
    begin(signup)
    original = signup.state.gloo.create_response
    def invented(**kwargs):
        if isinstance(json.loads(kwargs['input']), list):
            return SimpleNamespace(output_text=json.dumps({'signup':True,'identity_reply':True,**EXPECTED}),usage=None)
        return original(**kwargs)
    signup.state.gloo.create_response = invented
    inbound(signup, 'Other Different', 'invented')
    tick_signup(signup.state)
    assert person(signup) is None and recovery_calls(signup)


def test_gloo_unavailable_partial_recovery_has_no_profile_or_fallback(signup):
    begin(signup)
    original = signup.state.gloo.create_response
    def unavailable(**kwargs):
        data = json.loads(kwargs['input'])
        if isinstance(data,dict) and 'recovery' in data:
            raise GlooUnavailableError('Synthetic unavailable recovery')
        return original(**kwargs)
    signup.state.gloo.create_response = unavailable
    inbound(signup, 'My name is Judge', 'partial-outage')
    tick_signup(signup.state)
    assert person(signup) is None and len(signup.state.google_voice_connector.calls) == 1


def test_stop_precedes_name_interpretation_and_registration_cannot_clear_it(signup):
    begin(signup, EXPECTED)
    inbound(signup, 'STOP', 'stop')
    tick_signup(signup.state)
    before = len(signup.state.gloo.calls)
    inbound(signup, 'Other Different', 'after-stop')
    tick_signup(signup.state)
    assert person(signup) is None and len(signup.state.gloo.calls) == before
    assert register(signup).status_code == 409


def test_self_reported_profile_and_preferences_do_not_grant_qualifications(signup):
    begin(signup, EXPECTED)
    for index, body in enumerate(['Other Person', 'Greeter', 'Sunday morning twice per month']):
        inbound(signup, body, 'signup-' + str(index))
        tick_signup(signup.state)
    with signup.state.session_factory() as session:
        volunteer = session.get(m.Volunteer, person(signup).id)
        assert volunteer.name == 'Other Person' and volunteer.sms_opt_in
        assert volunteer.preferences['onboarding_stage'] == 'complete'
        assert volunteer.preferences['interested_roles'] == ['Greeter']
        assert volunteer.preferences['max_per_month'] == 2 and not volunteer.qualifications
        assert registered_consent_provenance(session, volunteer)


def test_stop_and_original_actual_name_proof_remain_required(signup):
    begin(signup, EXPECTED)
    inbound(signup, 'Other Person', 'name')
    tick_signup(signup.state)
    inbound(signup, 'STOP', 'later-stop')
    tick_signup(signup.state)
    with signup.state.session_factory() as session:
        volunteer = session.get(m.Volunteer, person(signup).id)
        assert not registered_consent_provenance(session, volunteer)
        assert registered_consent_provenance(session, volunteer, require_current_consent=False)
        proof = session.get(m.Policy, RECIPIENT_KEY + PHONE).value['consent']
        session.get(m.Message, proof['reply_message_id']).body = 'Fabricated Person'
        assert not registered_consent_provenance(session, volunteer, require_current_consent=False)


def test_imported_approval_cannot_replace_actual_google_reply_name(signup):
    from app.core.signup import approve_signup
    begin(signup)
    with signup.state.session_factory() as session:
        with pytest.raises(ValueError, match='original sender-name evidence'):
            approve_signup(session,signup.state.clock,SimpleNamespace(payload={'phone':PHONE,**EXPECTED}))
        assert session.scalar(select(m.Volunteer).where(m.Volunteer.phone == PHONE)) is None
