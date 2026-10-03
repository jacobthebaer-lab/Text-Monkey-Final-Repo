"""Acceptance boundaries using fictional contacts and an in-process Mac queue.

No connector is run, no Messages call occurs, and Gloo responses are fixtures.
"""
import json
import time
from datetime import timedelta
from types import SimpleNamespace
from uuid import uuid4

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select

from app.config import Settings
from app.db import models as m
from app.integrations.mac_models import MacDeliveryClaim
from app.llm.gloo_client import GlooUnavailableError
from app.main import create_app
from app.web.texty import admin
from tests.session_fixtures import session_json, session_id
from tests.test_admin_setup import OWNER_A, save
from tests.test_admin_text_settings import enable

PHONE = '+12025550199'
OTHER_PHONE = '+12025550198'
BRIDGE_TOKEN = 'synthetic-acceptance-credential-' + 'x' * 40
CHECK_PATH = '/api/setup/admin-texts/send-check'


class FixtureGloo:
    def __init__(self, settings):
        self.settings = settings
        self.calls = []
        self.unavailable = False

    def create_response(self, **kwargs):
        self.calls.append(kwargs)
        if self.unavailable:
            raise GlooUnavailableError('Fixture outage')
        return SimpleNamespace(output_text=json.loads(kwargs['input'])['approved_message'])


@pytest.fixture
def acceptance_app(tmp_path, clock, monkeypatch):
    settings = Settings(
        database_url=f'sqlite:///{tmp_path}/acceptance.db',
        demo_mode=False, automation_enabled=False,
        gloo_api_key='synthetic-key', gloo_signup_replies=False,
        sms_provider='mac_messages', mac_bridge_enabled=True,
        mac_bridge_token=BRIDGE_TOKEN, admin_password='synthetic-acceptance-password',
        mac_demo_phones=f'{PHONE},{OTHER_PHONE}',
        mac_test_sessions=session_json([PHONE, OTHER_PHONE], clock.now()),
    )
    gloo = FixtureGloo(settings)
    monkeypatch.setattr('app.main.build_gloo', lambda _: gloo)
    app = create_app(settings)
    app.state.clock = app.state.mac_delivery_clock = clock
    app.state.mac_last_poll = time.monotonic()
    app.dependency_overrides[admin] = lambda: {'id': OWNER_A, 'email': 'admin@example.test'}
    with TestClient(app) as client:
        assert save(client, complete=True).status_code == 200
        assert enable(client, phone=PHONE).status_code == 200
        yield client, app, gloo, clock


def check(client, request_id=None):
    return client.post(CHECK_PATH, json={'request_id': request_id or str(uuid4())})


def pull(client):
    return client.post('/mac/outbound/pull', json={},
                       headers={'Authorization': 'Bearer ' + BRIDGE_TOKEN})


def test_scheduled_readiness_does_not_claim_active_automation_for_one_shot_check(acceptance_app):
    client, app, gloo, _ = acceptance_app
    status = client.get('/api/setup/admin-texts').json()
    assert status['enabled'] and not status['ready']
    assert any('paused' in issue for issue in status['issues'])
    result = check(client).json()
    assert result['delivery'] == 'queued_for_mac' and result['status'] == 'queued'
    assert len(gloo.calls) == 1  # Gloo required even with signup composition off.
    with app.state.session_factory() as session:
        assert session.get(m.Message, result['message_id']).status == 'queued'
        assert not session.scalar(select(MacDeliveryClaim))  # Queue is not delivery proof.


@pytest.mark.parametrize('change', ['pause', 'replace', 'optout', 'expiry'])
def test_admin_queue_rechecks_recipient_and_session_before_claim(acceptance_app, change):
    client, app, gloo, clock = acceptance_app
    message_id = check(client).json()['message_id']
    if change == 'pause':
        assert client.post('/api/setup/admin-texts', json={'enabled': False}).status_code == 200
    elif change == 'replace':
        assert enable(client, phone=OTHER_PHONE).status_code == 200
    elif change == 'optout':
        with app.state.session_factory() as session:
            session.add(m.Policy(key='sms_opt_out:' + PHONE, value={'value': True}))
            session.commit()
    else:
        clock.advance(timedelta(hours=1))
    assert pull(client).json()['messages'] == []
    with app.state.session_factory() as session:
        assert session.get(m.Message, message_id).status in {
            'blocked_admin_updates', 'blocked_opt_out', 'blocked_test_session',
        }
        assert not session.scalar(select(MacDeliveryClaim))
    assert len(gloo.calls) == 1


def test_stop_after_session_expiry_cannot_be_overridden_by_reenrollment(acceptance_app):
    client, app, _, clock = acceptance_app
    clock.advance(timedelta(hours=1))
    response = client.post('/mac/inbound', json={
        'guid': 'acceptance-expired-stop', 'phone': PHONE, 'body': 'STOP',
        'session_id': session_id(PHONE),
    }, headers={'Authorization': 'Bearer ' + BRIDGE_TOKEN})
    assert response.status_code == 200
    assert enable(client, phone=PHONE).status_code == 409
    assert check(client).status_code == 409
    with app.state.session_factory() as session:
        recipient = session.scalar(select(m.Volunteer).where(m.Volunteer.phone == PHONE))
        assert not recipient.sms_opt_in
        assert not session.scalar(select(MacDeliveryClaim))


def test_gloo_outage_and_fresh_request_recovery_never_use_a_template(acceptance_app):
    client, app, gloo, _ = acceptance_app
    gloo.unavailable = True
    first = check(client).json()
    assert first['delivery'] == 'pending' and first['message_id'] is None
    with app.state.session_factory() as session:
        assert not session.scalar(select(m.Message))
    gloo.unavailable = False
    recovered = check(client).json()
    assert recovered['delivery'] == 'queued_for_mac'
    assert len(gloo.calls) == 2
    with app.state.session_factory() as session:
        assert len(session.scalars(select(m.Message)).all()) == 1


@pytest.mark.xfail(strict=True, reason='9ed9d71: same-ID pending admin check is not retried after Gloo recovery with scheduler off')
def test_same_request_recovers_after_gloo_outage_without_scheduler(acceptance_app):
    client, _, gloo, clock = acceptance_app
    request_id = str(uuid4())
    gloo.unavailable = True
    assert check(client, request_id).json()['delivery'] == 'pending'
    gloo.unavailable = False
    clock.advance(timedelta(minutes=2))  # Respect the stored composition retry time.
    recovered = check(client, request_id).json()
    assert recovered['delivery'] == 'queued_for_mac'
    assert len(gloo.calls) == 2
    assert check(client, request_id).json()['message_id'] == recovered['message_id']
    assert len(gloo.calls) == 2
