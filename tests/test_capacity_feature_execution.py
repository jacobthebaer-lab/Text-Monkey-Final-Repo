"""Execute capacity features with changed fixtures and saved evidence, offline."""
import json
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

import pytest
from sqlalchemy import select

from app.agents.capacity_agent import scan
from app.db import models as m
from app.db.seed import seed
from tests.test_capacity_narration import ctx

CASES = [
    ('single_point_of_failure', 'single-point'), ('burnout', 'burnout'), ('drop_off', 'dropoff'),
    ('expiring', 'expiry'), ('chronic_gap', 'chronic-gaps'), ('untapped', 'untapped'),
    ('unused_skill', 'unused-skills'), ('growing_need', 'growing-needs'), ('rebalance', 'ministry-rebalance'),
]


def key(flag):
    return flag.evidence['key']


def add_service(session, clock, person, role):
    event = m.Event(title='Synthetic historical service', status='completed',
        starts_at=clock.now()-timedelta(days=1), ends_at=clock.now()-timedelta(days=1)+timedelta(hours=1))
    session.add(event); session.flush()
    shift = m.Shift(event_id=event.id, role_id=role.id, slot_index=0)
    session.add(shift); session.flush()
    session.add(m.Assignment(shift_id=shift.id, volunteer_id=person.id, status='completed', source='admin',
        created_at=event.starts_at, updated_at=event.ends_at))


def add_pool(session, clock, roles, *, count=4):
    qualification_types = {name for role in roles for name in role.required_qualifications}
    for index in range(count):
        person = m.Volunteer(name=f'Synthetic pool member {index}', phone=f'+1555099{index:04}',
            status='active', sms_opt_in=True, is_coordinator=False, is_pastor=False,
            created_at=clock.now()-timedelta(days=60),
            preferences={'interested_roles': [role.name for role in roles], 'max_per_month': 8})
        session.add(person); session.flush()
        for name in qualification_types:
            session.add(m.Qualification(volunteer_id=person.id, type=name, status='verified',
                verified_by='Synthetic fixture administrator', verified_at=clock.now()-timedelta(days=2)))


@pytest.mark.parametrize('kind,feature_id', CASES)
def test_capacity_flag_has_real_evidence_and_clears_scan_when_its_source_changes(
    session, clock, provider, tmp_path, record_property, kind, feature_id
):
    seed(session)
    initial = scan(ctx(session, clock, provider, tmp_path))
    flag = next(row for row in initial if row.type == kind)
    original_key = key(flag)
    original_evidence = dict(flag.evidence)
    assert flag.status == 'open' and flag.evidence['narration']['state'] == 'ready'
    assert flag.evidence['observation'] in flag.summary
    assert flag.suggested_action == flag.evidence['next_step']
    count = len(session.scalars(select(m.Flag)).all())
    repeat = scan(ctx(session, clock, provider, tmp_path))
    assert len(session.scalars(select(m.Flag)).all()) == count
    assert sum(key(row) == original_key for row in repeat) == 1
    facts = original_evidence
    if kind == 'burnout':
        person = session.get(m.Volunteer, facts['volunteer_id'])
        person.preferences = {**person.preferences, 'max_per_month': facts['count']}
    elif kind == 'expiring':
        session.get(m.Qualification, facts['qualification_id']).expires_on = clock.now().date()+timedelta(days=31)
    elif kind in {'drop_off', 'untapped', 'unused_skill'}:
        person = session.get(m.Volunteer, facts['volunteer_id'])
        role = session.get(m.Role, facts.get('role_id')) if kind == 'unused_skill' else session.scalar(select(m.Role))
        add_service(session, clock, person, role)
    elif kind == 'chronic_gap':
        person = session.scalar(select(m.Volunteer).where(m.Volunteer.is_coordinator.is_(False)))
        for shift_id in facts['shift_ids']:
            shift = session.get(m.Shift, shift_id)
            session.add(m.Assignment(shift_id=shift_id, volunteer_id=person.id, status='completed', source='admin',
                created_at=shift.starts_at, updated_at=shift.ends_at))
    elif kind == 'single_point_of_failure':
        role = session.get(m.Role, facts['role_id'])
        for assignment in session.scalars(select(m.Assignment).join(m.Shift).where(m.Shift.role_id == role.id)):
            assignment.status = 'cancelled'
        add_pool(session, clock, [role])
    elif kind == 'growing_need':
        role = session.get(m.Role, facts['role_id'])
        add_pool(session, clock, [role], count=max(4, (facts['slots']-facts['capacity'])//16+1))
    elif kind == 'rebalance':
        add_pool(session, clock, session.scalars(select(m.Role)).all())
    session.commit()
    # Persist and reload every source before the second decision.
    session.expire_all()
    after = scan(ctx(session, clock, provider, tmp_path))
    assert original_key not in {key(row) for row in after}
    assert not provider.sent and not session.scalars(select(m.Message).where(m.Message.direction == 'out')).all()
    record_property('feature_ids', json.dumps([feature_id, 'capacity-ai', 'flag-management']))
    record_property('before_flag_evidence', json.dumps(original_evidence, default=str, sort_keys=True))
    record_property('after_current_flag_keys', json.dumps(sorted(key(row) for row in after)))
    record_property('expected', 'Source-triggered flag appears once, refreshed scan drops it after changed facts; historical review evidence remains. No contacts or automatic qualification grants.')
    record_property('native_dispatches', 0)


def test_dropoff_counts_church_local_months_after_a_real_database_reload(
    session, clock, provider, make_volunteer, make_shift, assign, tmp_path, record_property
):
    zone = ZoneInfo('America/Denver')
    clock.set_time(datetime(2026, 8, 20, 10, tzinfo=zone))
    person = make_volunteer(prefs={'interested_roles': ['Greeter']})
    for month, days in [(4, [29, 30]), (5, [30, 31]), (6, [29, 30])]:
        for day in days:
            shift = make_shift('Greeter', starts=datetime(2026, month, day, 20, tzinfo=zone))
            assign(person, shift, status='completed')
    person_id = person.id
    session.commit(); session.expunge_all()
    flags = scan(ctx(session, clock, provider, tmp_path))
    dropout = [flag for flag in flags if flag.type == 'drop_off' and flag.evidence['volunteer_id'] == person_id]
    assert len(dropout) == 1
    assert dropout[0].evidence['prior_month_counts'] == {'2026-04': 2, '2026-05': 2, '2026-06': 2}
    assert not provider.sent
    record_property('feature_ids', json.dumps(['dropoff', 'timezone-dst', 'capacity-ai']))
    record_property('persisted_month_counts', json.dumps(dropout[0].evidence['prior_month_counts']))


def test_qualification_expiry_and_pool_use_church_local_today_not_clock_offset(
    session, clock, provider, make_volunteer, make_shift, tmp_path, record_property
):
    from datetime import date, timezone
    clock.set_time(datetime(2026, 10, 2, 2, tzinfo=timezone.utc))  # Still October 1 in Denver.
    role = make_shift('Greeter', required=['synthetic_training']).role
    for _ in range(4):
        make_volunteer(prefs={'interested_roles': ['Greeter']},
            quals=[('synthetic_training', 'verified', date(2026, 10, 1))])
    session.commit(); session.expunge_all()
    flags = scan(ctx(session, clock, provider, tmp_path))
    assert len([flag for flag in flags if flag.type == 'expiring']) == 4
    assert not any(flag.type == 'single_point_of_failure' and flag.evidence['role_id'] == role.id for flag in flags)
    assert all(row.status == 'verified' for row in session.scalars(select(m.Qualification)))
    assert not provider.sent
    record_property('feature_ids', json.dumps(['expiry', 'single-point', 'timezone-dst']))
    record_property('church_date', '2026-10-01')
    record_property('clock_date', '2026-10-02')
