"""Adversarial sync regressions with synthetic resources and disposable databases."""
from copy import deepcopy
from datetime import timedelta

import pytest
from sqlalchemy.orm import sessionmaker

from app.db.models import Event, Policy
from app.integrations.planning_center import PCOBase, PCOEventLink, PCOVolunteerPerson
from app.integrations.planning_center_sync import (
    AVAILABILITY_PREFIX, native_availability_problem, refresh_mapped_availability,
    sync_linked_events, sync_mapped_availability,
)
from tests.test_planning_center import CONFIG
from tests.test_planning_center_sync import linked


class ManyEventsAPI:
    def __init__(self, events):
        self.events = events
        self.reads = set()

    def organization(self):
        return {'id': '10'}

    def request(self, method, path, **kwargs):
        assert method == 'GET'
        plan = path.rsplit('/', 1)[1]
        self.reads.add(plan)
        return {'data': {'id': plan, 'attributes': {'title': self.events[plan].title},
            'relationships': {'service_type': {'data': {'id': '20'}}}}}

    def collection(self, path):
        plan = path.split('/')[-2]
        event = self.events[plan]
        return [{'id': '60', 'attributes': {'time_type': 'service',
            'starts_at': event.starts_at.isoformat(), 'ends_at': event.ends_at.isoformat()}}]


def test_bounded_event_ticks_eventually_visit_links_after_first_hundred(session, clock, tmp_path):
    PCOBase.metadata.create_all(session.get_bind())
    events = {}
    for i in range(101):
        plan = str(1000 + i)
        key = f'10:20:{plan}:60'
        event = Event(gcal_event_id='pco:' + key, title='Service ' + plan,
            starts_at=clock.now()+timedelta(days=3),
            ends_at=clock.now()+timedelta(days=3, hours=1), status='scheduled')
        session.add(event); session.flush()
        session.add(PCOEventLink(key=key, organization_id='10', service_type_id='20',
            plan_id=plan, event_id=event.id))
        events[plan] = event
    session.commit()
    api = ManyEventsAPI(events)
    factory = sessionmaker(bind=session.get_bind(), expire_on_commit=False)
    assert sync_linked_events(factory, api, CONFIG, clock.now())['baselined'] == 100
    # Restart with a separate file-backed engine, without any Python cursor state.
    import sqlite3
    from app.db.session import make_engine, make_session_factory
    database = tmp_path / 'synthetic-event-sync.sqlite'
    connection = session.get_bind().raw_connection()
    with sqlite3.connect(database) as destination:
        connection.driver_connection.backup(destination)
    connection.close()
    restarted_engine = make_engine('sqlite:///' + str(database))
    try:
        report = sync_linked_events(make_session_factory(restarted_engine), api, CONFIG, clock.now())
        assert report['baselined'] == 1 and report['unchanged'] == 99
    finally:
        restarted_engine.dispose()
    assert api.reads == set(events), 'The 101st event must not starve behind old links'


class EmptyAvailabilityAPI:
    def organization(self):
        return {'id': '10'}

    def collection(self, path):
        assert path == '/services/v2/people/70/blockouts'
        return []


@pytest.fixture
def cached_availability(session, clock, make_volunteer, make_shift):
    PCOBase.metadata.create_all(session.get_bind())
    volunteer, shift = make_volunteer(), make_shift()
    mapping = PCOVolunteerPerson(organization_id='10', volunteer_id=volunteer.id,
        person_id='70', created_at=clock.now())
    session.add(mapping); session.flush()
    assert refresh_mapped_availability(session, EmptyAvailabilityAPI(), CONFIG,
        clock.now())['refreshed'] == 1
    session.commit()
    assert native_availability_problem(session, volunteer, shift) is None
    return volunteer, shift, mapping


@pytest.mark.parametrize('change', ['person', 'organization', 'deleted', 'phone', 'recreated'])
def test_cached_empty_availability_cannot_clear_a_changed_identity(
    session, cached_availability, change
):
    volunteer, shift, mapping = cached_availability
    if change == 'person':
        mapping.person_id = '71'
    elif change == 'organization':
        mapping.organization_id = '11'
    elif change == 'deleted':
        session.delete(mapping)
    elif change == 'phone':
        volunteer.phone = '+15559999999'
    else:
        session.delete(mapping); session.flush()
        session.add(PCOVolunteerPerson(organization_id='10', volunteer_id=volunteer.id,
            person_id='70', created_at=mapping.created_at+timedelta(minutes=1)))
    session.commit()
    assert native_availability_problem(session, volunteer, shift) is not None


@pytest.mark.parametrize('bad_value', [[], {'intervals': None},
    {'intervals': [{'starts_at': '2026-10-04T18:00:00Z',
                    'ends_at': '2026-10-04T17:00:00Z'}]}])
def test_malformed_availability_is_a_hold_instead_of_clearance_or_exception(
    session, cached_availability, bad_value
):
    volunteer, shift, _ = cached_availability
    state = session.get(Policy, AVAILABILITY_PREFIX + str(volunteer.id))
    value = deepcopy(state.value)
    if isinstance(bad_value, dict):
        value.update(bad_value)
    else:
        value = bad_value
    state.value = value; session.commit()
    assert native_availability_problem(session, volunteer, shift) is not None


def test_refresh_recovers_malformed_availability_cache(session, clock, cached_availability):
    volunteer, _, _ = cached_availability
    session.get(Policy, AVAILABILITY_PREFIX + str(volunteer.id)).value = ['corrupt']
    session.commit()
    report = refresh_mapped_availability(session, EmptyAvailabilityAPI(), CONFIG, clock.now())
    assert report['refreshed'] == 1
    assert isinstance(session.get(Policy, AVAILABILITY_PREFIX + str(volunteer.id)).value, dict)


def test_runtime_cannot_clear_mapped_person_before_first_refresh(
    session, clock, cached_availability, make_volunteer
):
    volunteer, shift, _ = cached_availability
    session.delete(session.get(Policy, AVAILABILITY_PREFIX + str(volunteer.id)))
    session.commit()
    session.info['pco_availability_clock'] = clock
    assert native_availability_problem(session, volunteer, shift) is not None
    # Native availability does not impose a new mapping on ordinary local users.
    assert native_availability_problem(session, make_volunteer(), shift) is None


def test_untitled_native_plan_uses_same_service_title_as_schedule_import(session, clock, linked):
    event, api, factory = linked
    original = api.request
    api.title = ''
    def request(method, path, **kwargs):
        if path == '/services/v2/service_types/20':
            assert method == 'GET'
            return {'data': {'type': 'ServiceType', 'id': '20',
                'attributes': {'name': event.title}}}
        return original(method, path, **kwargs)
    api.request = request
    # fetch_schedule already uses ServiceType.name for these native plans.
    assert sync_linked_events(factory, api, CONFIG, clock.now())['unchanged'] == 1
    session.expire_all()
    assert event.title == 'Sunday Service'


def test_availability_worker_does_not_hold_sqlite_writer_during_http(
    session, clock, cached_availability, tmp_path, monkeypatch
):
    import sqlite3
    from app.config import Settings
    from app.db.session import make_engine, make_session_factory
    from app.integrations import planning_center_sync as sync
    volunteer, _, _ = cached_availability
    session.delete(session.get(Policy, AVAILABILITY_PREFIX + str(volunteer.id)))
    session.add(Policy(key='synthetic_concurrent_writer', value=0)); session.commit()
    database = tmp_path / 'synthetic-availability.sqlite'
    source = session.get_bind().raw_connection()
    with sqlite3.connect(database) as destination:
        source.driver_connection.backup(destination)
    source.close()
    engine = make_engine('sqlite:///' + str(database))
    class API(EmptyAvailabilityAPI):
        def __enter__(self): return self
        def __exit__(self, *args): pass
        def collection(self, path):
            # A different runtime operation must be able to commit while the
            # native GET is in flight, rather than wait for its network timeout.
            with sqlite3.connect(database, timeout=0) as writer:
                writer.execute("update policies set value='1' where key='synthetic_concurrent_writer'")
            return super().collection(path)
    monkeypatch.setattr(sync, 'sync_schedule', lambda *a, **k: {})
    monkeypatch.setattr(sync, 'sync_linked_events', lambda *a, **k: {})
    monkeypatch.setattr(sync, 'sync_mapped_names', lambda *a, **k: {})
    try:
        result = sync.sync_metadata_tick(make_session_factory(engine),
            Settings(pco_sync_enabled=True), CONFIG, clock.now(), client_factory=lambda config: API())
        assert result['availability']['refreshed'] == 1
    finally:
        engine.dispose()


@pytest.mark.parametrize('change', ['mapping', 'phone', 'cache'])
def test_availability_get_cannot_replace_a_concurrently_changed_snapshot(
    session, clock, cached_availability, change
):
    from app.db.models import Volunteer
    volunteer, shift, mapping = cached_availability
    factory = sessionmaker(bind=session.get_bind(), expire_on_commit=False)
    key = AVAILABILITY_PREFIX + str(volunteer.id)
    original = deepcopy(session.get(Policy, key).value)
    class API(EmptyAvailabilityAPI):
        def collection(self, path):
            with factory() as editor:
                if change == 'mapping':
                    editor.get(PCOVolunteerPerson, mapping.id).person_id = '71'
                elif change == 'phone':
                    editor.get(Volunteer, volunteer.id).phone = '+15559999999'
                else:
                    state = editor.get(Policy, key)
                    state.value = {**state.value, 'reason': 'Newer explicitly saved hold'}
                editor.commit()
            return super().collection(path)
    report = sync_mapped_availability(factory, API(), CONFIG, clock.now()+timedelta(minutes=1))
    assert report == {'refreshed': 0, 'held': 1}
    session.expire_all()
    saved = session.get(Policy, key).value
    assert saved == ({**original, 'reason': 'Newer explicitly saved hold'} if change == 'cache' else original)
    assert native_availability_problem(session, volunteer, shift) is not None


def test_runtime_availability_refresh_recovers_legacy_or_malformed_cache(
    session, clock, cached_availability
):
    volunteer, shift, _ = cached_availability
    session.get(Policy, AVAILABILITY_PREFIX + str(volunteer.id)).value = ['corrupt']
    session.commit()
    factory = sessionmaker(bind=session.get_bind(), expire_on_commit=False)
    assert sync_mapped_availability(factory, EmptyAvailabilityAPI(), CONFIG, clock.now()) == {
        'refreshed': 1, 'held': 0}
    session.expire_all()
    assert native_availability_problem(session, volunteer, shift) is None


@pytest.mark.parametrize('organization_id', [[], {}, ['10'], 10, '', 'another church'])
def test_malformed_cached_organization_holds_before_sql_binding(
    session, cached_availability, organization_id
):
    volunteer, shift, _ = cached_availability
    state = session.get(Policy, AVAILABILITY_PREFIX + str(volunteer.id))
    state.value = {**state.value, 'organization_id': organization_id}; session.commit()
    assert native_availability_problem(session, volunteer, shift) is not None
