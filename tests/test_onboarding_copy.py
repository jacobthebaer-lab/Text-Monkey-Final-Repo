"""Editor persistence and operational mapping; fabricated accounts and mock transport."""
import json
from types import SimpleNamespace
import pytest
from sqlalchemy import select

from app.config import Settings
from app.core import onboarding
from app.core.onboarding_copy import DEFAULTS, copy_key, preferred_wording
from app.db import models as m
from app.llm.gloo_client import GlooUnavailableError
from tests.test_admin_setup import setup_client, OWNER_A, OWNER_B
from tests.test_mac_messages import mac_app, setup_invitation_app


def save_copy(client, messages=None, revision=0, **extra):
    return client.post('/api/setup/onboarding-copy', json={
        'messages': messages or DEFAULTS, 'revision': revision, **extra})


def test_copy_save_reload_reset_owner_isolation_and_no_delivery(setup_client):
    client, app, user = setup_client
    initial = client.get('/api/setup/onboarding-copy').json()
    assert initial['messages'] == DEFAULTS and initial['revision'] == 0
    edited = {**DEFAULTS, 'availability': 'Which days work best, {first_name}?'}
    saved = save_copy(client, edited)
    assert saved.status_code == 200
    assert saved.json()['revision'] == 1 and saved.json()['texts_sent'] == 0
    assert client.get('/api/setup/onboarding-copy').json()['messages'] == edited
    assert save_copy(client, revision=0).status_code == 409
    user['id'] = OWNER_B
    assert client.get('/api/setup/onboarding-copy').json()['messages'] == DEFAULTS
    assert save_copy(client, {**DEFAULTS, 'interests':'B only: {roles}'}).status_code == 200
    user['id'] = OWNER_A
    assert client.get('/api/setup/onboarding-copy').json()['messages'] == edited
    assert save_copy(client, DEFAULTS, revision=1).json()['messages'] == DEFAULTS
    with app.state.session_factory() as session:
        assert session.scalar(select(m.Message)) is None
        assert session.scalar(select(m.Approval)) is None
        assert session.scalar(select(m.Volunteer)) is None
        assert len(session.scalars(select(m.Policy)).all()) == 2


@pytest.mark.parametrize('changes', [
    {'availability': ''}, {'availability': 'x'*601}, {'availability': 1},
    {'availability': 'Visit https://example.test'}, {'completion':'Hi {first_name.__class__}'},
    {'interests':'Hi {roles'}, {'availability':'bad\x00copy'}, {'extra':'unsupported'},
])
def test_invalid_copy_is_not_saved(setup_client, changes):
    client, app, _ = setup_client
    assert save_copy(client, {**DEFAULTS, **changes}).status_code == 422
    assert client.get('/api/setup/onboarding-copy').json()['revision'] == 0
    with app.state.session_factory() as session:
        assert session.scalar(select(m.Policy)) is None


def test_untrusted_owner_and_revision_rejected_and_auth_required(setup_client):
    client, app, user = setup_client
    assert save_copy(client, owner_id=OWNER_B).status_code == 422
    assert save_copy(client, revision=True).status_code == 422
    assert save_copy(client, revision=-1).status_code == 422
    user['id'] = 'not-a-uuid'
    assert client.get('/api/setup/onboarding-copy').status_code == 403
    app.dependency_overrides.clear()
    assert client.get('/api/setup/onboarding-copy').status_code != 200
    assert save_copy(client).status_code != 200


class RecordedGloo:
    settings = Settings(gloo_signup_replies=True)
    def __init__(self, extraction=None):
        self.calls = []
        self.extraction = extraction or {'understood':False}
    def create_response(self, **kwargs):
        facts = json.loads(kwargs['input'])
        self.calls.append(facts)
        return SimpleNamespace(output_text=facts['approved_message'] if 'approved_message' in facts else json.dumps(self.extraction))


def store(session, owner_id, marker):
    session.add(m.Policy(key=copy_key(owner_id), value={'messages':{
        key: marker + ' {first_name} {roles}' for key in DEFAULTS},'revision':1}))
    session.flush()


def test_bound_owner_is_the_only_copy_sent_to_gloo_and_canonical_facts_win(session, clock, gate, provider, make_volunteer, make_shift):
    store(session, OWNER_A, 'A draft')
    store(session, OWNER_B, 'B draft')
    make_shift('Greeter')
    a, b, unbound = make_volunteer('Alpha Example'), make_volunteer('Beta Example'), make_volunteer('Unbound Example')
    gloo = RecordedGloo()
    onboarding.start(session, clock, gate, a, gloo, copy_owner=OWNER_A)
    assert a.preferences['onboarding_copy_owner'] == OWNER_A
    assert gloo.calls[-1]['preferred_wording'].startswith('A draft Alpha')
    assert 'B draft' not in json.dumps(gloo.calls[-1])
    assert 'Greeter' in gloo.calls[-1]['preferred_wording']
    onboarding.start(session, clock, gate, b, gloo, copy_owner=OWNER_B)
    assert gloo.calls[-1]['preferred_wording'].startswith('B draft Beta')
    assert 'A draft' not in json.dumps(gloo.calls[-1])
    onboarding.start(session, clock, gate, unbound, gloo)
    assert 'preferred_wording' not in gloo.calls[-1]
    assert all(message.body.startswith('What would you like to help with?') for message in provider.sent)
    assert not session.scalars(select(m.Assignment)).all()


def test_saved_clarification_and_completion_use_current_bound_sender(session, clock, gate, provider, make_volunteer):
    store(session, OWNER_A, 'Account A')
    person = make_volunteer('Casey Example', prefs={'onboarding_stage':'availability','onboarding_copy_owner':OWNER_A})
    gloo = RecordedGloo()
    assert onboarding.handle(session, clock, gate, person, 'unclear', gloo) == 'onboarding_clarify'
    assert gloo.calls[-1]['preferred_wording'].startswith('Account A Casey')
    assert gloo.calls[-1]['approved_message'] == 'Which days can you serve, and how often each month?'
    assert person.preferences['onboarding_stage'] == 'availability'
    gloo.extraction = {'understood':True, 'weekdays':[6], 'preferred_services':[], 'max_per_month':2,
        'available_dates':[], 'unavailable_dates':[]}
    assert onboarding.handle(session, clock, gate, person, 'Flexible', gloo) == 'onboarding_complete'
    assert 'Casey' in gloo.calls[-1]['approved_message']
    assert person.preferences['onboarding_stage'] == 'complete'
    assert not session.scalars(select(m.Assignment)).all()


def test_partial_availability_keeps_days_and_uses_only_bound_clarification_copy(session, clock, gate, provider, make_volunteer):
    store(session, OWNER_A, 'A draft')
    store(session, OWNER_B, 'B private draft')
    person = make_volunteer('Casey Example', prefs={
        'onboarding_stage': 'availability', 'onboarding_copy_owner': OWNER_A})
    gloo = RecordedGloo({'understood': True, 'availability_known': True,
        'frequency_known': False, 'weekdays': [6, 2], 'all_day': True,
        'preferred_services': [], 'max_per_month': None,
        'available_dates': [], 'unavailable_dates': []})
    assert onboarding.handle(session, clock, gate, person,
        'Sundays and Wednesdays all day', gloo) == 'onboarding_clarify'
    facts = gloo.calls[-1]
    assert facts['approved_message'] == 'How often would you like to serve each month?'
    assert facts['preferred_wording'].startswith('A draft Casey')
    assert 'B private draft' not in json.dumps(facts)
    assert person.preferences['onboarding_availability_draft']['weekdays'] == [6, 2]
    assert provider.sent[-1].body == facts['approved_message']
    assert not session.scalars(select(m.Assignment)).all()


def test_unbound_or_invalid_binding_never_uses_another_admin(session, make_volunteer):
    store(session, OWNER_A, 'Private A')
    person = make_volunteer()
    assert preferred_wording(session, 'availability', person) is None
    person.preferences = {'onboarding_copy_owner':'invalid'}
    assert preferred_wording(session, 'availability', person) is None
    person.preferences = {'onboarding_copy_owner':OWNER_B}
    assert preferred_wording(session, 'availability', person) is None


@pytest.mark.parametrize('failure', ['none', 'unavailable', 'disabled_setting'])
def test_onboarding_always_requires_gloo_never_sends_saved_literal(session, clock, gate, provider, make_volunteer, failure):
    store(session, OWNER_A, 'Literal draft should never send')
    person = make_volunteer()
    def unavailable(**kwargs):
        raise GlooUnavailableError('Synthetic outage')
    gloo = None if failure == 'none' else SimpleNamespace(
        settings=Settings(gloo_signup_replies=failure != 'disabled_setting'), create_response=unavailable)
    with pytest.raises(GlooUnavailableError):
        onboarding.start(session, clock, gate, person, gloo, copy_owner=OWNER_A)
    assert not provider.sent
    assert session.scalar(select(m.Message)) is None


def test_standalone_editor_assets_are_available_without_account_data(setup_client):
    client, _, _ = setup_client
    for path in ['/onboarding-copy.html','/onboarding-copy.js','/onboarding-copy-nav.js','/onboarding-copy.css','/onboarding-copy-defaults.json']:
        assert client.get(path).status_code == 200
    assert client.get('/onboarding-copy-defaults.json').json() == DEFAULTS


def test_new_unbound_start_clears_an_old_admin_binding(session, clock, gate, make_volunteer):
    store(session, OWNER_A, 'Old A copy')
    person = make_volunteer(prefs={'onboarding_copy_owner':OWNER_A})
    gloo = RecordedGloo()
    onboarding.start(session, clock, gate, person, gloo)
    assert 'onboarding_copy_owner' not in person.preferences
    assert 'preferred_wording' not in gloo.calls[-1]


def test_authenticated_start_binds_the_verified_owner_and_restart_replaces_old_binding(mac_app):
    from fastapi.testclient import TestClient
    from app.web.texty import admin
    volunteer_id = setup_invitation_app(mac_app)
    user = {'id':OWNER_A, 'email':'coordinator@example.test'}
    mac_app.dependency_overrides[admin] = lambda: user
    gloo = RecordedGloo()
    gloo.settings = mac_app.state.settings
    mac_app.state.gloo = gloo
    with TestClient(mac_app) as client:
        assert save_copy(client, {**DEFAULTS, 'interests':'Account A: {roles}'}).status_code == 200
        user['id'] = OWNER_B
        assert save_copy(client, {**DEFAULTS, 'interests':'Account B: {roles}'}).status_code == 200
        user['id'] = OWNER_A
        route = f'/api/volunteers/{volunteer_id}/text-setup'
        assert client.post(route).status_code == 200
        assert gloo.calls[-1]['preferred_wording'].startswith('Account A:')
        with mac_app.state.session_factory() as session:
            session.info['record_authorized'] = True
            person = session.get(m.Volunteer, volunteer_id)
            person.preferences = {**person.preferences, 'onboarding_stage':'complete'}
            session.commit()
        user['id'] = OWNER_B
        assert client.post(route).status_code == 200
        assert gloo.calls[-1]['preferred_wording'].startswith('Account B:')
        assert 'Account A:' not in json.dumps(gloo.calls[-1])
        with mac_app.state.session_factory() as session:
            assert session.get(m.Volunteer, volunteer_id).preferences['onboarding_copy_owner'] == OWNER_B
