"""Exercise authenticated legacy views without changing stored/reviewed data."""
from copy import deepcopy
from datetime import timedelta

import pytest
from fastapi.testclient import TestClient

from app.config import Settings
from app.core import confirmations
from app.db import models as m
from app.main import create_app


@pytest.fixture
def legacy(tmp_path, clock):
    app = create_app(Settings(database_url=f'sqlite:///{tmp_path}/legacy.sqlite',
        admin_password='isolated-password', sms_provider='mock', live_sms=False,
        automation_enabled=False))
    app.state.clock = clock
    with TestClient(app) as client:
        yield app, client, clock


def test_natural_labels_across_legacy_views_preserve_exact_records_and_reviews(legacy):
    app, client, clock = legacy
    with app.state.session_factory() as session:
        person = m.Volunteer(name='Test Jordan Ellis [Fictional]', phone='+12025550100',
            sms_opt_in=False, status='active', preferences={}, created_at=clock.now())
        role = m.Role(name='Synthetic Greeter', ministry='Welcome',
            required_qualifications=['reviewed-clearance'], criticality='standard', fill_policy='needs_approval')
        kind = m.EventType(name='Demo Sunday (Test)', title_patterns=['exact original pattern'])
        session.add_all([person, role, kind]); session.flush()
        event = m.Event(title='Test Sunday Service [Fictional]', event_type_id=kind.id,
            starts_at=clock.now()+timedelta(days=1), ends_at=clock.now()+timedelta(days=1,hours=1),
            status='scheduled')
        recipe = m.RoleRecipe(event_type_id=kind.id, role_id=role.id, count=5)
        session.add_all([event, recipe]); session.flush()
        shift = m.Shift(event_id=event.id, role_id=role.id, slot_index=0)
        session.add(shift); session.flush()
        assignment = m.Assignment(volunteer_id=person.id, shift_id=shift.id, status='approved',
            source='manual', created_at=clock.now(), updated_at=clock.now())
        approval = confirmations.stage(session, clock.now(), {
            'phone':person.phone, 'body':'Test exact reviewed [Fictional] wording.', 'purpose':'manual'})
        history_body = '[Fictional history] Test exact original conversation.'
        session.add(m.Message(volunteer_id=person.id, phone=person.phone, body=history_body,
            direction='in', kind='synthetic', status='simulated', purpose='fictional_accept',
            created_at=clock.now()))
        session.add(assignment); session.commit()
        ids = person.id, role.id, kind.id, event.id, shift.id, assignment.id, recipe.id, approval.id
        before = deepcopy(approval.payload)
    auth = ('admin', 'isolated-password')
    for path in ['/', '/schedule?month=2026-10', '/needs', '/volunteers', '/approvals', f'/simulator?as={ids[0]}']:
        response = client.get(path, auth=auth)
        assert response.status_code == 200, path
        assert 'Test Jordan Ellis [Fictional]' not in response.text
        assert 'Synthetic Greeter' not in response.text
        assert 'Test Sunday Service [Fictional]' not in response.text
        if path in ['/', '/schedule?month=2026-10']:
            assert 'Sunday Service' in response.text and 'Jordan' in response.text
        if path == '/needs':
            assert 'Sunday' in response.text and f'/needs/recipe/{ids[6]}' in response.text
        if path == '/volunteers':
            assert 'Jordan Ellis' in response.text and 'opted out' in response.text
        if path == '/approvals':
            assert before['body'] in response.text
            assert f'/approvals/{ids[7]}/approve' in response.text
        if path.startswith('/simulator'):
            assert history_body in response.text
            assert f'/simulator/{ids[0]}/send' in response.text
    with app.state.session_factory() as session:
        assert session.get(m.Volunteer, ids[0]).name == 'Test Jordan Ellis [Fictional]'
        assert session.get(m.Role, ids[1]).name == 'Synthetic Greeter'
        assert session.get(m.Role, ids[1]).required_qualifications == ['reviewed-clearance']
        assert session.get(m.Role, ids[1]).fill_policy == 'needs_approval'
        assert session.get(m.EventType, ids[2]).title_patterns == ['exact original pattern']
        assert session.get(m.Event, ids[3]).title == 'Test Sunday Service [Fictional]'
        assert session.get(m.Shift, ids[4]).event_id == ids[3]
        assert session.get(m.Assignment, ids[5]).status == 'approved'
        assert session.get(m.Approval, ids[7]).payload == before
        assert before['content_hash'] == confirmations.digest(before)
    assert app.state.provider.sent == []


def test_labels_remain_escaped_distinct_rows_and_legitimate_name_words_survive(legacy):
    app, client, clock = legacy
    names = ['Test Jordan Ellis [Fictional]', 'Jordan Ellis',
             'Synthetic <img src=x onerror=alert(1)> [Fictional]', 'Testament Jones']
    with app.state.session_factory() as session:
        for i, name in enumerate(names):
            session.add(m.Volunteer(name=name, phone=f'+120255501{i:02}', sms_opt_in=False,
                status='inactive', preferences={}, created_at=clock.now()))
        session.commit()
    response = client.get('/volunteers', auth=('admin','isolated-password'))
    assert response.status_code == 200
    assert response.text.count('<strong>Jordan Ellis</strong>') == 2
    assert '&lt;img src=x onerror=alert(1)&gt;' in response.text
    assert '<img src=x' not in response.text
    assert 'Testament Jones' in response.text
    with app.state.session_factory() as session:
        assert [session.get(m.Volunteer, i+1).name for i in range(4)] == names
