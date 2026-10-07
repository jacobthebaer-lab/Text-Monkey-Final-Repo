"""A reserved batch serializes native attempts without discarding queued siblings."""
from dataclasses import replace
from datetime import timedelta
import json
from types import SimpleNamespace

import httpx
import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select

from app.agents import fill_agent
from app.agents.fill_agent import FillContext
from app.core import offer_windows as offers
from app.db import models as m
from app.integrations.mac_models import MacDeliveryClaim
from app.integrations.test_sessions import parse_sessions
from tests.session_fixtures import session_specs
from tests.test_clyde_algorithm import enable, start, rows
from tests.test_fill_agent import ScriptedAgentGloo
from tests.test_mac_messages import mac_app, post


@pytest.fixture(autouse=True)
def no_external_transports(monkeypatch):
    def forbidden(*args, **kwargs):
        pytest.fail('External HTTP/native actions forbidden in batch regression')
    monkeypatch.setattr(httpx.HTTPTransport, 'handle_request', forbidden)
    monkeypatch.setattr(httpx.AsyncHTTPTransport, 'handle_async_request', forbidden)
    import requests.sessions
    import urllib.request
    import app.integrations.mac_messages as native
    monkeypatch.setattr(requests.sessions.Session, 'request', forbidden)
    monkeypatch.setattr(urllib.request, 'urlopen', forbidden)
    monkeypatch.setattr(native, 'send_native', forbidden)


@pytest.fixture
def native_batch(mac_app, clock):
    app = mac_app
    phones = ['+12025550301', '+12025550302', '+12025550303']
    app.state.provider.phones = frozenset(phones)
    specs = session_specs(phones, clock.now())
    app.state.provider.test_sessions = parse_sessions(specs, set(phones))
    app.state.settings = replace(app.state.settings, mac_demo_phones=','.join(phones),
                                 mac_test_sessions=json.dumps(specs))
    app.state.gloo = ScriptedAgentGloo()
    with app.state.session_factory() as session:
        session.info['record_authorized'] = True
        session.scalar(select(m.Volunteer)).sms_opt_in = False
        enable(session)
        role = m.Role(name='Fictional Greeter', ministry='Test', required_qualifications=[],
                      fill_policy='auto', criticality='standard')
        event = m.Event(title='Fictional service', starts_at=clock.now()+timedelta(days=2),
                        ends_at=clock.now()+timedelta(days=2, hours=1), status='scheduled')
        session.add_all([role, event]); session.flush()
        shift = m.Shift(event_id=event.id, role_id=role.id, slot_index=0)
        session.add(shift); session.flush()
        people = []
        for phone in phones:
            person = m.Volunteer(name='Fictional Helper '+phone[-1], phone=phone,
                sms_opt_in=True, status='active', preferences={},
                created_at=clock.now()-timedelta(days=400))
            session.add(person); session.flush(); people.append(person.id)
        fill = m.FillRequest(shift_id=shift.id, state='in_progress', urgency='normal',
                            current_tranche=1, created_at=clock.now())
        session.add(fill); session.flush()
        start(FillContext(session, clock, app.state.provider, app.state.gloo), fill)
        outreaches = rows(session, fill)
        assert len(outreaches) == 2
        result = SimpleNamespace(app=app, clock=clock, phones=phones, people=people,
            fill_id=fill.id, shift_id=shift.id, event_id=event.id,
            offers=[o.id for o in outreaches], messages=[o.message_id for o in outreaches])
        session.commit()
    return result


def pull(client):
    result = post(client, '/mac/outbound/pull')
    assert result.status_code == 200, result.text
    return result.json()['messages']


def submitted(client, item):
    assert post(client, f"/mac/outbound/{item['id']}/verify", {'token':item['token']}).status_code == 200
    result = post(client, f"/mac/outbound/{item['id']}/ack",
                  {'token':item['token'], 'outcome':'submitted'})
    assert result.status_code == 200, result.text


def test_pending_claim_preserves_sibling_then_ack_allows_one_fresh_claim(native_batch):
    f = native_batch
    with f.app.state.session_factory() as session:
        sibling = session.get(m.Message, f.messages[1])
        body = sibling.body
        window = offers.metadata(session, session.get(m.Outreach, f.offers[1]))
        review_state = (window.state, window.body, window.expires_at)
    with TestClient(f.app) as client:
        first = pull(client)
        assert len(first) == 1 and first[0]['phone'] == f.phones[0]
        assert pull(client) == []
        with f.app.state.session_factory() as session:
            sibling = session.get(m.Message, f.messages[1])
            outreach = session.get(m.Outreach, f.offers[1])
            window = offers.metadata(session, outreach)
            assert sibling.status == 'queued' and sibling.body == body
            assert outreach.response == 'none'
            assert (window.state, window.body, window.expires_at) == review_state
            assert len(session.scalars(select(MacDeliveryClaim)).all()) == 1
        submitted(client, first[0])
        second = pull(client)
        assert len(second) == 1 and second[0]['phone'] == f.phones[1]
        assert pull(client) == []
        submitted(client, second[0])
        assert pull(client) == []
        with f.app.state.session_factory() as session:
            assert len(session.scalars(select(MacDeliveryClaim)).all()) == 2
            assert [session.get(m.Message, i).status for i in f.messages] == ['submitted', 'submitted']
            assert [session.get(m.Outreach, i).response for i in f.offers] == ['none', 'none']
            assert [offers.metadata(session, session.get(m.Outreach, i)).state for i in f.offers] == ['offer_active', 'offer_active']


def test_uncertain_receipt_never_claims_sibling_or_retries_attempt(native_batch):
    f = native_batch
    with TestClient(f.app) as client:
        first = pull(client)[0]
        assert post(client, f"/mac/outbound/{first['id']}/verify", {'token':first['token']}).status_code == 200
        payload = {'token':first['token'], 'outcome':'uncertain'}
        for _ in range(2):
            assert post(client, f"/mac/outbound/{first['id']}/ack", payload).status_code == 200
            assert pull(client) == []
        with f.app.state.session_factory() as session:
            assert len(session.scalars(select(MacDeliveryClaim)).all()) == 1
            assert session.get(MacDeliveryClaim, first['id']).token == first['token']
            assert session.get(m.Message, first['id']).status == 'uncertain'
            assert session.get(m.FillRequest, f.fill_id).state == 'escalated'
            assert offers.metadata(session, session.get(m.Outreach, f.offers[0])).state == 'offer_uncertain'
            assert session.get(MacDeliveryClaim, f.messages[1]) is None


@pytest.mark.parametrize('change', ['consent', 'event', 'membership', 'interval', 'unrelated_hold'])
@pytest.mark.parametrize('resolved', [False, True], ids=['claim_pending', 'after_submitted_ack'])
def test_wait_does_not_preserve_terminally_invalid_sibling(native_batch, change, resolved):
    f = native_batch
    with TestClient(f.app) as client:
        first = pull(client)[0]
        if resolved:
            submitted(client, first)
        with f.app.state.session_factory() as session:
            if change == 'consent':
                session.get(m.Volunteer, f.people[1]).sms_opt_in = False
            elif change == 'event':
                session.get(m.Event, f.event_id).status = 'cancelled'
            elif change == 'membership':
                receipt = session.scalar(select(m.Notification).where(m.Notification.purpose == 'algorithm_batch'))
                receipt.detail = {**receipt.detail, 'volunteer_ids':[f.people[0]]}
            elif change == 'interval':
                session.get(m.Event, f.event_id).ends_at += timedelta(minutes=15)
            else:
                # Even a valid sibling cannot mask another unresolved hold.
                session.add(m.Notification(key='offer:unrelated-proof', purpose='offer_window',
                    volunteer_id=f.people[1], state='offer_uncertain', created_at=f.clock.now(),
                    due_at=f.clock.now(), detail={'shift_id':-1}))
            session.commit()
        assert pull(client) == []
        with f.app.state.session_factory() as session:
            assert session.get(m.Message, f.messages[1]).status != 'queued'
            assert session.get(MacDeliveryClaim, f.messages[1]) is None
            assert len(session.scalars(select(MacDeliveryClaim)).all()) == 1
            assert session.get(MacDeliveryClaim, first['id']).token == first['token']


def test_winner_closes_sibling_preserving_inflight_unknown_and_deduplicates(native_batch):
    f = native_batch
    with TestClient(f.app) as client:
        first = pull(client)[0]
        submitted(client, first)
        held = pull(client)[0]
        assert post(client, f"/mac/outbound/{held['id']}/verify", {'token':held['token']}).status_code == 200
        with f.app.state.session_factory() as session:
            claim = session.get(MacDeliveryClaim, held['id'])
            snapshot = (claim.message_id, claim.token, session.get(m.Message, held['id']).status)
            session.info['mac_test_session'] = f.app.state.provider.test_sessions[f.phones[0]]
            outcome = fill_agent.on_outreach_reply(
                FillContext(session, f.clock, f.app.state.provider, f.app.state.gloo),
                session.get(m.Volunteer, f.people[0]), session.get(m.Outreach, f.offers[0]), 'accept')
            assert outcome.action == 'filled'
            assert [session.get(m.Outreach, i).response for i in f.offers] == ['yes', 'revoked']
            assert (claim.message_id, claim.token, session.get(m.Message, held['id']).status) == snapshot
            session.commit()
        for attempt in range(2):
            result = post(client, f"/mac/outbound/{held['id']}/ack",
                          {'token':held['token'], 'outcome':'uncertain'})
            assert result.status_code == 200, result.text
            outgoing = pull(client)
            if attempt == 0:
                # A truthful confirmation of the actual winner is distinct
                # from retrying the closed, uncertain sibling invitation.
                assert len(outgoing) == 1 and outgoing[0]['phone'] == f.phones[0]
                with f.app.state.session_factory() as session:
                    assert session.get(m.Message, outgoing[0]['id']).purpose == 'confirmation'
                submitted(client, outgoing[0])
            else:
                assert outgoing == []
        with f.app.state.session_factory() as session:
            assert session.get(m.FillRequest, f.fill_id).state == 'filled'
            assert len(session.scalars(select(m.Assignment).where(m.Assignment.shift_id == f.shift_id)).all()) == 1
            assert session.get(m.Message, held['id']).status == 'uncertain'
            assert session.get(MacDeliveryClaim, held['id']).token == held['token']
            context = FillContext(session, f.clock, f.app.state.provider, f.app.state.gloo)
            assert fill_agent.advance_due(context) == []
            session.info['mac_test_session'] = f.app.state.provider.test_sessions[f.phones[1]]
            late = fill_agent.on_outreach_reply(context, session.get(m.Volunteer, f.people[1]),
                                               session.get(m.Outreach, f.offers[1]), 'accept')
            assert late.action != 'filled'
            assert len(session.scalars(select(m.Assignment).where(m.Assignment.shift_id == f.shift_id)).all()) == 1
            closure = session.scalars(select(m.Notification).where(m.Notification.key.startswith(f'closed:{f.fill_id}:'))).all()
            assert len(closure) == 1 and all(n.state == 'blocked_policy' for n in closure)
            assert len(session.scalars(select(m.Message).where(m.Message.direction == 'out', m.Message.purpose == 'outreach')).all()) == 2
