"""Account drafts must not cross Gloo inputs. No real transport is invoked."""
import json
from dataclasses import replace

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select

from app.core import onboarding_copy as copy
from app.config import Settings
from app.core.onboarding import start, compose_reply, prompt_for
from app.core.send_gate import SendGate
from app.db import models as m
from app.main import create_app
from app.web.texty import admin
from tests.test_admin_setup import OWNER_A, OWNER_B
from tests.test_demo_acceptance_review import FixtureGloo, acceptance_app, OTHER_PHONE

PATH = '/api/setup/onboarding-copy'


@pytest.fixture
def editor_client(tmp_path, clock, monkeypatch):
    settings = Settings(database_url=f'sqlite:///{tmp_path}/copy.db',
                        demo_mode=True, automation_enabled=False)
    gloo = FixtureGloo(settings)
    monkeypatch.setattr('app.main.build_gloo', lambda _: gloo)
    app = create_app(settings)
    app.state.clock = clock
    user = {'id': OWNER_A, 'email': 'admin@example.test'}
    app.dependency_overrides[admin] = lambda: user
    with TestClient(app) as client:
        yield client, app, gloo, user


def drafts(label):
    return {field: f'{label} {field} wording {{first_name}} {{roles}}'
            for field in copy.FIELDS}


def save_drafts(client, label):
    result = client.post(PATH, json={'messages': drafts(label), 'revision': 0})
    assert result.status_code == 200
    return result.json()


def test_saved_drafts_are_account_scoped_and_cannot_forge_owner(editor_client):
    client, app, _, user = editor_client
    save_drafts(client, 'ALPHA')
    user['id'] = OWNER_B
    assert 'ALPHA' not in json.dumps(client.get(PATH).json())
    save_drafts(client, 'BRAVO')
    assert 'ALPHA' not in json.dumps(client.get(PATH).json())
    forged = client.post(PATH, json={
        'messages': drafts('FORGED'), 'revision': 1, 'owner_id': OWNER_A,
    })
    assert forged.status_code == 422
    user['id'] = OWNER_A
    assert client.get(PATH).json()['messages'] == drafts('ALPHA')
    with app.state.session_factory() as session:
        assert not session.scalar(select(m.Message))


def test_unbound_inbound_signup_never_selects_any_admin_draft(editor_client):
    client, app, gloo, user = editor_client
    save_drafts(client, 'ALPHA')
    user['id'] = OWNER_B
    save_drafts(client, 'BRAVO')
    with app.state.session_factory() as session:
        volunteer = m.Volunteer(name='Unbound Sample', phone='+12025550191',
            sms_opt_in=True, status='active', preferences={}, created_at=app.state.clock.now())
        session.add(volunteer); session.flush()
        start(session, app.state.clock, SendGate(session, app.state.clock, app.state.provider),
              volunteer, gloo)
        facts = json.loads(gloo.calls[-1]['input'])
        assert 'preferred_wording' not in facts
        assert 'ALPHA' not in json.dumps(facts) and 'BRAVO' not in json.dumps(facts)


@pytest.mark.parametrize('binding', [None, 'invalid-owner', OWNER_B])
def test_absent_invalid_or_unsaved_owner_never_falls_back_to_another_account(editor_client, binding):
    client, app, gloo, _ = editor_client
    save_drafts(client, 'ALPHA')
    with app.state.session_factory() as session:
        preferences = {'onboarding_stage': 'interests'}
        if binding is not None:
            preferences['onboarding_copy_owner'] = binding
        volunteer = m.Volunteer(name='Fallback Sample', phone='+12025550193',
            sms_opt_in=True, status='active', preferences=preferences, created_at=app.state.clock.now())
        session.add(volunteer); session.flush()
        compose_reply(session, app.state.clock, gloo, prompt_for(session, 'interests'),
                      volunteer, 'interests')
        facts = json.loads(gloo.calls[-1]['input'])
        assert 'preferred_wording' not in facts
        assert 'ALPHA' not in json.dumps(facts)


def test_explicit_admin_binding_and_rebinding_never_cross_accounts(editor_client):
    client, app, gloo, user = editor_client
    save_drafts(client, 'ALPHA')
    user['id'] = OWNER_B
    save_drafts(client, 'BRAVO')
    with app.state.session_factory() as session:
        volunteer = m.Volunteer(name='Bound Sample', phone='+12025550192',
            sms_opt_in=True, status='active', preferences={}, created_at=app.state.clock.now())
        session.add(volunteer); session.flush()
        gate = SendGate(session, app.state.clock, app.state.provider)
        start(session, app.state.clock, gate, volunteer, gloo, copy_owner=OWNER_A)
        assert 'ALPHA' in json.loads(gloo.calls[-1]['input'])['preferred_wording']
        start(session, app.state.clock, gate, volunteer, gloo, copy_owner=OWNER_B)
        facts = json.loads(gloo.calls[-1]['input'])
        assert 'BRAVO' in facts['preferred_wording'] and 'ALPHA' not in json.dumps(facts)
        assert volunteer.preferences['onboarding_copy_owner'] == OWNER_B


def test_authenticated_start_uses_server_owner_and_replaces_old_binding(acceptance_app):
    client, app, gloo, _ = acceptance_app
    user = {'id': OWNER_A, 'email': 'admin@example.test'}
    app.dependency_overrides[admin] = lambda: user
    app.state.settings = replace(app.state.settings, gloo_signup_replies=True)
    gloo.settings = app.state.settings
    save_drafts(client, 'ALPHA')
    user['id'] = OWNER_B
    save_drafts(client, 'BRAVO')
    with app.state.session_factory() as session:
        session.add(m.Policy(key='full_text_onboarding', value={'value': True}))
        volunteer = m.Volunteer(name='Route Sample', phone=OTHER_PHONE,
            sms_opt_in=True, status='active', preferences={}, created_at=app.state.clock.now())
        session.add(volunteer); session.flush()
        volunteer_id = volunteer.id
        session.commit()
    path = f'/api/volunteers/{volunteer_id}/text-setup'
    user['id'] = OWNER_A
    assert client.post(path, json={}).status_code == 200
    assert 'ALPHA' in json.loads(gloo.calls[-1]['input'])['preferred_wording']
    with app.state.session_factory() as session:
        volunteer = session.get(m.Volunteer, volunteer_id)
        volunteer.preferences = {**volunteer.preferences, 'onboarding_stage': 'complete'}
        session.commit()
    user['id'] = OWNER_B
    # Even an untrusted body field cannot select a different administrator.
    assert client.post(path, json={'copy_owner': OWNER_A}).status_code == 200
    facts = json.loads(gloo.calls[-1]['input'])
    assert 'BRAVO' in facts['preferred_wording'] and 'ALPHA' not in json.dumps(facts)
    with app.state.session_factory() as session:
        assert session.get(m.Volunteer, volunteer_id).preferences['onboarding_copy_owner'] == OWNER_B
