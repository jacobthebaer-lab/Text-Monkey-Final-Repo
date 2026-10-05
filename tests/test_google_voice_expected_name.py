"""Expected-name restrictions use actual sender replies, never admin-imported names."""
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
                first, last = names[0], ' '.join(names[1:]) or None
            else:
                names = body.split()
                first, last = names[0], ' '.join(names[1:]) or None
            return SimpleNamespace(output_text=json.dumps({'signup': True, 'identity_reply': True,
                'first_name': first, 'last_name': last}), usage=None)
        return super().create_response(**kwargs)


def begin(signup, expected=EXPECTED):
    signup.state.gloo = NameGloo(signup.state.settings)
    enable(signup)
    response = TestClient(signup).post('/api/cloud-texting/demo/recipients', json={
        'phone': PHONE, 'name': 'Display only', 'expected_name': expected})
    assert response.status_code == 200, response.text
    tick_signup(signup.state)


def person(signup):
    with signup.state.session_factory() as session:
        return session.scalar(select(m.Volunteer).where(m.Volunteer.phone == PHONE))


def recovery_calls(signup):
    return [json.loads(call['input'])['recovery'] for call in signup.state.gloo.calls
        if isinstance(json.loads(call['input']), dict) and 'recovery' in json.loads(call['input'])]


@pytest.mark.parametrize('wrong', ['Other Example', 'Judge Different', 'Other Different'])
def test_wrong_first_last_or_both_reprompt_once_then_exact_name_advances(signup, wrong):
    begin(signup)
    inbound(signup, wrong, 'wrong')
    tick_signup(signup.state)
    assert person(signup) is None
    assert len(signup.state.google_voice_connector.calls) == 2
    assert recovery_calls(signup)[-1]['actual_reply'] == wrong
    with signup.state.session_factory() as session:
        assert session.get(m.Policy, 'signup_identity_draft:' + PHONE) is None
        registration = session.get(m.Policy, RECIPIENT_KEY + PHONE)
        assert registration.value['consent_state'] == 'awaiting_name'
        assert registration.value['expected_name'] == EXPECTED
    before = len(signup.state.gloo.calls)
    inbound(signup, wrong, 'wrong')  # Same transport ID is not a new correction source.
    tick_signup(signup.state)
    assert len(signup.state.gloo.calls) == before
    assert len(signup.state.google_voice_connector.calls) == 2
    inbound(signup, 'My name is  judge   EXAMPLE', 'correct')
    tick_signup(signup.state)
    assert person(signup).sms_opt_in
    assert person(signup).preferences['onboarding_stage'] == 'interests'
    with signup.state.session_factory() as session:
        assert registered_consent_provenance(session, session.get(m.Volunteer, person(signup).id))


def test_two_rejected_full_names_cannot_stitch_matching_fragments_and_each_gets_gloo(signup):
    begin(signup)
    for index, text in enumerate(['Other Example', 'Judge Different', 'Other Different', 'Judge Different']):
        inbound(signup, text, 'wrong-' + str(index))
        tick_signup(signup.state)
        assert person(signup) is None
        with signup.state.session_factory() as session:
            assert session.get(m.Policy, 'signup_identity_draft:' + PHONE) is None
    assert len(recovery_calls(signup)) == 4
    assert len(signup.state.google_voice_connector.calls) == 5


def test_valid_partial_then_wrong_missing_part_requires_actual_correction(signup):
    begin(signup)
    for index, text in enumerate(['My name is Judge', 'My last name is Different']):
        inbound(signup, text, 'partial-' + str(index))
        tick_signup(signup.state)
        assert person(signup) is None
    with signup.state.session_factory() as session:
        draft = session.get(m.Policy, 'signup_identity_draft:' + PHONE).value
        assert draft['first_name'] == 'Judge' and 'last_name' not in draft
    inbound(signup, 'My last name is Example', 'correct-last')
    tick_signup(signup.state)
    assert person(signup).sms_opt_in
    with signup.state.session_factory() as session:
        volunteer = session.get(m.Volunteer, person(signup).id)
        assert registered_consent_provenance(session, volunteer)
        row = session.get(m.Policy, RECIPIENT_KEY + PHONE)
        row.value = {**row.value, 'expected_name': {'first_name':'Wrong','last_name':'Example'}}
        assert not registered_consent_provenance(session, volunteer)


def test_stop_wins_before_wrong_name_composition_and_registration_cannot_reset(signup):
    begin(signup)
    inbound(signup, 'STOP', 'stop')
    tick_signup(signup.state)
    calls = len(signup.state.gloo.calls)
    inbound(signup, 'Other Example', 'after-stop')
    tick_signup(signup.state)
    assert person(signup) is None
    assert len(signup.state.gloo.calls) == calls
    assert len(signup.state.google_voice_connector.calls) == 1
    assert register(signup).status_code == 409


def test_gloo_unavailable_correction_holds_without_creating_profile_or_fallback(signup):
    begin(signup)
    original = signup.state.gloo.create_response
    def fail_recovery(**kwargs):
        data = json.loads(kwargs['input'])
        if isinstance(data, dict) and 'recovery' in data:
            raise GlooUnavailableError('Synthetic unavailable correction')
        return original(**kwargs)
    signup.state.gloo.create_response = fail_recovery
    inbound(signup, 'Other Example', 'wrong-outage')
    tick_signup(signup.state)
    assert person(signup) is None
    assert len(signup.state.google_voice_connector.calls) == 1
    with signup.state.session_factory() as session:
        assert session.get(m.Policy, RECIPIENT_KEY + PHONE).value['consent_state'] == 'awaiting_name'


def test_registration_validation_legacy_names_and_identity_replacement_hold(signup):
    enable(signup)
    client = TestClient(signup)
    for invalid in [None, {'first_name':'Judge'}, {'first_name':'','last_name':'Example'},
                    {'first_name':'123','last_name':'Example'}]:
        response = client.post('/api/cloud-texting/demo/recipients', json={'phone':PHONE,'expected_name':invalid})
        assert response.status_code == 400
    assert register(signup).status_code == 200
    with signup.state.session_factory() as session:
        assert 'expected_name' not in session.get(m.Policy, RECIPIENT_KEY + PHONE).value
    response = client.post('/api/cloud-texting/demo/recipients', json={'phone':PHONE,'expected_name':EXPECTED})
    assert response.status_code == 409


def test_expected_name_does_not_substitute_for_actual_sender_text_or_invent_profile(signup):
    begin(signup)
    original = signup.state.gloo.create_response
    def invented_names(**kwargs):
        data = json.loads(kwargs['input'])
        if isinstance(data, list):
            return SimpleNamespace(output_text=json.dumps({'signup':True,'identity_reply':True, **EXPECTED}), usage=None)
        return original(**kwargs)
    signup.state.gloo.create_response = invented_names
    inbound(signup, 'Other Different', 'false-parser-names')
    tick_signup(signup.state)
    assert person(signup) is None
    assert len(signup.state.google_voice_connector.calls) == 2
    assert recovery_calls(signup)[-1]['actual_reply'] == 'Other Different'


def test_expected_recovery_rechecks_actual_reply_and_constraint_at_claim_boundary(signup, monkeypatch):
    from app.core import outbound_conversation
    begin(signup)
    monkeypatch.setattr('app.integrations.google_voice_signup.dispatch_outbound', lambda *_a: None)
    inbound(signup, 'Other Example', 'wrong-queued')
    tick_signup(signup.state)
    state = signup.state
    with state.session_factory() as session:
        selected = state.provider.test_sessions[PHONE]
        session.info['mac_test_session'] = selected
        queued = session.scalar(select(m.Message).where(m.Message.phone==PHONE, m.Message.direction=='out', m.Message.status=='queued'))
        assert queued
        from app.core import confirmations
        approval = confirmations.proof_for(session, queued)
        assert not outbound_conversation.queued_problem(session, queued, state.clock.now(), approval)
        source = session.get(m.Message, approval.payload['reply_to_message_id'])
        source.body = 'tampered reply'
        assert outbound_conversation.queued_problem(session, queued, state.clock.now(), approval)


def test_correct_expected_pair_continues_signup_and_preferences_without_role_grants(signup):
    begin(signup, {'first_name':'  Judge  ', 'last_name':' Example '})
    for index, text in enumerate(['Judge Example', 'Greeter', 'Sunday morning, twice per month']):
        inbound(signup, text, 'signup-' + str(index))
        tick_signup(signup.state)
    with signup.state.session_factory() as session:
        volunteer = session.get(m.Volunteer, person(signup).id)
        assert volunteer.sms_opt_in and volunteer.preferences['onboarding_stage'] == 'complete'
        assert volunteer.preferences['interested_roles'] == ['Greeter']
        assert volunteer.preferences['max_per_month'] == 2
        assert not volunteer.qualifications  # Interest never invents clearance.
        assert registered_consent_provenance(session, volunteer)
    assert len(signup.state.google_voice_connector.calls) == 3  # Completion remains silent.


def test_consent_publication_guard_distinguishes_historical_stop_evidence(signup):
    begin(signup)
    inbound(signup, 'Judge Example', 'correct')
    tick_signup(signup.state)
    inbound(signup, 'STOP', 'later-stop')
    tick_signup(signup.state)
    with signup.state.session_factory() as session:
        volunteer = session.get(m.Volunteer, person(signup).id)
        assert not registered_consent_provenance(session, volunteer)
        assert registered_consent_provenance(session, volunteer, require_current_consent=False)
        row = session.get(m.Policy, RECIPIENT_KEY + PHONE)
        row.value = {**row.value, 'expected_name': {'first_name':'Other','last_name':'Example'}}
        assert not registered_consent_provenance(session, volunteer, require_current_consent=False)


def test_legacy_approval_cannot_publish_expected_registration_identity(signup):
    from app.core.signup import approve_signup
    begin(signup)
    with signup.state.session_factory() as session:
        approval = SimpleNamespace(payload={'phone':PHONE, **EXPECTED})
        with pytest.raises(ValueError, match='original matching sender-name evidence'):
            approve_signup(session, signup.state.clock, approval)
        assert session.scalar(select(m.Volunteer).where(m.Volunteer.phone==PHONE)) is None
