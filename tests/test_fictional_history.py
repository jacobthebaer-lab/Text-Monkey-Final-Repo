"""Authenticated synthetic history must never broaden native conversation scope."""
from datetime import datetime, timezone
import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select
from app.db import models as m
from app.web.texty import admin
from app.web.fictional_history import DATASET
from tests.test_mac_messages import mac_app, PHONE


@pytest.fixture
def fictional_app(mac_app):
    mac_app.dependency_overrides[admin] = lambda: {'email': 'coordinator@example.test'}
    people, messages = [], []
    with mac_app.state.session_factory() as session:
        for i in range(100):
            person = m.Volunteer(name=f'Synthetic Person {i+1} [Fictional]', phone=f'+120255501{i:02}',
                sms_opt_in=False, status='active' if i < 85 else 'inactive',
                preferences={'synthetic': True, 'fictional_seed': True, 'synthetic_dataset': DATASET,
                             'synthetic_person_key': f'person-{i+1:03}'}, created_at=mac_app.state.clock.now())
            session.add(person); session.flush(); people.append(person.id)
            for j in range(19):
                message = m.Message(volunteer_id=person.id, phone=person.phone,
                    direction='in' if j % 2 else 'out', kind='synthetic', status='simulated',
                    purpose='fictional_availability_reply' if j % 2 else 'fictional_invitation',
                    body=f'[Fictional history] Person {i+1} simulated reply {j}', provider_sid=None,
                    created_at=datetime(2026, 7+j%3, 1+j, 12, tzinfo=timezone.utc))
                session.add(message); session.flush(); messages.append(message.id)
        session.add(m.Policy(key='synthetic_dataset:'+DATASET, value={
            'synthetic': True, 'provenance': DATASET, 'no_consent_no_delivery': True,
            'date_range': ['2026-07-01', '2026-09-30'], 'timezone': 'America/Denver',
            'artifact_sha256': 'a'*64,
            'id_maps': {'volunteers': {str(i+1): v for i,v in enumerate(people)},
                        'messages': {str(i+1): v for i,v in enumerate(messages)}},
        })); session.commit()
    yield mac_app, people, messages
    mac_app.dependency_overrides.clear()


def test_all_fictional_profiles_and_complete_three_month_person_history(fictional_app):
    app, people, messages = fictional_app
    with TestClient(app) as client:
        roster = client.get('/api/state').json()
        fictional = [v for v in roster['volunteers'] if v['fictional']]
        assert len(fictional) == 100
        assert sum(v['status'] == 'active' for v in fictional) == 85
        assert all(not v['consent'] and not v['can_start_text_setup'] for v in fictional)
        assert all(v['text_setup_block_code'] == 'fictional_profile' for v in fictional)
        assert roster['messages'] == []  # The global Mac session inbox stays untouched.
        result = client.get(f'/api/volunteers/{people[0]}/history')
        assert result.status_code == 200
        assert result.headers['Cache-Control'] == 'no-store'
        history = result.json()
        assert history['fictional'] and history['volunteer_id'] == str(people[0])
        assert len(history['messages']) == 19
        assert {m['created_at'][5:7] for m in history['messages']} == {'07','08','09'}
        assert all(m['fictional'] and m['status'] == 'simulated' and 'Person 1 simulated' in m['body'] for m in history['messages'])
        assert history['next_before_id'] is None
        assert 'Person 2 simulated' not in result.text
        first = client.get(f'/api/volunteers/{people[0]}/history?limit=10').json()
        second = client.get(f'/api/volunteers/{people[0]}/history?limit=10&before_id={first["next_before_id"]}').json()
        ids = [m['id'] for m in first['messages']+second['messages']]
        assert len(ids) == len(set(ids)) == 19
        for query in ('limit=201', 'limit=0', 'before_id=-1'):
            assert client.get(f'/api/volunteers/{people[0]}/history?{query}').status_code == 422
        assert client.get('/api/volunteers/99999/history').status_code == 404
        app.dependency_overrides.clear()
        assert client.get(f'/api/volunteers/{people[0]}/history').status_code != 200
    with app.state.session_factory() as session:
        assert len(session.scalars(select(m.Message)).all()) == 1900
        assert session.scalar(select(m.Approval)) is None
        assert session.scalar(select(m.AgentRun)) is None


@pytest.mark.parametrize('fault', ['marker', 'marker_provenance', 'marker_hash', 'volunteer_map',
    'message_map', 'phone', 'name', 'seed', 'dataset', 'person_key', 'consent',
    'kind', 'status', 'purpose', 'provider_sid', 'body', 'direction', 'date', 'binding'])
def test_fictional_history_requires_all_provenance_and_row_predicates(fictional_app, fault):
    app, people, messages = fictional_app
    target_id = people[0]
    target_message = messages[0]
    with app.state.session_factory() as session:
        person = session.get(m.Volunteer, target_id)
        message = session.get(m.Message, target_message)
        marker = session.get(m.Policy, 'synthetic_dataset:'+DATASET)
        value = {**marker.value, 'id_maps': {k:dict(v) for k,v in marker.value['id_maps'].items()}}
        prefs = dict(person.preferences)
        if fault == 'marker': session.delete(marker)
        elif fault == 'marker_provenance': value['provenance'] = 'unverified'
        elif fault == 'marker_hash': value['artifact_sha256'] = ''
        elif fault == 'volunteer_map': value['id_maps']['volunteers'].pop('1')
        elif fault == 'message_map': value['id_maps']['messages'].pop('1')
        elif fault == 'phone': person.phone = '+15555550999'; message.phone = person.phone
        elif fault == 'name': person.name = 'Unmarked person'
        elif fault == 'seed': prefs.pop('fictional_seed')
        elif fault == 'dataset': prefs['synthetic_dataset'] = 'unverified'
        elif fault == 'person_key': prefs['synthetic_person_key'] = 'person-000'
        elif fault == 'consent': person.sms_opt_in = True
        elif fault == 'kind': message.kind = 'admin'
        elif fault == 'status': message.status = 'received'
        elif fault == 'purpose': message.purpose = 'inbound'
        elif fault == 'provider_sid': message.provider_sid = 'MACunverified'
        elif fault == 'body': message.body = 'Synthetic excluded unmarked body'
        elif fault == 'direction': message.direction = 'other'
        elif fault == 'date': message.created_at = datetime(2026, 10, 2, tzinfo=timezone.utc)
        elif fault == 'binding': message.volunteer_id = people[1]
        person.preferences = prefs
        if fault != 'marker': marker.value = value
        session.commit()
    with TestClient(app) as client:
        result = client.get(f'/api/volunteers/{target_id}/history')
        assert result.status_code == 200
        assert str(target_message) not in [m['id'] for m in result.json()['messages']]
        assert 'Person 2 simulated' not in result.text


def test_fictional_profiles_cannot_create_welcome_or_manual_text_even_if_consent_tampered(fictional_app):
    app, people, messages = fictional_app
    with app.state.session_factory() as session:
        person = session.get(m.Volunteer, people[0]); person.sms_opt_in = True; session.commit()
    with TestClient(app) as client:
        assert client.post(f'/api/volunteers/{people[0]}/text-setup').status_code == 409
        result = client.post('/api/reply', json={'volunteer_id':people[0], 'body':'Synthetic manual text',
                                              'request_id':'1c7cde98-1850-4b5c-a5ea-765a666c420a'})
        assert result.status_code == 409
        assert 'Fictional profiles' in result.json()['detail']
    with app.state.session_factory() as session:
        assert len(session.scalars(select(m.Message)).all()) == 1900
        assert session.scalar(select(m.Approval)) is None
        assert session.scalar(select(m.AgentRun)) is None


def test_new_history_keeps_native_session_and_recipient_privacy(mac_app):
    from tests.session_fixtures import session_id
    mac_app.dependency_overrides[admin] = lambda: {'email':'coordinator@example.test'}
    with mac_app.state.session_factory() as session:
        person = session.scalar(select(m.Volunteer))
        for phone, purpose, body in [(PHONE,'test:'+session_id(PHONE),'Synthetic scoped native input'),
                                      (PHONE,None,'Synthetic excluded same-phone input'),
                                      ('+15555550999','test:'+session_id(PHONE),'Synthetic excluded other phone')]:
            session.add(m.Message(volunteer_id=person.id,phone=phone,purpose=purpose,body=body,
                kind='inbound',status='received',direction='in',created_at=mac_app.state.clock.now()))
        session.commit(); person_id=person.id
    try:
        with TestClient(mac_app) as client:
            result=client.get(f'/api/volunteers/{person_id}/history').json()
            assert not result['fictional']
            assert [m['body'] for m in result['messages']] == ['Synthetic scoped native input']
            assert not result['messages'][0]['fictional']
            mac_app.state.provider.test_sessions.clear()
            assert client.get(f'/api/volunteers/{person_id}/history').json()['messages'] == []
    finally:
        mac_app.dependency_overrides.clear()


def test_fictional_person_with_101_messages_loads_complete_history_across_default_page_boundary(fictional_app):
    app, people, messages = fictional_app
    with app.state.session_factory() as session:
        person=session.get(m.Volunteer,people[0])
        marker=session.get(m.Policy,'synthetic_dataset:'+DATASET)
        value={**marker.value,'id_maps':{k:dict(v) for k,v in marker.value['id_maps'].items()}}
        for i in range(82):
            message=m.Message(volunteer_id=person.id,phone=person.phone,direction='out',kind='synthetic',
                status='simulated',purpose='fictional_admin_status',provider_sid=None,
                body='[Fictional history] Synthetic extra history',
                created_at=datetime(2026,9,25,12,tzinfo=timezone.utc))
            session.add(message);session.flush()
            value['id_maps']['messages'][str(1901+i)]=message.id
        marker.value=value;session.commit()
    with TestClient(app) as client:
        first=client.get(f'/api/volunteers/{people[0]}/history').json()
        assert len(first['messages'])==100 and first['next_before_id'] is not None
        second=client.get(f'/api/volunteers/{people[0]}/history?before_id={first["next_before_id"]}').json()
        assert len(second['messages'])==1 and second['next_before_id'] is None
        ids=[m['id'] for m in first['messages']+second['messages']]
        assert len(set(ids))==101
