"""No real Gloo, network, Messages database or native text delivery."""
import json
from copy import deepcopy
from types import SimpleNamespace

import httpx
import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select

from app.config import Settings
from app.core import confirmations, templates
from app.core.message_style import (EM_DASH_CHARACTERS, NO_EM_DASH_INSTRUCTIONS,
                                    OutboundStyleError, validate_outbound_style)
from app.core.send_gate import SendStatus, VALID_PURPOSES, _send_direct
from app.db import models as m
from app.integrations.mac_messages import MacWorker, send_native
from app.integrations.mac_models import MacDeliveryClaim
from app.llm.gloo_client import GlooClient
from tests.test_mac_messages import (PHONE, HEADERS, ReaderFixture, config, mac_app, post)  # noqa: F401
from tests.test_mac_messages import queue_essential_intake
from tests.session_fixtures import session_id


@pytest.mark.parametrize('dash', sorted(EM_DASH_CHARACTERS))
def test_em_dash_and_actual_presentation_forms_are_rejected(dash):
    with pytest.raises(OutboundStyleError, match='Regenerate through Gloo'):
        validate_outbound_style('Before' + dash + 'after')


def test_hyphen_en_dash_and_exact_user_baseline_are_preserved():
    from app.core.signup_copy import EXACT_COPY
    for body in ['Sunday 8–10, role-specific.', *[v for v in EXACT_COPY.values() if v is not None]]:
        original = body
        validate_outbound_style(body)
        assert body == original


@pytest.mark.parametrize('purpose', sorted(VALID_PURPOSES))
def test_every_send_purpose_blocks_before_provider_message_or_approval(gate, session, provider, make_volunteer, purpose):
    volunteer = make_volunteer()
    result = gate.send(body='Status update\u2014please check.', purpose=purpose, volunteer=volunteer)
    assert result.status == SendStatus.BLOCKED_STYLE
    assert 'Regenerate through Gloo' in result.reason
    assert not provider.sent
    assert session.scalar(select(m.Message)) is None
    assert session.scalar(select(m.Approval)) is None


@pytest.mark.parametrize('review', [False, True])
def test_generated_em_dash_never_reaches_provider_or_human_review(gate, session, clock, provider, make_volunteer, make_shift, review):
    volunteer = make_volunteer()
    shift = make_shift()
    assignment = m.Assignment(volunteer_id=volunteer.id, shift_id=shift.id, status='confirmed', source='planner',
                              created_at=clock.now(), updated_at=clock.now())
    session.add(assignment); session.flush()
    session.info['competition_confirmation_required'] = review
    gate.gloo = SimpleNamespace(settings=Settings(gloo_signup_replies=True),
        create_response=lambda **kwargs: SimpleNamespace(output_text=json.loads(kwargs['input'])['approved_message'] + '\u2014thank you.'))
    result = gate.send(body='Your shift is confirmed.', purpose='confirmation', volunteer=volunteer,
                       conversation={'assignment_id':assignment.id, 'notice':'scheduled'})
    assert result.status == SendStatus.BLOCKED_STYLE
    assert not provider.sent and session.scalar(select(m.Message)) is None
    assert session.scalar(select(m.Approval)) is None


def test_existing_approved_body_and_hash_are_not_rewritten(gate, session, clock, provider, make_volunteer):
    volunteer = make_volunteer()
    payload = {'phone': volunteer.phone, 'body': 'Approved\u2014copy.', 'purpose': 'confirmation'}
    approval = confirmations.stage(session, clock.now(), payload)
    approval.status = 'approved'
    original = deepcopy(approval.payload)
    result = gate.send(body=payload['body'], purpose='confirmation', volunteer=volunteer, _confirmation=approval)
    assert result.status == SendStatus.BLOCKED_STYLE
    assert approval.payload == original and approval.payload['content_hash'] == confirmations.digest(approval.payload)
    assert not provider.sent and session.scalar(select(m.Message)) is None


def test_direct_stop_start_helper_also_blocks_before_provider(session, clock, provider, make_volunteer):
    volunteer = make_volunteer()
    with pytest.raises(OutboundStyleError):
        _send_direct(session, clock, provider, volunteer, 'Stopped\u2014no texts.', 'stop_confirm')
    assert not provider.sent and session.scalar(select(m.Message)) is None


def test_preexisting_bad_queue_row_never_gets_claim_and_other_row_can_progress(mac_app):
    with mac_app.state.session_factory() as session:
        selected = mac_app.state.provider.test_sessions[PHONE]
        session.add(m.Message(direction='out', phone=PHONE, body='Old\u2014queued body.', purpose='signup_reply',
            kind='ai', status='queued', provider_sid=selected.outbound_prefix + 'old',
            created_at=mac_app.state.clock.now()))
        session.commit()
    queue_essential_intake(mac_app, body='What roles would you like?')
    with TestClient(mac_app) as client:
        batch = post(client, '/mac/outbound/pull').json()['messages']
        assert [item['body'] for item in batch] == ['What roles would you like?']
    with mac_app.state.session_factory() as session:
        bad = session.scalar(select(m.Message).where(m.Message.body == 'Old\u2014queued body.'))
        assert bad.status == 'blocked_style' and session.get(MacDeliveryClaim, bad.id) is None
        assert bad.body == 'Old\u2014queued body.'


def test_changed_claim_body_is_rejected_at_native_preflight(mac_app):
    queue_essential_intake(mac_app, body='What roles would you like?')
    with TestClient(mac_app) as client:
        item = post(client, '/mac/outbound/pull').json()['messages'][0]
        with mac_app.state.session_factory() as session:
            session.get(m.Message, item['id']).body = 'Tampered\u2014body.'
            session.commit()
        rejected = post(client, f"/mac/outbound/{item['id']}/verify", {'token': item['token']})
        assert rejected.status_code == 409 and 'Regenerate through Gloo' in rejected.json()['detail']
    with mac_app.state.session_factory() as session:
        assert session.get(m.Message, item['id']).status == 'blocked_style'


@pytest.mark.parametrize('preflight_changed', [False, True])
def test_worker_never_attempts_or_acknowledges_forbidden_cached_or_preflight_body(tmp_path, preflight_changed):
    sent, requests = [], []
    item = {'id': 17, 'token': 'c'*64, 'phone': PHONE, 'session_id': session_id(PHONE),
            'body': 'Compliant body.' if preflight_changed else 'Cached\u2014body.'}
    if preflight_changed:
        item['offer_preflight_required'] = True
    def server(request):
        requests.append(request.url.path)
        if request.url.path.endswith('/pull'):
            return httpx.Response(200, json={'messages': [item]})
        if request.url.path.endswith('/verify'):
            return httpx.Response(200, json={'verified': True, 'phone': PHONE, 'body': 'Changed\u2014body.'})
        pytest.fail('Forbidden text must not get a submitted or uncertain acknowledgment')
    worker = MacWorker(config(tmp_path), live=True, client=httpx.Client(transport=httpx.MockTransport(server)),
        reader=ReaderFixture(), sender=lambda *args: sent.append(args) or 'submitted')
    worker.once()
    assert not sent
    entry = worker.state['dispatches']['17']
    assert entry['outcome'] == 'blocked' and 'Regenerate through Gloo' in entry['reason']
    assert not any(path.endswith('/ack') for path in requests)


def test_raw_native_send_rejects_before_osascript(monkeypatch):
    monkeypatch.setattr('app.integrations.mac_messages.subprocess.run', lambda *args, **kwargs: pytest.fail('Native side effect forbidden'))
    with pytest.raises(OutboundStyleError):
        send_native(PHONE, 'Raw\ufe58body.')


def test_common_gloo_generation_instructions_cover_every_writer():
    requests = []
    client = GlooClient(Settings(gloo_api_key='synthetic'),
        client=SimpleNamespace(responses=SimpleNamespace(create=lambda **kwargs: requests.append(kwargs) or SimpleNamespace(usage=None))))
    client.create_response(model='synthetic', input='Facts', instructions='Preserve original exact approved wording.')
    assert requests[0]['instructions'].startswith('Preserve original exact approved wording.')
    assert requests[0]['instructions'].endswith(NO_EM_DASH_INSTRUCTIONS)


def test_fixed_template_generation_uses_safe_punctuation():
    for body in [templates.filled_thanks('Alex Example'), templates.cancellation_ack('Alex Example'),
                 templates.clarify_which_shift('Alex Example', ['Sunday 8–10']), templates.partial_thanks('Alex Example'),
                 templates.clarify_generic('Alex Example'), templates.pastor_alert('Alex Example', 'Needs a call.')]:
        validate_outbound_style(body)
