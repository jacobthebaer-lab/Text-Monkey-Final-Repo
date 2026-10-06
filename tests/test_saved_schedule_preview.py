"""Saved fictional settings feed preview and real jobs, with mock transport only."""
from datetime import datetime
import json
from pathlib import Path
import subprocess
import sys

import pytest
from sqlalchemy import select

from app.agents.fill_agent import FillContext
from app.core import confirmations, reminders
from app.core.demo_schedule import preview
from app.db import models as m
from app.jobs import process_due_fill_requests
from tests.test_exact_day_before_reminder import ExactGloo
from tests.test_admin_setup import setup_client, save, DETAILS

SETTINGS = {'timezone': 'America/Denver', 'service_times': 'Sunday 9AM / 11AM'}


def test_saved_admin_settings_feed_preview_without_creating_records(setup_client):
    client, app, _ = setup_client
    save(client, {**DETAILS, **SETTINGS}, complete=True)
    saved = client.get('/api/setup').json()['details']
    plan = preview(saved, '2026-10-11', 75, 1)
    assert plan['source']['service_times'] == saved['service_times']
    assert [r['starts_at'] for r in plan['events']] == ['2026-10-11T09:00:00-06:00', '2026-10-11T11:00:00-06:00']
    assert plan['events'][0]['ends_at'] == '2026-10-11T10:15:00-06:00'
    assert plan['events'][0]['admin_update_window_opens_at'] == '2026-10-11T06:00:00-06:00'
    assert plan['events'][1]['volunteer_reminder_local_date'] == '2026-10-10'
    assert plan['assignments'] == plan['messages'] == []
    assert not plan['delivery_verified'] and not plan['scheduler_verified']
    assert 'coordinator_phone' not in json.dumps(plan)
    with app.state.session_factory() as session:
        for cls in (m.Event, m.Shift, m.Assignment, m.Message, m.Approval):
            assert session.scalar(select(cls)) is None


@pytest.mark.parametrize('times', ['9AM / 11AM', 'Sunday 9 / 11', 'Sunday 9AM / 9AM',
    'Sunday 9AM and Wednesday 7PM', 'Sunday 25PM', 'Sunday 9AM?', 'Sunday 9AM /'])
def test_ambiguous_preferences_are_held(times):
    with pytest.raises(ValueError):
        preview({**SETTINGS, 'service_times':times}, '2026-10-11', 75, 1)


@pytest.mark.parametrize('day,duration,role', [('2026-10-12',75,1), ('2026-10-11',0,1),
    ('2026-10-11',True,1), ('2026-10-11',75,0)])
def test_explicit_date_duration_and_role_are_required(day,duration,role):
    with pytest.raises(ValueError):
        preview(SETTINGS, day, duration, role)


def test_clock_change_is_not_guessed():
    with pytest.raises(ValueError, match='clock-change'):
        preview({**SETTINGS,'service_times':'Sunday 1:30AM'}, '2026-11-01', 75, 1)
    assert preview(SETTINGS, '2026-11-01', 75, 1)['events'][0]['starts_at'].endswith('-07:00')


def test_same_two_service_fixture_stages_event_bound_reminders_and_dedupes_admin_updates(
    session,clock,provider,make_volunteer,assign,tmp_path
):
    role = m.Role(name='greeter', ministry='Fictional demo', required_qualifications=[],
                  criticality='standard', fill_policy='auto')
    session.add(role); session.flush()
    plan = preview(SETTINGS, '2026-10-04', 75, role.id)
    admin = make_volunteer('Fictional Admin', coordinator=True)
    for data in plan['events']:
        event = m.Event(title=data['title'], status=data['status'],
            starts_at=datetime.fromisoformat(data['starts_at']), ends_at=datetime.fromisoformat(data['ends_at']))
        session.add(event); session.flush()
        shift = m.Shift(event_id=event.id, **data['role_slots'][0])
        session.add(shift); session.flush()
        assign(make_volunteer(), shift, status='confirmed')
    session.commit(); session.expire_all()
    clock.set_time(datetime.fromisoformat('2026-10-03T10:00:00-06:00'))
    session.info[confirmations.MODE_KEY] = True
    model = ExactGloo()
    ctx = FillContext(session,clock,provider,model,log_dir=tmp_path)
    reminders.process(ctx)
    reviews = session.scalars(select(m.Approval).where(m.Approval.kind=='confirm_text')).all()
    assert len(reviews) == 2 and not provider.sent
    assert {r.payload['conversation']['source']['starts_at'] for r in reviews} == {
        '2026-10-04T15:00:00+00:00','2026-10-04T17:00:00+00:00'}
    assert all(r.payload['purpose']=='reminder' for r in reviews)
    reminders.process(ctx)
    assert model.calls == 2
    # Only this synthetic fixture disables exact mode for the existing mock
    # admin-update replay. The preview/CLI never changes runtime policy.
    session.info[confirmations.MODE_KEY] = False
    clock.set_time(datetime.fromisoformat('2026-10-04T08:00:00-06:00'))
    process_due_fill_requests(ctx)
    session.commit(); session.expire_all()
    process_due_fill_requests(ctx)
    assert len(provider.sent_to(admin.phone)) == 2
    assert len(provider.sent) == 2
    assert all('All set:' in msg.body and '\u2014' not in msg.body for msg in provider.sent)


def test_cli_only_emits_schedule_fields_and_leaves_input_unchanged(tmp_path):
    path = tmp_path/'private-settings.json'
    original = json.dumps({'details':{**SETTINGS,'coordinator_phone':'private-marker'}})
    path.write_text(original)
    tool = Path(__file__).resolve().parents[1]/'tools/preview_saved_schedule.py'
    run = subprocess.run([sys.executable,str(tool),'--settings-json',str(path),
        '--sunday','2026-10-11','--duration-minutes','75','--role-id','1'],capture_output=True,text=True)
    assert run.returncode == 0, run.stderr
    assert 'private-marker' not in run.stdout
    assert json.loads(run.stdout)['state'] == 'requires_exact_record_review'
    assert path.read_text() == original and list(tmp_path.iterdir()) == [path]
