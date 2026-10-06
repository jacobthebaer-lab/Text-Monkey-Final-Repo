"""PCO acceptance through real app transitions; every API and text is synthetic."""
from copy import deepcopy
from datetime import timedelta
from types import SimpleNamespace

import httpx
import pytest
from sqlalchemy import select
from sqlalchemy.orm import sessionmaker

from app.agents.fill_agent import FillContext
from app.config import Settings
from app.core.inbound import handle_inbound
from app.db.models import Assignment, Event, FillRequest, Role, Shift, Volunteer
from app.integrations.planning_center import (
    PCOBase, PCOClient, PCOConfig, PCOEventLink, PCOStaffingIntent,
    PCOStaffingLink, PlanningCenterError,
)
from app.integrations.planning_center_staffing import (
    CONTEXT, SUPPRESS, _claim, _release, _snapshot, catch_up_assignments,
    enqueue_staffing_intent, map_position, map_volunteer, process_staffing_outbox,
    refresh_staffing, staffing_tick,
)
from tests.test_fill_agent import ScriptedAgentGloo, parser_returning
from tests.test_planning_center import CONFIG, client, sync_schedule


@pytest.fixture(autouse=True)
def staffing_tables(session):
    PCOBase.metadata.create_all(session.get_bind())


class StaffingAPI:
    """Strict supported API shape, independent from the production implementation."""
    def __init__(self, *, starts, ends, rows=(), quantity=1):
        self.starts, self.ends = starts, ends
        self.rows = deepcopy(list(rows))
        self.quantity = quantity
        self.writes = []
        self.reads = []
        self.org = '10'
        self.schedule_to = 'plan'
        self.memberships = True
        self.prepare = False
        self.post_error = False
        self.after_write_read_error = False
        self.before_write = None
        self.next_id = 80
        self.position_name = 'Usher'
        self.team_relationships = {'service_type': {'data': {'type': 'ServiceType', 'id': '20'}}}
        self.membership_mutation = None

    def __enter__(self): return self
    def __exit__(self, *args): pass
    def organization(self): return {'type': 'Organization', 'id': self.org}

    def member(self, *, person='70', status='C', ident='80'):
        return {'type': 'PlanPerson', 'id': ident,
            'attributes': {'status': status, 'team_position_name': self.position_name,
                'updated_at': '2026-10-01T16:00:00Z', 'prepare_notification': self.prepare,
                'notification_prepared_at': None if not self.prepare else '2026-10-01T16:00:00Z',
                'can_accept_partial': False},
            'relationships': {k: {'data': {'type': kind, 'id': value}} for k, kind, value in (
                ('person', 'Person', person), ('team', 'Team', '30'), ('plan', 'Plan', '40'),
                ('service_type', 'ServiceType', '20'))} |
                {'service_times': {'data': [{'type': 'PlanTime', 'id': '60'}]}}}

    def collection(self, path):
        self.reads.append(path)
        if self.after_write_read_error and self.writes:
            raise PlanningCenterError('Synthetic readback outage')
        if path == '/services/v2/service_types/20/plans/40/plan_times':
            return [{'type': 'PlanTime', 'id': '60', 'attributes': {'time_type': 'service',
                'starts_at': self.starts.isoformat(), 'ends_at': self.ends.isoformat()}}]
        if path == '/services/v2/service_types/20/plans/40/team_members':
            return deepcopy(self.rows)
        if path == '/services/v2/service_types/20/plans/40/needed_positions':
            return [{'type': 'NeededPosition', 'id': '50', 'attributes': {
                'quantity': self.quantity, 'team_position_name': self.position_name},
                'relationships': {'team': {'data': {'id': '30'}}}}] if self.quantity else []
        if path == '/services/v2/service_types/20/team_positions/90/person_team_position_assignments':
            rows = [{'type': 'PersonTeamPositionAssignment', 'id': str(100 + index),
                'attributes': {'schedule_preference': 'Every week'},
                'relationships': {'person': {'data': {'type': 'Person', 'id': person}},
                    'team_position': {'data': {'type': 'TeamPosition', 'id': '90'}}}}
                for index, person in enumerate(('70', '71'))] if self.memberships else []
            if self.membership_mutation:
                self.membership_mutation(rows)
            return rows
        raise AssertionError('Unexpected collection: '+path)

    def request(self, method, path, data=None, **kwargs):
        if method == 'GET':
            self.reads.append(path)
            if path.startswith('/services/v2/people/') and path.count('/') == 4:
                return {'data': {'type': 'Person', 'id': path.rsplit('/',1)[-1], 'attributes': {}}}
            if path == '/services/v2/teams/30':
                return {'data': {'type': 'Team', 'id': '30', 'attributes': {'schedule_to': self.schedule_to},
                    'relationships': deepcopy(self.team_relationships)}}
            if path == '/services/v2/service_types/20/team_positions/90':
                return {'data': {'type': 'TeamPosition', 'id': '90', 'attributes': {'name': self.position_name},
                    'relationships': {'team': {'data': {'id': '30'}}}}}
            if path == '/services/v2/service_types/20/plans/40':
                return {'data': {'type': 'Plan', 'id': '40', 'attributes': {},
                    'relationships': {'service_type': {'data': {'id': '20'}}}}}
            raise AssertionError('Unexpected GET: '+path)
        if self.before_write:
            self.before_write(method, path, data)
        self.writes.append((method, path, deepcopy(data)))
        if self.post_error:
            raise PlanningCenterError('Synthetic unknown write outcome')
        attrs = data['data']['attributes']
        assert attrs['prepare_notification'] is False and attrs['notification_prepared_at'] is None
        if method == 'POST':
            assert path == '/services/v2/service_types/20/plans/40/team_members'
            assert attrs['team_position_name'] == self.position_name and attrs['team_id'] == '30'
            person, ident = attrs['person_id'], str(self.next_id); self.next_id += 1
            self.quantity = max(0, self.quantity-1)
        else:
            assert method == 'PATCH' and path.startswith('/services/v2/people/') and '/plan_people/' in path
            ident = data['data']['id']
            previous = next(r for r in self.rows if r['id'] == ident)
            person = previous['relationships']['person']['data']['id']
            if previous['attributes']['status'] != attrs['status']:
                if attrs['status'] == 'D':
                    self.quantity += 1
                elif previous['attributes']['status'] == 'D':
                    self.quantity -= 1
        result = self.member(person=person, ident=ident, status=attrs['status'])
        self.rows = [r for r in self.rows if r['id'] != ident]+[result]
        return {'data': deepcopy(result)}


def setup_assignment(session, clock, make_volunteer, *, status='confirmed', opt_in=True, mapped=True):
    with client(quantity=1) as api:
        sync_schedule(session, api, CONFIG)
    shift = session.scalar(select(Shift))
    # Upstream fixture is fixed; real API double uses exactly the imported time.
    api = StaffingAPI(starts=shift.event.starts_at, ends=shift.event.ends_at)
    volunteer = make_volunteer(opt_in=opt_in)
    if mapped:
        map_volunteer(session, CONFIG, volunteer.id, '70', clock.now(), client=api)
        map_position(session, api, CONFIG, shift_id=shift.id, team_id='30', position_id='90', plan_time_id='60', now=clock.now())
    assignment = Assignment(shift_id=shift.id, volunteer_id=volunteer.id, status=status,
        source='admin', created_at=clock.now(), updated_at=clock.now())
    session.add(assignment); session.flush()
    factory = sessionmaker(bind=session.get_bind(), expire_on_commit=False)
    return volunteer, assignment, api, factory


def enqueue(session, assignment, clock, action='accept', **kwargs):
    intent = enqueue_staffing_intent(session, CONFIG, assignment_id=assignment.id,
        action=action, now=clock.now(), **kwargs)
    ident = intent.id
    session.commit()
    return ident


def state(factory, ident):
    with factory() as session:
        row = session.get(PCOStaffingIntent, ident)
        return row.state, row.reason


def test_acceptance_commits_unknown_before_http_and_is_idempotent(session, clock, make_volunteer):
    volunteer, assignment, api, factory = setup_assignment(session, clock, make_volunteer)
    ident = enqueue(session, assignment, clock)
    def check_durable(*args):
        with factory() as separate:
            assert separate.get(PCOStaffingIntent, ident).state == 'unknown'
    api.before_write = check_durable
    assert process_staffing_outbox(factory, api, CONFIG, clock.now(), enabled=True)['verified'] == 1
    assert state(factory, ident)[0] == 'verified'
    assert len(api.writes) == 1
    assert not process_staffing_outbox(factory, api, CONFIG, clock.now(), enabled=True)['verified']
    assert volunteer.sms_opt_in is True


def test_unknown_post_is_reconciled_after_new_session_and_never_repeated(session, clock, make_volunteer):
    _, assignment, api, factory = setup_assignment(session, clock, make_volunteer)
    ident = enqueue(session, assignment, clock)
    api.post_error = True
    process_staffing_outbox(factory, api, CONFIG, clock.now(), enabled=True)
    assert state(factory, ident)[0] == 'unknown' and len(api.writes) == 1
    api.post_error = False; api.rows = [api.member()]
    clock.advance(timedelta(minutes=2))
    assert process_staffing_outbox(factory, api, CONFIG, clock.now(), enabled=True)['verified'] == 1
    assert len(api.writes) == 1


def test_unknown_absent_stays_unknown_without_second_post(session, clock, make_volunteer):
    _, assignment, api, factory = setup_assignment(session, clock, make_volunteer)
    ident = enqueue(session, assignment, clock)
    api.post_error = True
    process_staffing_outbox(factory, api, CONFIG, clock.now(), enabled=True)
    api.post_error = False; clock.advance(timedelta(minutes=2))
    process_staffing_outbox(factory, api, CONFIG, clock.now(), enabled=True)
    assert state(factory, ident)[0] == 'unknown' and len(api.writes) == 1


@pytest.mark.parametrize('failure', ['org', 'memberships', 'schedule_to', 'position_name', 'quantity'])
def test_fresh_preflight_holds_unsafe_writes(session, clock, make_volunteer, failure):
    _, assignment, api, factory = setup_assignment(session, clock, make_volunteer)
    ident = enqueue(session, assignment, clock)
    setattr(api, failure, {'org':'11','memberships':False,'schedule_to':'time','position_name':'Changed','quantity':0}[failure])
    process_staffing_outbox(factory, api, CONFIG, clock.now(), enabled=True)
    assert state(factory, ident)[0] == 'held' and not api.writes


def test_notification_readback_must_be_false_and_null(session, clock, make_volunteer):
    _, assignment, api, factory = setup_assignment(session, clock, make_volunteer)
    ident = enqueue(session, assignment, clock)
    api.prepare = True
    process_staffing_outbox(factory, api, CONFIG, clock.now(), enabled=True)
    assert state(factory, ident)[0] == 'unknown'
    assert 'notification' in state(factory, ident)[1]
    assert len(api.writes) == 1


def test_stale_local_accept_and_outside_scope_intents_never_write(session, clock, make_volunteer):
    _, assignment, api, factory = setup_assignment(session, clock, make_volunteer)
    ident = enqueue(session, assignment, clock)
    with factory() as changed:
        changed.get(Assignment, assignment.id).status = 'cancelled'
        changed.commit()
    process_staffing_outbox(factory, api, CONFIG, clock.now(), enabled=True)
    assert state(factory, ident)[0] == 'held' and not api.writes
    with factory() as changed:
        intent = changed.get(PCOStaffingIntent, ident); intent.service_type_id = '999'; intent.state = 'pending'
        changed.commit()
    process_staffing_outbox(factory, api, CONFIG, clock.now(), enabled=True)
    assert not api.writes and state(factory, ident)[0] == 'held'


def test_cancel_uses_documented_endpoint_and_rechecks_expected_state(session, clock, make_volunteer):
    _, assignment, api, factory = setup_assignment(session, clock, make_volunteer)
    accept = enqueue(session, assignment, clock)
    process_staffing_outbox(factory, api, CONFIG, clock.now(), enabled=True)
    session.expire_all(); assignment = session.get(Assignment, assignment.id)
    assignment.status = 'cancelled'; assignment.updated_at = clock.now()
    cancel = enqueue(session, assignment, clock, 'cancel')
    process_staffing_outbox(factory, api, CONFIG, clock.now(), enabled=True)
    assert state(factory, cancel)[0] == 'verified'
    assert api.writes[-1][1] == '/services/v2/people/70/plan_people/80'
    assert api.rows[0]['attributes']['status'] == 'D'


def test_external_admin_change_is_not_overwritten(session, clock, make_volunteer):
    _, assignment, api, factory = setup_assignment(session, clock, make_volunteer)
    enqueue(session, assignment, clock)
    process_staffing_outbox(factory, api, CONFIG, clock.now(), enabled=True)
    session.expire_all(); assignment = session.get(Assignment, assignment.id)
    assignment.status = 'cancelled'; assignment.updated_at = clock.now()
    cancel = enqueue(session, assignment, clock, 'cancel')
    api.rows[0]['attributes']['updated_at'] = '2026-10-02T00:00:00Z'
    process_staffing_outbox(factory, api, CONFIG, clock.now(), enabled=True)
    assert state(factory, cancel)[0] == 'held' and len(api.writes) == 1


def test_inbound_decline_reaccept_removal_preserves_history_and_no_echo(session, clock, make_volunteer):
    _, assignment, api, factory = setup_assignment(session, clock, make_volunteer)
    enqueue(session, assignment, clock)
    process_staffing_outbox(factory, api, CONFIG, clock.now(), enabled=True)
    with factory(info={CONTEXT:(Settings(pco_staffing_write_enabled=True), CONFIG)}) as poll:
        api.rows = [api.member(status='D')]
        refresh_staffing(poll, api, CONFIG, clock.now(), service_type_id='20', plan_id='40')
        poll.commit()
        assert poll.get(Assignment, assignment.id).status == 'cancelled'
        api.rows = [api.member(status='C')]
        refresh_staffing(poll, api, CONFIG, clock.now(), service_type_id='20', plan_id='40')
        poll.commit()
        assert poll.get(Assignment, assignment.id).status == 'confirmed'
        api.rows = []
        refresh_staffing(poll, api, CONFIG, clock.now(), service_type_id='20', plan_id='40')
        poll.commit()
        assert poll.get(Assignment, assignment.id).status == 'cancelled'
        assert len(list(poll.scalars(select(PCOStaffingIntent)))) == 1


def test_serialized_claims_and_expired_unknown_never_create_twice(session, clock, make_volunteer):
    _, assignment, api, factory = setup_assignment(session, clock, make_volunteer)
    ident = enqueue(session, assignment, clock)
    first = _claim(factory, '10:20:40', clock.now())
    assert first and _claim(factory, '10:20:40', clock.now()) is None
    process_staffing_outbox(factory, api, CONFIG, clock.now(), enabled=True)
    assert not api.writes
    _release(factory, '10:20:40', first)
    process_staffing_outbox(factory, api, CONFIG, clock.now(), enabled=True)
    assert len(api.writes) == 1 and state(factory, ident)[0] == 'verified'


def test_authoritative_inbound_confirmation_enqueues_same_transaction_and_rolls_back(session, clock, provider, make_volunteer):
    volunteer, assignment, api, factory = setup_assignment(session, clock, make_volunteer, status='approved')
    session.commit()
    session.info[CONTEXT] = (Settings(pco_staffing_write_enabled=True), CONFIG)
    ctx = FillContext(session, clock, provider, ScriptedAgentGloo())
    result = handle_inbound(session, clock, provider, volunteer.phone, 'Yes',
        parser_returning(intent='confirm'), ctx=ctx)
    session.flush()
    assert session.get(Assignment, assignment.id).status == 'confirmed'
    assert session.scalar(select(PCOStaffingIntent)).action == 'accept'
    session.rollback()
    assert session.get(Assignment, assignment.id).status == 'approved'
    assert session.scalar(select(PCOStaffingIntent)) is None
    handle_inbound(session, clock, provider, volunteer.phone, 'Yes', parser_returning(intent='confirm'), ctx=ctx)
    session.commit()
    process_staffing_outbox(factory, api, CONFIG, clock.now(), enabled=True)
    assert api.rows[0]['attributes']['status'] == 'C' and len(api.writes) == 1


def test_cancellation_application_hook_and_replacement_dependency(session, clock, provider, make_volunteer):
    volunteer, assignment, api, factory = setup_assignment(session, clock, make_volunteer)
    enqueue(session, assignment, clock)
    process_staffing_outbox(factory, api, CONFIG, clock.now(), enabled=True)
    session.expire_all()
    session.info[CONTEXT] = (Settings(pco_staffing_write_enabled=True), CONFIG)
    ctx = FillContext(session, clock, provider, ScriptedAgentGloo())
    handle_inbound(session, clock, provider, volunteer.phone, "I can't serve", parser_returning(intent='cancel'), ctx=ctx)
    session.commit()
    cancelled = session.scalar(select(PCOStaffingIntent).where(PCOStaffingIntent.action == 'cancel'))
    assert cancelled and session.get(Assignment, assignment.id).status == 'cancelled'
    process_staffing_outbox(factory, api, CONFIG, clock.now(), enabled=True)
    assert api.rows[0]['attributes']['status'] == 'D'


def test_default_off_and_absent_context_do_not_enqueue_or_open_client(session, clock, make_volunteer):
    _, assignment, api, factory = setup_assignment(session, clock, make_volunteer, status='approved')
    assignment.status = 'confirmed'; session.commit()
    assert session.scalar(select(PCOStaffingIntent)) is None
    session.info[CONTEXT] = (Settings(pco_staffing_write_enabled=False), CONFIG)
    assignment.status = 'cancelled'; session.commit()
    assert session.scalar(select(PCOStaffingIntent)) is None
    assert staffing_tick(factory, Settings(), CONFIG, clock.now(),
        client_factory=lambda cfg: pytest.fail('Default off must not create API client')) == {'disabled':True}


def test_explicit_bounded_catch_up_and_poll_interval(session, clock, make_volunteer):
    _, assignment, api, factory = setup_assignment(session, clock, make_volunteer)
    catch_up_assignments(session, CONFIG, [assignment.id], clock.now()); session.commit()
    settings = Settings(pco_staffing_write_enabled=True, pco_staffing_poll_enabled=True)
    staffing_tick(factory, settings, CONFIG, clock.now(), client_factory=lambda cfg: api)
    count = len(api.reads)
    staffing_tick(factory, Settings(pco_staffing_poll_enabled=True), CONFIG, clock.now(), client_factory=lambda cfg: api)
    assert len(api.reads) == count
    clock.advance(timedelta(minutes=1))
    staffing_tick(factory, Settings(pco_staffing_poll_enabled=True), CONFIG, clock.now(), client_factory=lambda cfg: api)
    assert len(api.reads) > count and len(api.writes) == 1


def test_actual_fill_acceptance_enqueues_dependent_replacement_and_reports_verified_coverage(
    session, clock, provider, make_volunteer
):
    from app.core import offer_windows as offers
    from app.core.notifications import staffing_snapshot
    from app.db.models import Message, Outreach
    original, assignment, api, factory = setup_assignment(session, clock, make_volunteer)
    enqueue(session, assignment, clock)
    process_staffing_outbox(factory, api, CONFIG, clock.now(), enabled=True)
    session.expire_all()
    replacement = make_volunteer()
    map_volunteer(session, CONFIG, replacement.id, '71', clock.now(), client=api)
    session.info[CONTEXT] = (Settings(pco_staffing_write_enabled=True), CONFIG)
    ctx = FillContext(session, clock, provider, ScriptedAgentGloo())
    handle_inbound(session, clock, provider, original.phone, "I can't serve", parser_returning(intent='cancel'), ctx=ctx)
    fill = session.scalar(select(FillRequest).where(FillRequest.cancelled_assignment_id == assignment.id))
    fill.state = 'in_progress'
    message = Message(direction='out', volunteer_id=replacement.id, phone=replacement.phone,
        body='Synthetic reviewed invitation.', purpose='outreach', kind='ai', status='sent', created_at=clock.now())
    session.add(message); session.flush()
    outreach = session.scalar(select(Outreach).where(Outreach.fill_request_id == fill.id,
        Outreach.volunteer_id == replacement.id))
    if outreach is None:
        outreach = Outreach(fill_request_id=fill.id, volunteer_id=replacement.id, tranche=1,
                            response='none', message_id=message.id)
        session.add(outreach); session.flush()
    else:
        outreach.response = 'none'; outreach.message_id = message.id
    offers.prepare(session, outreach, message.body, clock.now())
    offers.dispatch(session, outreach, message, clock.now())
    result = handle_inbound(session, clock, provider, replacement.phone, 'Yes', parser_returning(intent='accept'), ctx=ctx)
    assert result.notes == ['filled']
    session.commit()
    intents = list(session.scalars(select(PCOStaffingIntent).order_by(PCOStaffingIntent.id)))
    assert [i.action for i in intents] == ['accept','cancel','accept']
    assert intents[-1].depends_on == intents[-2].id
    assert not staffing_snapshot(session, assignment.shift.event)['fully_staffed']
    assert staffing_snapshot(session, assignment.shift.event)['covered'] == 0
    process_staffing_outbox(factory, api, CONFIG, clock.now(), enabled=True)
    session.expire_all()
    assert [r['attributes']['status'] for r in api.rows] == ['D','C']
    assert staffing_snapshot(session, session.get(Event, assignment.shift.event_id))['fully_staffed']
    assert len(api.writes) == 3


def test_unconfirmed_external_assignment_never_counts_as_covered(session, clock, make_volunteer):
    from app.core.notifications import staffing_snapshot
    volunteer, assignment, api, factory = setup_assignment(session, clock, make_volunteer, status='cancelled')
    api.rows = [api.member(status='U')]; api.quantity = 0
    refresh_staffing(session, api, CONFIG, clock.now(), service_type_id='20', plan_id='40')
    session.commit()
    snapshot = staffing_snapshot(session, assignment.shift.event)
    assert snapshot['required'] == 1 and snapshot['covered'] == 0 and not snapshot['fully_staffed']
    assert volunteer.sms_opt_in is True


def test_open_need_import_preserves_occupied_slot_in_addition_to_unfilled_need(session, clock, make_volunteer):
    _, assignment, api, factory = setup_assignment(session, clock, make_volunteer)
    with client(quantity=1) as remote:
        sync_schedule(session, remote, CONFIG)
    assert len(list(session.scalars(select(Shift)))) == 2
    assert session.get(Assignment, assignment.id).status == 'confirmed'
    with client(quantity=1) as remote:
        sync_schedule(session, remote, CONFIG)
    assert len(list(session.scalars(select(Shift)))) == 2


def test_application_consumes_flags_and_preserves_general_scheduler_pause(tmp_path, monkeypatch):
    from fastapi.testclient import TestClient
    from app.main import create_app
    import app.jobs
    import apscheduler.schedulers.background
    calls, registered = [], []
    class SyntheticScheduler:
        def add_job(self, fn, *args, **kwargs):
            registered.append((kwargs['id'], fn))
        def start(self): pass
        def shutdown(self, **kwargs): pass
    monkeypatch.setattr(apscheduler.schedulers.background, 'BackgroundScheduler', SyntheticScheduler)
    monkeypatch.setattr(PCOConfig, 'from_env', classmethod(lambda cls: CONFIG))
    monkeypatch.setattr(app.jobs, 'process_pco_staffing', lambda *args: calls.append(args))
    settings = Settings(database_url='sqlite:///'+str(tmp_path/'synthetic.db'), demo_mode=False,
        automation_enabled=False, pco_staffing_write_enabled=True, pco_staffing_poll_enabled=True)
    app = create_app(settings)
    with TestClient(app):
        assert [x[0] for x in registered] == ['pco_staffing_tick']
        registered[0][1]()
    assert len(calls) == 1 and calls[0][1] is settings
    with app.state.session_factory() as active:
        assert active.info[CONTEXT] == (settings, CONFIG)
    assert settings.automation_enabled is False


def test_file_database_restart_and_other_process_observe_unknown_before_http(
    session, clock, make_volunteer, tmp_path
):
    import sqlite3
    import subprocess
    import sys
    from app.db.session import make_engine, make_session_factory
    _, assignment, api, unused = setup_assignment(session, clock, make_volunteer)
    ident = enqueue(session, assignment, clock)
    database = tmp_path/'durable-staffing.db'
    source = session.get_bind().raw_connection()
    with sqlite3.connect(database) as destination:
        source.driver_connection.backup(destination)
    source.close()
    engine = make_engine('sqlite:///'+str(database)); factory = make_session_factory(engine)
    def inspect_in_another_process(*args):
        script = "import sqlite3,sys; c=sqlite3.connect('file:'+sys.argv[1]+'?mode=ro',uri=True); print(c.execute('select state from pco_staffing_intents where id=?',(int(sys.argv[2]),)).fetchone()[0])"
        result = subprocess.run([sys.executable, '-c', script, str(database), str(ident)],
                                capture_output=True, text=True, check=True)
        assert result.stdout.strip() == 'unknown'
    api.before_write = inspect_in_another_process
    api.post_error = True
    process_staffing_outbox(factory, api, CONFIG, clock.now(), enabled=True)
    assert state(factory, ident)[0] == 'unknown'
    engine.dispose()
    restarted = make_engine('sqlite:///'+str(database)); restarted_factory = make_session_factory(restarted)
    second = StaffingAPI(starts=api.starts, ends=api.ends, rows=[api.member()], quantity=0)
    clock.advance(timedelta(minutes=2))
    assert process_staffing_outbox(restarted_factory, second, CONFIG, clock.now(), enabled=True)['verified'] == 1
    assert not second.writes
    restarted.dispose()


def test_concurrent_database_claims_have_one_writer(session, clock, make_volunteer, tmp_path):
    import sqlite3
    from concurrent.futures import ThreadPoolExecutor
    from threading import Barrier
    from app.db.session import make_engine, make_session_factory
    _, assignment, api, unused = setup_assignment(session, clock, make_volunteer)
    enqueue(session, assignment, clock)
    database = tmp_path/'serialized-staffing.db'
    source = session.get_bind().raw_connection()
    with sqlite3.connect(database) as destination:
        source.driver_connection.backup(destination)
    source.close()
    engine = make_engine('sqlite:///'+str(database)); factory = make_session_factory(engine)
    barrier = Barrier(2)
    def claim():
        barrier.wait()
        return _claim(factory, '10:20:40', clock.now())
    with ThreadPoolExecutor(max_workers=2) as workers:
        results = list(workers.map(lambda _: claim(), range(2)))
    assert len([owner for owner in results if owner]) == 1
    assert process_staffing_outbox(factory, api, CONFIG, clock.now(), enabled=True)['verified'] == 0
    assert not api.writes
    _release(factory, '10:20:40', next(owner for owner in results if owner))
    process_staffing_outbox(factory, api, CONFIG, clock.now(), enabled=True)
    assert len(api.writes) == 1
    engine.dispose()


def test_readback_failure_keeps_returned_id_and_reconciles_without_repeat(session, clock, make_volunteer):
    _, assignment, api, factory = setup_assignment(session, clock, make_volunteer)
    ident = enqueue(session, assignment, clock)
    api.after_write_read_error = True
    process_staffing_outbox(factory, api, CONFIG, clock.now(), enabled=True)
    with factory() as saved:
        assert saved.get(PCOStaffingIntent, ident).plan_person_id == '80'
    assert state(factory, ident)[0] == 'unknown'
    api.after_write_read_error = False; clock.advance(timedelta(minutes=2))
    process_staffing_outbox(factory, api, CONFIG, clock.now(), enabled=True)
    assert state(factory, ident)[0] == 'verified' and len(api.writes) == 1


def test_missing_mapping_holds_authoritative_transition_without_inventing_identity(session, clock, make_volunteer):
    _, assignment, api, factory = setup_assignment(session, clock, make_volunteer, status='approved', mapped=False)
    session.info[CONTEXT] = (Settings(pco_staffing_write_enabled=True), CONFIG)
    assignment.status = 'confirmed'; session.commit()
    intent = session.scalar(select(PCOStaffingIntent))
    assert intent.state == 'held' and 'mapping required' in intent.reason
    assert intent.person_id == '' and intent.team_id == ''
    process_staffing_outbox(factory, api, CONFIG, clock.now(), enabled=True)
    assert not api.writes


def test_rate_limit_retry_after_is_sanitized_and_honored():
    def respond(request):
        return httpx.Response(429, headers={'Retry-After':'180'}, json={'private':'Do not log'})
    with PCOClient(CONFIG, transport=httpx.MockTransport(respond)) as api:
        with pytest.raises(PlanningCenterError) as failure:
            api.request('GET','/services/v2')
    assert failure.value.retry_after == 180
    assert 'private' not in str(failure.value)


def test_actual_confirmation_of_existing_unconfirmed_reservation_patches_without_create(
    session, clock, provider, make_volunteer
):
    volunteer, assignment, api, factory = setup_assignment(session, clock, make_volunteer, status='cancelled')
    api.rows = [api.member(status='U')]; api.quantity = 0
    refresh_staffing(session, api, CONFIG, clock.now(), service_type_id='20', plan_id='40')
    session.commit()
    reserved = session.scalar(select(Assignment).where(Assignment.status == 'approved'))
    assert reserved is not None
    session.info[CONTEXT] = (Settings(pco_staffing_write_enabled=True), CONFIG)
    ctx = FillContext(session, clock, provider, ScriptedAgentGloo())
    handle_inbound(session, clock, provider, volunteer.phone, 'Yes', parser_returning(intent='confirm'), ctx=ctx)
    session.commit()
    assert process_staffing_outbox(factory, api, CONFIG, clock.now(), enabled=True)['verified'] == 1
    assert [write[0] for write in api.writes] == ['PATCH']
    assert api.rows[0]['attributes']['status'] == 'C'


def test_known_remote_identity_change_is_held_instead_of_false_removal(session, clock, make_volunteer):
    _, assignment, api, factory = setup_assignment(session, clock, make_volunteer)
    enqueue(session, assignment, clock)
    process_staffing_outbox(factory, api, CONFIG, clock.now(), enabled=True)
    session.expire_all(); assignment = session.get(Assignment, assignment.id)
    assignment.status = 'cancelled'; assignment.updated_at = clock.now()
    ident = enqueue(session, assignment, clock, 'cancel')
    api.rows[0]['attributes']['team_position_name'] = 'Another role'
    process_staffing_outbox(factory, api, CONFIG, clock.now(), enabled=True)
    assert state(factory, ident)[0] == 'held' and 'identity/position changed' in state(factory, ident)[1]
    assert len(api.writes) == 1


def test_preflight_rate_limit_defers_and_prevents_more_requests(session, clock, make_volunteer):
    _, assignment, api, factory = setup_assignment(session, clock, make_volunteer)
    ident = enqueue(session, assignment, clock)
    original = api.organization
    calls = []
    def rate_limited():
        calls.append(True)
        error = PlanningCenterError('Planning Center returned HTTP 429'); error.retry_after = 180
        raise error
    api.organization = rate_limited
    result = process_staffing_outbox(factory, api, CONFIG, clock.now(), enabled=True)
    assert result['rate_limited'] and state(factory, ident)[0] == 'pending' and not api.writes
    assert len(calls) == 1
    process_staffing_outbox(factory, api, CONFIG, clock.now()+timedelta(seconds=179), enabled=True)
    assert len(calls) == 1
    api.organization = original
    process_staffing_outbox(factory, api, CONFIG, clock.now()+timedelta(seconds=180), enabled=True)
    assert state(factory, ident)[0] == 'verified'


def test_explicit_catchup_can_repair_held_missing_mapping_without_broad_backfill(session, clock, make_volunteer):
    volunteer, assignment, api, factory = setup_assignment(session, clock, make_volunteer, mapped=False)
    ident = enqueue(session, assignment, clock)
    assert state(factory, ident)[0] == 'held'
    map_volunteer(session, CONFIG, volunteer.id, '70', clock.now(), client=api)
    map_position(session, api, CONFIG, shift_id=assignment.shift_id, team_id='30', position_id='90', plan_time_id='60', now=clock.now())
    retried = catch_up_assignments(session, CONFIG, [assignment.id], clock.now())
    session.commit()
    assert retried[0].id != ident
    process_staffing_outbox(factory, api, CONFIG, clock.now(), enabled=True)
    assert state(factory, retried[0].id)[0] == 'verified' and len(api.writes) == 1
    assert len(list(session.scalars(select(PCOStaffingIntent)))) == 2


def test_poll_holds_existing_unlinked_commitment_instead_of_duplicating(session, clock, make_volunteer):
    _, assignment, api, factory = setup_assignment(session, clock, make_volunteer)
    api.rows = [api.member()]; api.quantity = 0
    report = refresh_staffing(session, api, CONFIG, clock.now(), service_type_id='20', plan_id='40')
    session.commit()
    assert report['conflicts'] == 1 and report['created'] == 0
    assert list(session.scalars(select(Assignment))) == [assignment]
    assert session.get(PCOStaffingLink, assignment.id) is None
    intent = catch_up_assignments(session, CONFIG, [assignment.id], clock.now())[0]
    ident = intent.id; session.commit()
    assert process_staffing_outbox(factory, api, CONFIG, clock.now(), enabled=True)['verified'] == 1
    assert state(factory, ident)[0] == 'verified' and not api.writes
    assert session.get(PCOStaffingLink, assignment.id) is not None


def test_initial_decline_retains_history_and_reaccepts_without_new_assignment(session, clock, make_volunteer):
    _, old, api, _ = setup_assignment(session, clock, make_volunteer, status='cancelled')
    api.rows = [api.member(status='D')]
    report = refresh_staffing(session, api, CONFIG, clock.now(), service_type_id='20', plan_id='40')
    session.commit()
    assert report['declined'] == 1
    link = session.scalar(select(PCOStaffingLink))
    assignment = session.get(Assignment, link.assignment_id)
    assert assignment.status == 'cancelled' and link.remote_status == 'D'
    api.rows = [api.member(status='C')]; api.quantity = 0
    refresh_staffing(session, api, CONFIG, clock.now(), service_type_id='20', plan_id='40')
    session.commit()
    assert assignment.status == 'confirmed' and link.remote_status == 'C'
    assert len(list(session.scalars(select(Assignment)))) == 2
    assert old.status == 'cancelled'


@pytest.mark.parametrize('change', ['cancel', 'revoke_consent', 'close_event'])
def test_local_change_during_remote_reads_is_held_before_http(
    session, clock, make_volunteer, tmp_path, change
):
    import sqlite3
    from app.db.session import make_engine, make_session_factory
    volunteer, assignment, api, unused = setup_assignment(session, clock, make_volunteer)
    ident = enqueue(session, assignment, clock)
    database = tmp_path/'preflight-race.db'
    source = session.get_bind().raw_connection()
    with sqlite3.connect(database) as destination:
        source.driver_connection.backup(destination)
    source.close()
    engine = make_engine('sqlite:///'+str(database)); factory = make_session_factory(engine)
    original = api.collection
    def concurrent_change(path):
        rows = original(path)
        if path.endswith('/person_team_position_assignments'):
            with factory() as other:
                if change == 'cancel':
                    current = other.get(Assignment, assignment.id)
                    current.status = 'cancelled'; current.updated_at = clock.now()+timedelta(seconds=1)
                elif change == 'revoke_consent':
                    other.get(Volunteer, volunteer.id).sms_opt_in = False
                else:
                    other.get(Event, assignment.shift.event_id).status = 'cancelled'
                other.commit()
        return rows
    api.collection = concurrent_change
    report = process_staffing_outbox(factory, api, CONFIG, clock.now(), enabled=True)
    assert report['held'] == 1 and report['verified'] == 0 and not api.writes
    assert state(factory, ident)[0] == 'held' and 'preflight' in state(factory, ident)[1]
    engine.dispose()


def test_preflight_longer_than_lease_never_claims_unknown_or_writes(session, clock, make_volunteer, monkeypatch):
    import app.integrations.planning_center_staffing as bridge
    _, assignment, api, factory = setup_assignment(session, clock, make_volunteer)
    ident = enqueue(session, assignment, clock)
    moments = iter((0, 601))
    monkeypatch.setattr(bridge, 'monotonic', lambda: next(moments))
    report = process_staffing_outbox(factory, api, CONFIG, clock.now(), enabled=True)
    assert report['skipped'] == 1 and state(factory, ident)[0] == 'pending' and not api.writes


@pytest.mark.parametrize('relationships', [
    {'service_types': {'data': [{'type': 'ServiceType', 'id': '20'}]}},
    {'service_type': {'data': None}, 'service_types': {'data': [
        {'type': 'ServiceType', 'id': '21'}, {'type': 'ServiceType', 'id': '20'}]}},
])
def test_plural_team_scope_maps_and_reconciles_exact_staffing(session, clock, make_volunteer, relationships):
    _, assignment, api, factory = setup_assignment(session, clock, make_volunteer)
    api.team_relationships = relationships
    map_position(session, api, CONFIG, shift_id=assignment.shift_id, team_id='30',
                 position_id='90', plan_time_id='60', now=clock.now())
    ident = enqueue(session, assignment, clock)
    assert process_staffing_outbox(factory, api, CONFIG, clock.now(), enabled=True)['verified'] == 1
    assert state(factory, ident)[0] == 'verified'
    refresh_staffing(session, api, CONFIG, clock.now(), service_type_id='20', plan_id='40')
    assert assignment.status == 'confirmed'
    assert len(api.writes) == 1


@pytest.mark.parametrize('relationships', [
    {'service_type': {'data': {'id': '20'}}},
    {'service_type': {'data': {'type': 'Team', 'id': '20'}}},
    {'service_types': {'data': [{'type': 'ServiceType', 'id': '21'}]}},
    {'service_types': {'data': [{'type': 'ServiceType', 'id': '20'},
                              {'type': 'ServiceType', 'id': '20'}]}},
    {'service_type': {'data': {'type': 'ServiceType', 'id': '21'}},
     'service_types': {'data': [{'type': 'ServiceType', 'id': '20'}]}},
])
def test_staffing_holds_untrusted_team_scope_before_write(session, clock, make_volunteer, relationships):
    _, assignment, api, factory = setup_assignment(session, clock, make_volunteer)
    ident = enqueue(session, assignment, clock)
    api.team_relationships = relationships
    assert process_staffing_outbox(factory, api, CONFIG, clock.now(), enabled=True)['held'] == 1
    assert state(factory, ident)[0] == 'held'
    assert not api.writes


@pytest.mark.parametrize('mutation', [
    lambda rows: rows[0].update(type='Person'),
    lambda rows: rows[0].pop('id'),
    lambda rows: rows[0].update(id='not-an-id'),
    lambda rows: rows[0]['relationships']['person']['data'].update(type='Team'),
    lambda rows: rows[0]['relationships']['team_position']['data'].update(type='Person'),
    lambda rows: rows[0]['attributes'].update(schedule_preference=None),
    lambda rows: rows[1].update(id=rows[0]['id']),
])
def test_malformed_position_membership_never_authorizes_acceptance(session, clock, make_volunteer, mutation):
    _, assignment, api, factory = setup_assignment(session, clock, make_volunteer)
    ident = enqueue(session, assignment, clock)
    api.membership_mutation = mutation
    assert process_staffing_outbox(factory, api, CONFIG, clock.now(), enabled=True)['held'] == 1
    assert state(factory, ident)[0] == 'held'
    assert not api.writes
