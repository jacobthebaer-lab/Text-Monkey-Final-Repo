from copy import deepcopy
from datetime import timedelta

import pytest
import httpx
from sqlalchemy import select
from sqlalchemy.orm import sessionmaker

from app.core.eligibility import check
from app.db.models import Event, Policy, Role, Shift
from app.integrations.planning_center import PCOBase, PCOEventLink, PCOVolunteerPerson, PlanningCenterError
from app.integrations.planning_center_sync import (
    PREFIX, refresh_mapped_availability, sync_linked_events, sync_mapped_names,
)
from tests.test_planning_center import CONFIG, client, sync_schedule


def test_metadata_only_job_imports_and_reconciles_without_delivery(session, clock):
    from app.config import Settings
    from app.jobs import process_pco_staffing
    from app.db.models import Message
    from app.integrations.planning_center import PCOClient
    from tests.test_planning_center import fixture_api
    original = fixture_api()
    def respond(request):
        if request.url.path == '/services/v2/service_types/20/plans/40':
            return httpx.Response(200, json={'data': {'id': '40',
                'attributes': {'title': 'Synthetic Sunday'},
                'relationships': {'service_type': {'data': {'id': '20'}}}}})
        return original.handle_request(request)
    PCOBase.metadata.create_all(session.get_bind())
    factory = sessionmaker(bind=session.get_bind(), expire_on_commit=False)
    settings = Settings(pco_sync_enabled=True, automation_enabled=False)
    make_client = lambda config: PCOClient(config, transport=httpx.MockTransport(respond))
    first = process_pco_staffing(factory, settings, CONFIG, clock, client_factory=make_client)
    assert first['sync']['import']['events_created'] == 1
    assert first['sync']['events']['baselined'] == 1
    second = process_pco_staffing(factory, settings, CONFIG, clock, client_factory=make_client)
    assert second['sync']['events']['unchanged'] == 1
    assert second['sync']['import']['events_created'] == 0
    assert not list(session.scalars(select(Message)))


class EventAPI:
    def __init__(self, event):
        self.title = event.title
        self.start, self.end = event.starts_at.isoformat(), event.ends_at.isoformat()
        self.writes = []
        self.timeout_after_patch = False
        self.blocks = []

    def organization(self):
        return {'id': '10'}

    def request(self, method, path, *, data=None):
        if method == 'GET':
            assert path == '/services/v2/service_types/20/plans/40'
            return {'data': {'id': '40', 'attributes': {'title': self.title},
                'relationships': {'service_type': {'data': {'id': '20'}}}}}
        assert method == 'PATCH'
        self.writes.append((path, deepcopy(data)))
        attrs = data['data']['attributes']
        if path.endswith('/plans/40'):
            self.title = attrs['title']
        else:
            assert path == '/services/v2/service_types/20/plan_times/60'
            self.start = attrs.get('starts_at', self.start)
            self.end = attrs.get('ends_at', self.end)
        if self.timeout_after_patch:
            raise PlanningCenterError('Synthetic response lost after mutation')
        return {'data': {}}

    def collection(self, path):
        if path == '/services/v2/service_types/20/plans/40/plan_times':
            return [{'id': '60', 'attributes': {'time_type': 'service',
                'starts_at': self.start, 'ends_at': self.end}}]
        if path == '/services/v2/people/70/blockouts':
            return deepcopy(self.blocks)
        assert path == '/services/v2/people/70/blockouts/90/blockout_dates'
        return [{'type': 'BlockoutDate', 'attributes': {
            'starts_at_utc': self.start, 'ends_at_utc': self.end}}]


@pytest.fixture
def linked(session, clock):
    PCOBase.metadata.create_all(session.get_bind())
    event = Event(gcal_event_id='pco:10:20:40:60', title='Sunday Service',
        starts_at=clock.now()+timedelta(days=3), ends_at=clock.now()+timedelta(days=3, hours=1),
        status='scheduled')
    session.add(event); session.flush()
    session.add(PCOEventLink(key='10:20:40:60', organization_id='10',
        service_type_id='20', plan_id='40', event_id=event.id)); session.commit()
    api = EventAPI(event)
    factory = sessionmaker(bind=session.get_bind(), expire_on_commit=False)
    assert sync_linked_events(factory, api, CONFIG, clock.now())['baselined'] == 1
    return event, api, factory


def test_local_and_native_event_edits_reconcile_both_directions(session, clock, linked):
    event, api, factory = linked
    event.title = 'Sunday Worship'
    event.starts_at += timedelta(hours=1); event.ends_at += timedelta(hours=1)
    session.commit()
    assert sync_linked_events(factory, api, CONFIG, clock.now(), write_enabled=True)['pushed'] == 1
    assert api.title == 'Sunday Worship' and len(api.writes) == 2
    assert all(set(body['data']['attributes']) <= {'title', 'starts_at', 'ends_at'} for _, body in api.writes)
    api.title = 'Community Worship'
    assert sync_linked_events(factory, api, CONFIG, clock.now(), write_enabled=True)['pulled'] == 1
    session.expire_all()
    assert event.title == 'Community Worship'
    assert sync_linked_events(factory, api, CONFIG, clock.now(), write_enabled=True)['unchanged'] == 1
    assert len(api.writes) == 2


def test_conflicting_edits_hold_both_records(session, clock, linked):
    event, api, factory = linked
    event.title = 'Local edit'; session.commit()
    api.title = 'Native edit'
    assert sync_linked_events(factory, api, CONFIG, clock.now(), write_enabled=True)['held'] == 1
    session.expire_all()
    assert event.title == 'Local edit' and api.title == 'Native edit' and not api.writes


def test_lost_patch_response_reconciles_without_duplicate_write(session, clock, linked):
    event, api, factory = linked
    event.title = 'Sunday Worship'; session.commit()
    api.timeout_after_patch = True
    assert sync_linked_events(factory, api, CONFIG, clock.now(), write_enabled=True)['held'] == 1
    api.timeout_after_patch = False
    assert sync_linked_events(factory, api, CONFIG, clock.now(), write_enabled=True)['pushed'] == 1
    assert len(api.writes) == 1
    session.expire_all()
    assert session.get(Policy, PREFIX+'10:20:40:60').value['pending'] is None


def test_recurring_import_does_not_overwrite_unsynced_local_edit(session):
    PCOBase.metadata.create_all(session.get_bind())
    with client() as api:
        sync_schedule(session, api, CONFIG)
        event = session.scalar(select(Event)); event.title = 'Local edit'
        sync_schedule(session, api, CONFIG, create_only=True)
    assert event.title == 'Local edit' and event.status == 'scheduled'


def test_native_blockouts_affect_eligibility_without_changing_consent_or_local_dates(
    session, clock, linked, make_volunteer
):
    event, api, factory = linked
    vol = make_volunteer()
    role = Role(name='Greeter', ministry='Welcome', required_qualifications=[],
        criticality='standard', fill_policy='auto_send')
    session.add(role); session.flush()
    shift = Shift(event_id=event.id, role_id=role.id, slot_index=0)
    session.add(shift)
    session.add(PCOVolunteerPerson(organization_id='10', volunteer_id=vol.id,
        person_id='70', created_at=clock.now())); session.flush()
    assert check(session, vol, shift)
    api.blocks = [{'type': 'Blockout', 'id': '90', 'attributes': {'repeat_frequency': 'no_repeat'},
        'relationships': {k: {'data': {'id': v}} for k, v in (('person', '70'), ('organization', '10'))}}]
    assert refresh_mapped_availability(session, api, CONFIG, clock.now())['refreshed'] == 1
    assert 'Unavailable in Planning Center for this interval' in check(session, vol, shift).reasons
    api.blocks = []
    refresh_mapped_availability(session, api, CONFIG, clock.now())
    assert check(session, vol, shift) and vol.sms_opt_in and vol.preferences == {}
    session.info['pco_availability_clock'] = clock
    clock.advance(timedelta(minutes=6))
    assert 'Planning Center availability refresh is overdue' in check(session, vol, shift).reasons


class ProfileAPI:
    def __init__(self, volunteer):
        self.name, self.phone = volunteer.name, volunteer.phone
        self.writes = []

    def organization(self): return {'id': '10'}

    def request(self, method, path, *, data=None):
        if path == '/people/v2': return {'data': {'id': '10'}}
        assert path == '/people/v2/people/70'
        if method == 'PATCH':
            attrs = data['data']['attributes']
            assert set(attrs) == {'first_name', 'last_name'}
            self.writes.append(deepcopy(data))
            self.name = attrs['first_name'] + ' ' + attrs['last_name']
        return {'data': {'type': 'Person', 'id': '70', 'attributes': {'name': self.name}}}

    def collection(self, path):
        assert path == '/people/v2/people/70/phone_numbers'
        return [{'type': 'PhoneNumber', 'id': '91', 'attributes': {'e164': self.phone},
            'relationships': {'person': {'data': {'id': '70'}}}}]


def test_existing_mapped_profiles_sync_names_both_directions(session, clock, make_volunteer):
    PCOBase.metadata.create_all(session.get_bind())
    volunteer = make_volunteer(name='John Blair')
    session.add(PCOVolunteerPerson(organization_id='10', volunteer_id=volunteer.id,
        person_id='70', created_at=clock.now())); session.commit()
    api = ProfileAPI(volunteer)
    factory = sessionmaker(bind=session.get_bind(), expire_on_commit=False)
    assert sync_mapped_names(factory, api, CONFIG, clock.now())['baselined'] == 1
    volunteer.name = 'Jonathan Blair'; session.commit()
    assert sync_mapped_names(factory, api, CONFIG, clock.now(), write_enabled=True)['pushed'] == 1
    assert api.name == 'Jonathan Blair'
    api.name = 'John Blair'
    assert sync_mapped_names(factory, api, CONFIG, clock.now(), write_enabled=True)['pulled'] == 1
    session.expire_all()
    assert volunteer.name == 'John Blair' and volunteer.sms_opt_in
    api.phone = '+15559999999'
    volunteer.name = 'Jonathan Blair'; session.commit()
    assert sync_mapped_names(factory, api, CONFIG, clock.now(), write_enabled=True)['held'] == 1
    assert len(api.writes) == 1 and api.name == 'John Blair'
