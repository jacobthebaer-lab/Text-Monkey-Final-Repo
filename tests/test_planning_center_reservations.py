"""Truthful approved/U reservations on disposable stores; no external calls."""
from datetime import timedelta

import pytest
from sqlalchemy import select

from app.agents.fill_agent import FillContext
from app.config import Settings
from app.core.eligibility import check
from app.core.inbound import handle_inbound
from app.db.models import Assignment, FillRequest, Message, RoleRecipe, Shift
from app.integrations.planning_center import PCOBase, PCOStaffingIntent, PCOStaffingLink, PlanningCenterError
from app.integrations.planning_center_staffing import (
    CONTEXT, catch_up_approved_assignments, enqueue_staffing_intent,
    process_staffing_outbox, refresh_staffing, verified_coverage_counts,
)
from tests.test_planning_center import CONFIG
from tests.test_planning_center_staffing import setup_assignment, state
from tests.test_fill_agent import ScriptedAgentGloo, parser_returning


@pytest.fixture(autouse=True)
def tables(session):
    PCOBase.metadata.create_all(session.get_bind())


def reserve(session, assignment, clock):
    intent = catch_up_approved_assignments(session, CONFIG, [assignment.id], clock.now())[0]
    session.commit()
    return intent.id


def test_approved_reservation_is_native_u_never_confirmation(session, clock, make_volunteer):
    person, assignment, api, factory = setup_assignment(session, clock, make_volunteer, status='approved')
    ident = reserve(session, assignment, clock)
    assert process_staffing_outbox(factory, api, CONFIG, clock.now(), enabled=True)['verified'] == 1
    session.expire_all()
    assert assignment.status == 'approved'
    link = session.get(PCOStaffingLink, assignment.id)
    assert link.remote_status == api.rows[0]['attributes']['status'] == 'U'
    assert verified_coverage_counts(session, [assignment.shift.event], {assignment.shift_id: 0})[assignment.shift_id] == 0
    attrs = api.writes[0][2]['data']['attributes']
    assert attrs['status'] == 'U' and attrs['prepare_notification'] is False and attrs['notification_prepared_at'] is None
    assert state(factory, ident)[0] == 'verified'
    process_staffing_outbox(factory, api, CONFIG, clock.now(), enabled=True)
    assert len(api.writes) == 1 and not list(session.scalars(select(Message)))


def test_real_confirmation_and_cancellation_update_same_reserved_native_person(session, clock, provider, make_volunteer):
    person, assignment, api, factory = setup_assignment(session, clock, make_volunteer, status='approved')
    reserve(session, assignment, clock)
    process_staffing_outbox(factory, api, CONFIG, clock.now(), enabled=True)
    native_id = api.rows[0]['id']
    session.expire_all()
    session.info[CONTEXT] = (Settings(pco_staffing_write_enabled=True), CONFIG)
    ctx = FillContext(session, clock, provider, ScriptedAgentGloo())
    handle_inbound(session, clock, provider, person.phone, 'Yes', parser_returning(intent='confirm'), ctx=ctx)
    session.commit()
    process_staffing_outbox(factory, api, CONFIG, clock.now(), enabled=True)
    session.expire_all()
    assert assignment.status == 'confirmed' and api.rows[0]['id'] == native_id
    assert api.rows[0]['attributes']['status'] == 'C'
    assert verified_coverage_counts(session, [assignment.shift.event], {assignment.shift_id: 0})[assignment.shift_id] == 1
    handle_inbound(session, clock, provider, person.phone, "I can't serve", parser_returning(intent='cancel'), ctx=ctx)
    session.commit()
    process_staffing_outbox(factory, api, CONFIG, clock.now(), enabled=True)
    assert api.rows[0]['id'] == native_id and api.rows[0]['attributes']['status'] == 'D'
    assert [write[0] for write in api.writes] == ['POST', 'PATCH', 'PATCH']
    assert len(api.rows) == 1


def test_native_u_decline_records_refusal_and_one_fill_without_sms(session, clock, make_volunteer):
    person, assignment, api, factory = setup_assignment(session, clock, make_volunteer, status='approved')
    reserve(session, assignment, clock)
    process_staffing_outbox(factory, api, CONFIG, clock.now(), enabled=True)
    session.expire_all()
    api.rows = [api.member(status='D')]
    refresh_staffing(session, api, CONFIG, clock.now(), service_type_id='20', plan_id='40')
    refresh_staffing(session, api, CONFIG, clock.now(), service_type_id='20', plan_id='40')
    assert assignment.status == 'cancelled' and not check(session, person, assignment.shift)
    assert len(list(session.scalars(select(FillRequest)))) == 1
    assert not list(session.scalars(select(Message)))


@pytest.mark.parametrize('new_booking', [False, True])
def test_approval_and_reservation_intent_are_atomic(session, clock, make_volunteer, new_booking):
    person, assignment, api, factory = setup_assignment(session, clock, make_volunteer, status='proposed')
    session.commit()
    session.info[CONTEXT] = (Settings(pco_staffing_write_enabled=True), CONFIG)
    if new_booking:
        person2 = make_volunteer()
        shift = Shift(event_id=assignment.shift.event_id, role_id=assignment.shift.role_id, slot_index=1)
        session.add(shift); session.flush()
        booking = Assignment(shift_id=shift.id, volunteer_id=person2.id, status='approved', source='admin',
            created_at=clock.now(), updated_at=clock.now())
        session.add(booking)
    else:
        booking = assignment
        booking.status = 'approved'
    session.flush()
    intent = session.scalar(select(PCOStaffingIntent))
    assert intent.assignment_id == booking.id and intent.action == 'reserve'
    assert intent.expected['local']['status'] == 'approved'
    session.rollback()
    assert not list(session.scalars(select(PCOStaffingIntent)))
    assert session.get(Assignment, assignment.id).status == 'proposed'
    assert not api.writes


def test_named_catchup_prevalidates_all_and_does_not_promote_proposed(session, clock, make_volunteer):
    person, assignment, api, factory = setup_assignment(session, clock, make_volunteer, status='approved')
    for ids in ([], list(range(1, 27)), [assignment.id, 999], [True]):
        with pytest.raises(PlanningCenterError):
            catch_up_approved_assignments(session, CONFIG, ids, clock.now())
        assert not list(session.scalars(select(PCOStaffingIntent)))
    assignment.status = 'proposed'
    with pytest.raises(PlanningCenterError):
        enqueue_staffing_intent(session, CONFIG, assignment_id=assignment.id, action='reserve', now=clock.now())
    assert not api.writes


def test_unknown_reservation_does_not_replay_an_ambiguous_create(session, clock, make_volunteer):
    person, assignment, api, factory = setup_assignment(session, clock, make_volunteer, status='approved')
    ident = reserve(session, assignment, clock)
    api.post_error = True
    process_staffing_outbox(factory, api, CONFIG, clock.now(), enabled=True)
    assert state(factory, ident)[0] == 'unknown'
    api.post_error = False
    process_staffing_outbox(factory, api, CONFIG, clock.now()+timedelta(minutes=2), enabled=True)
    assert state(factory, ident)[0] == 'unknown' and len(api.writes) == 1
    session.expire_all()
    assert assignment.status == 'approved'


def test_unknown_reservation_after_mutation_recovers_read_only(session, clock, make_volunteer):
    person, assignment, api, factory = setup_assignment(session, clock, make_volunteer, status='approved')
    ident = reserve(session, assignment, clock)
    original = api.request
    def request(method, path, **kwargs):
        result = original(method, path, **kwargs)
        if method == 'POST':
            raise PlanningCenterError('Synthetic response lost after receiving U')
        return result
    api.request = request
    process_staffing_outbox(factory, api, CONFIG, clock.now(), enabled=True)
    assert state(factory, ident)[0] == 'unknown'
    api.request = original
    assert process_staffing_outbox(factory, api, CONFIG, clock.now()+timedelta(minutes=2), enabled=True)['verified'] == 1
    assert state(factory, ident)[0] == 'verified' and len(api.writes) == 1
    session.expire_all()
    assert assignment.status == 'approved' and session.get(PCOStaffingLink, assignment.id).remote_status == 'U'


def five_slots(session, assignment):
    for index in range(1, 5):
        session.add(Shift(event_id=assignment.shift.event_id, role_id=assignment.shift.role_id, slot_index=index))
    session.flush()


def test_u_consumes_one_open_need_without_inflating_five_slot_target(session, clock, make_volunteer):
    person, assignment, api, factory = setup_assignment(session, clock, make_volunteer, status='approved')
    five_slots(session, assignment)
    api.quantity = 5
    reserve(session, assignment, clock)
    assert process_staffing_outbox(factory, api, CONFIG, clock.now(), enabled=True)['verified'] == 1
    session.expire_all()
    assert api.quantity == 4 and api.rows[0]['attributes']['status'] == 'U'
    assert len(list(session.scalars(select(Shift)))) == 5
    assert session.scalar(select(RoleRecipe)).count == 5


def test_local_five_native_two_capacity_is_held_without_resizing_or_native_write(session, clock, make_volunteer):
    person, assignment, api, factory = setup_assignment(session, clock, make_volunteer, status='approved')
    five_slots(session, assignment)
    api.quantity = 2
    ident = reserve(session, assignment, clock)
    assert process_staffing_outbox(factory, api, CONFIG, clock.now(), enabled=True)['held'] == 1
    assert state(factory, ident)[0] == 'held' and not api.writes
    assert len(list(session.scalars(select(Shift)))) == 5
    assert session.scalar(select(RoleRecipe)) is None


def test_unexpected_native_open_needs_readback_keeps_unknown_barrier_and_target(session, clock, make_volunteer):
    person, assignment, api, factory = setup_assignment(session, clock, make_volunteer, status='approved')
    five_slots(session, assignment)
    api.quantity = 5
    ident = reserve(session, assignment, clock)
    original = api.request
    def request(method, path, **kwargs):
        result = original(method, path, **kwargs)
        if method == 'POST': api.quantity += 1  # Simulate a native API that does not consume the open need.
        return result
    api.request = request
    process_staffing_outbox(factory, api, CONFIG, clock.now(), enabled=True)
    assert state(factory, ident)[0] == 'unknown'
    process_staffing_outbox(factory, api, CONFIG, clock.now()+timedelta(minutes=2), enabled=True)
    assert len(api.writes) == 1 and state(factory, ident)[0] == 'unknown'
    session.expire_all()
    assert assignment.status == 'approved'
    assert len(list(session.scalars(select(Shift)))) == 5 and session.scalar(select(RoleRecipe)) is None
    assert session.get(PCOStaffingLink, assignment.id) is None


@pytest.mark.parametrize('hold', ['membership', 'qualification', 'consent'])
def test_reservations_keep_existing_serving_guards(session, clock, make_volunteer, hold):
    person, assignment, api, factory = setup_assignment(session, clock, make_volunteer, status='approved')
    if hold == 'membership': api.memberships = False
    elif hold == 'qualification': assignment.shift.role.required_qualifications = ['sound_training']
    else: person.sms_opt_in = False
    ident = reserve(session, assignment, clock)
    process_staffing_outbox(factory, api, CONFIG, clock.now(), enabled=True)
    assert state(factory, ident)[0] == 'held' and not api.writes
