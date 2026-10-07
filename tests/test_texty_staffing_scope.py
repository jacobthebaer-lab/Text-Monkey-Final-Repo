"""Read-only dashboard contracts for scheduled events without materialized shifts."""
from datetime import timedelta

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import func, select

from app.config import Settings
from app.db import models as m
from app.main import create_app
from app.web.routes import db
from app.web.texty import admin


@pytest.fixture
def state_client(session, clock):
    application = create_app(Settings(database_url='sqlite://', demo_mode=True, automation_enabled=False))
    application.state.clock = clock
    application.dependency_overrides[admin] = lambda: {'email': 'coordinator@example.test'}
    application.dependency_overrides[db] = lambda: session
    with TestClient(application) as client:
        yield client, application


def event_row(session, clock, title='Sample service', *, offset=timedelta(days=1), status='scheduled', event_type=None):
    row = m.Event(title=title, starts_at=clock.now()+offset, ends_at=clock.now()+offset+timedelta(hours=1),
                  status=status, event_type_id=event_type.id if event_type else None)
    session.add(row)
    session.flush()
    return row


def recipe(session, count=5):
    kind = m.EventType(name='Sample service type', title_patterns=[])
    role = m.Role(name='Production', ministry='Production', required_qualifications=[],
                  criticality='standard', fill_policy='auto')
    session.add_all([kind, role])
    session.flush()
    session.add(m.RoleRecipe(event_type_id=kind.id, role_id=role.id, count=count))
    session.flush()
    return kind, role


def get_state(client):
    response = client.get('/api/state')
    assert response.status_code == 200, response.text
    return response.json()


def test_zero_shift_event_reports_recipe_gaps_without_creating_slots_or_outreach(session, clock, state_client):
    kind, _ = recipe(session)
    row = event_row(session, clock, event_type=kind)
    session.commit()
    models = [m.Event, m.EventType, m.Role, m.RoleRecipe, m.Shift, m.Assignment, m.Message, m.Notification]
    counts = lambda: {model.__name__:session.scalar(select(func.count()).select_from(model)) for model in models}
    before = counts()
    client, application = state_client
    result = get_state(client)
    assert result['shifts'] == [] and result['assignments'] == []
    assert result['staffing'] == [{'event_id':str(row.id), 'title':row.title, 'starts_at':row.starts_at.isoformat(),
                                   'covered':0, 'required':5, 'fully_staffed':False,
                                   'gaps':[{'role':'Production', 'open':5}]}]
    assert counts() == before
    assert application.state.provider.sent == []


@pytest.mark.parametrize('status,offset', [('cancelled', timedelta(days=1)), ('completed', timedelta(days=1)),
                                          ('scheduled', timedelta(seconds=-1))])
def test_inactive_or_past_events_are_excluded_even_when_shift_rows_exist(session, clock, state_client, status, offset):
    kind, role = recipe(session)
    omitted = event_row(session, clock, 'Excluded event', status=status, offset=offset, event_type=kind)
    session.add(m.Shift(event_id=omitted.id, role_id=role.id, slot_index=0))
    visible = event_row(session, clock, 'Upcoming event', event_type=kind)
    session.commit()
    result = get_state(state_client[0])
    assert [row['event_id'] for row in result['staffing']] == [str(visible.id)]
    assert result['shifts'] == []


def test_event_at_current_window_start_remains_visible(session, clock, state_client):
    kind, _ = recipe(session)
    row = event_row(session, clock, offset=timedelta(0), event_type=kind)
    session.commit()
    assert [event['event_id'] for event in get_state(state_client[0])['staffing']] == [str(row.id)]


def test_upcoming_window_is_bounded_and_shift_rows_share_the_selected_event_scope(session, clock, state_client):
    kind, role = recipe(session)
    rows = [event_row(session, clock, f'Upcoming {i}', offset=timedelta(minutes=i+1), event_type=kind) for i in range(161)]
    session.add(m.Shift(event_id=rows[-1].id, role_id=role.id, slot_index=0))
    session.commit()
    result = get_state(state_client[0])
    assert [event['event_id'] for event in result['staffing']] == [str(row.id) for row in rows[:160]]
    assert result['shifts'] == []
    assert session.scalar(select(func.count()).select_from(m.Event)) == 161
    assert session.scalar(select(func.count()).select_from(m.Shift)) == 1


@pytest.mark.parametrize('typed', [False, True])
def test_zero_shift_events_without_recipe_keep_the_missing_plan_boundary(session, clock, state_client, typed):
    kind, _ = recipe(session)
    if typed:
        kind = m.EventType(name='No saved recipe', title_patterns=[])
        session.add(kind)
        session.flush()
    row = event_row(session, clock, event_type=kind if typed else None)
    session.commit()
    result = get_state(state_client[0])
    assert result['staffing'][0]['event_id'] == str(row.id)
    assert result['staffing'][0]['required'] == result['staffing'][0]['covered'] == 0
    assert result['staffing'][0]['gaps'] == []
    assert result['shifts'] == []
    assert session.get(m.Event, row.id).event_type_id == (kind.id if typed else None)
    assert session.scalar(select(func.count()).select_from(m.RoleRecipe)) == 1
