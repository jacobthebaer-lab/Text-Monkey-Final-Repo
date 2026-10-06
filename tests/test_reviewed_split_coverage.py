"""Actual in-memory offer/reply/review paths; no external models or sends."""
from copy import deepcopy
from datetime import timedelta
import json
from types import SimpleNamespace

import pytest
from sqlalchemy import select

from app.agents.fill_agent import FillContext
from app.core import eligibility, notifications, offer_windows, reminders, scheduler, split_coverage as split
from app.core.inbound import handle_inbound
from app.db import models as m
from tests.test_fill_agent import ScriptedAgentGloo, historical_invitation, parser_returning

OWNER = 'reviewing-coordinator@example.invalid'


@pytest.fixture
def prepared(session, clock, provider, make_shift, make_volunteer, tmp_path):
    return prepare_partial(session, clock, provider, make_shift, make_volunteer, tmp_path)


def prepare_partial(session, clock, provider, make_shift, make_volunteer, tmp_path):
    parent = make_shift('Fictional Interval Hospitality', minutes=120)
    helper = make_volunteer('Fictional Initial Partial Helper')
    if session.get(m.Policy,f'split_role:{parent.role_id}') is None:
        session.add(m.Policy(key=f'split_role:{parent.role_id}', value={'value': True}))
    fill = m.FillRequest(shift_id=parent.id, urgency='normal', state='in_progress', created_at=clock.now())
    session.add(fill); session.flush()
    outreach = historical_invitation(session, clock, helper, fill)
    ctx = FillContext(session, clock, provider, ScriptedAgentGloo(), log_dir=tmp_path)
    handle_inbound(session, clock, provider, helper.phone, 'I can serve only the first hour',
        parser_returning(intent='partial', partial_window='first hour'), ctx=ctx)
    incoming = session.scalar(select(m.Message).where(m.Message.direction == 'in',m.Message.phone==helper.phone)
        .order_by(m.Message.id.desc()))
    facts = split.partial_facts(session, parent.id, outreach.id, incoming.id, clock.now())
    normalized = split.extract_partial(SimpleNamespace(create_response=lambda **kw: SimpleNamespace(output_text=json.dumps({
        'start': parent.starts_at.isoformat(), 'end': (parent.starts_at+timedelta(hours=1)).isoformat()}))), facts)
    review = split.stage_partition(session, OWNER, facts, normalized, clock.now())
    return SimpleNamespace(parent=parent, ctx=ctx, review=review, facts=facts, normalized=normalized, partial=helper)


def partition(session, clock, prepared):
    review = split.decide(session, prepared.review.id, OWNER, prepared.review.payload['content_hash'], True, clock.now())
    return [session.get(m.Shift, ident) for ident in review.payload['applied_child_ids']]


def delivered_child_offer(session, clock, person, fill):
    child = session.get(m.Shift, fill.shift_id)
    fill.state, fill.current_tranche = 'in_progress', 1
    outreach = m.Outreach(fill_request_id=fill.id, volunteer_id=person.id, tranche=1)
    session.add(outreach);session.flush()
    metadata = offer_windows.prepare(session, outreach, 'Could you serve '+offer_windows.interval_label(session,child)+'?', clock.now())
    message = m.Message(direction='out', volunteer_id=person.id, phone=person.phone, body=metadata.body,
        purpose='outreach',kind='ai',status='sent',provider_sid='SYNTHETIC-INTERVAL-HISTORY',created_at=clock.now())
    session.add(message);session.flush();outreach.message_id=message.id
    assert offer_windows.dispatch(session,outreach,message,clock.now()) is None
    session.flush()
    return outreach


def accept_children(session, clock, prepared, make_volunteer, slots):
    people = []
    for child in slots:
        person = make_volunteer(f'Fictional Interval Helper {child.id}')
        people.append(person)
        fill = session.scalar(select(m.FillRequest).where(m.FillRequest.shift_id == child.id))
        outreach = delivered_child_offer(session, clock, person, fill)
        outcome = handle_inbound(session, clock, prepared.ctx.provider, person.phone, 'Yes, that exact interval works',
            parser_returning(intent='accept', shift_hint=None), ctx=prepared.ctx)
        assert 'awaiting_split_review' in outcome.notes
        assert outreach.response == 'yes' and fill.state == 'waiting_split_review'
    return people


def test_complete_two_review_path_shares_event_without_parent_assignment_or_early_booking(
    session, clock, prepared, make_volunteer
):
    original_event_count = len(session.scalars(select(m.Event)).all())
    slots = partition(session, clock, prepared)
    assert [(s.starts_at, s.ends_at) for s in slots] == [
        (prepared.parent.starts_at, prepared.parent.starts_at+timedelta(hours=1)),
        (prepared.parent.starts_at+timedelta(hours=1), prepared.parent.ends_at)]
    assert all(s.event_id == prepared.parent.event_id for s in slots)
    assert len(session.scalars(select(m.Event)).all()) == original_event_count
    assert not split.coverage(session, prepared.parent)['fully_covered']
    assert prepared.parent.id not in {s.id for s in scheduler.shifts_for(session, '2026-10')}
    people = accept_children(session, clock, prepared, make_volunteer, slots)
    assert session.scalar(select(m.Assignment)) is None
    booking = split.stage_booking(session, OWNER, prepared.parent.id, clock.now())
    split.decide(session, booking.id, OWNER, booking.payload['content_hash'], True, clock.now())
    assert all(r.state=='consumed' for r in session.scalars(select(m.Notification).where(
        m.Notification.purpose=='split_acceptance')))
    assert split.coverage(session, prepared.parent)['fully_covered']
    assignments = session.scalars(select(m.Assignment)).all()
    assert {a.shift_id for a in assignments} == {s.id for s in slots}
    assert {a.volunteer_id for a in assignments} == {p.id for p in people}
    assert session.scalar(select(m.Assignment).where(m.Assignment.shift_id == prepared.parent.id)) is None
    assert notifications.staffing_snapshot(session, prepared.parent.event)['fully_staffed']
    split.decide(session, booking.id, OWNER, booking.payload['content_hash'], True, clock.now())
    assert len(session.scalars(select(m.Assignment)).all()) == 2
    assert not prepared.ctx.provider.sent


def test_original_partial_helper_gets_fresh_child_offer_only_after_cooldown(session,clock,prepared):
    from app.agents.fill_agent import advance_due, replacement_pool
    from app.core import algorithm_outreach as algorithm
    session.add(m.Policy(key='algorithm_outreach_enabled',value={'value':True}))
    slots=partition(session,clock,prepared)
    scripted=prepared.ctx.gloo.create_response
    def exact_intervals(**kwargs):
        result=scripted(**kwargs)
        if isinstance(kwargs['input'],list) and not any(
                isinstance(i,dict) and i.get('type')=='function_call_output' for i in kwargs['input']):
            facts=json.loads(kwargs['input'][0]['content'])
            child=session.get(m.Shift,facts['shift']['shift_id'])
            for call in result.output:
                if getattr(call,'name',None)=='request_send_text':
                    args=json.loads(call.arguments)
                    args['body']='Could you serve '+offer_windows.interval_label(session,child)+'? Reply YES or NO.'
                    call.arguments=json.dumps(args)
        return result
    prepared.ctx.gloo.create_response=exact_intervals
    fills=session.scalars(select(m.FillRequest).where(m.FillRequest.shift_id.in_([s.id for s in slots]))).all()
    assert not replacement_pool(session,fills[0],clock.now(),'America/Denver')
    assert not prepared.ctx.provider.sent  # Existing 24-hour contact cooldown.
    clock.advance(timedelta(hours=25))
    split.start_outreach(session,prepared.parent.id,OWNER,prepared.review.payload['content_hash'],clock.now())
    advance_due(prepared.ctx)
    asks=session.scalars(select(m.Outreach).join(m.FillRequest).where(
        m.FillRequest.shift_id.in_([s.id for s in slots]),m.Outreach.volunteer_id==prepared.partial.id)).all()
    assert len(asks)==1 and asks[0].message_id is not None, [r.result for r in session.scalars(
        select(m.AgentStep).order_by(m.AgentStep.id.desc()).limit(5))]
    assert session.get(m.FillRequest,asks[0].fill_request_id).shift_id==slots[0].id
    assert asks[0].response=='none' and session.scalar(select(m.Assignment)) is None
    assert offer_windows.interval_label(session,slots[0]) in session.get(m.Message,asks[0].message_id).body
    # The fresh child reservation restores event-wide exclusion for siblings.
    assert prepared.partial.id in algorithm.contacted_for_event(session,fills[1])
    advance_due(prepared.ctx)
    assert len(prepared.ctx.provider.sent)==1


def test_applied_receipts_do_not_starve_later_pending_expiry(session,clock,prepared,make_shift,make_volunteer,tmp_path):
    slots=partition(session,clock,prepared)
    accept_children(session,clock,prepared,make_volunteer,slots)
    booking=split.stage_booking(session,OWNER,prepared.parent.id,clock.now())
    split.decide(session,booking.id,OWNER,booking.payload['content_hash'],True,clock.now())
    old=session.scalar(select(m.Notification).where(m.Notification.purpose=='split_acceptance'))
    for i in range(201):
        session.add(m.Notification(key=f'historical-split:{i:03}',purpose='split_acceptance',state='held',
            body='',created_at=clock.now(),due_at=clock.now(),expires_at=clock.now(),detail=deepcopy(old.detail)))
    fresh=prepare_partial(session,clock,prepared.ctx.provider,make_shift,make_volunteer,tmp_path)
    new_slots=partition(session,clock,fresh);fills=[];receipts=[]
    for index,child in enumerate(new_slots):
        if index:clock.advance(timedelta(minutes=1))
        person=make_volunteer();fill=session.scalar(select(m.FillRequest).where(m.FillRequest.shift_id==child.id))
        offer=delivered_child_offer(session,clock,person,fill)
        handle_inbound(session,clock,fresh.ctx.provider,person.phone,'YES',parser_returning(intent='accept'),ctx=fresh.ctx)
        fills.append(fill);receipts.append(session.get(m.Notification,f'split_accept:{offer.id}'))
    pending=split.stage_booking(session,OWNER,fresh.parent.id,clock.now())
    clock.set_time(receipts[0].expires_at)
    split.expire_acceptances(session,clock.now());split.expire_acceptances(session,clock.now())
    assert pending.status=='expired' and receipts[0].state=='expired'
    assert fills[0].state=='in_progress' and fills[1].state=='waiting_split_review'
    assert receipts[1].state=='held'
    assert all(r.state=='consumed' for r in session.scalars(select(m.Notification).where(
        m.Notification.key.startswith('historical-split:'))))
    assert len(session.scalars(select(m.Assignment)).all())==2


def test_one_accepted_child_cannot_fill_parent_or_skip_atomic_review(session, clock, prepared, make_volunteer):
    slots = partition(session, clock, prepared)
    person = accept_children(session, clock, prepared, make_volunteer, slots[:1])[0]
    with pytest.raises(ValueError, match='Every child'):
        split.stage_booking(session, OWNER, prepared.parent.id, clock.now())
    assert not split.coverage(session, prepared.parent)['fully_covered']
    assert not notifications.staffing_snapshot(session, prepared.parent.event)['fully_staffed']
    assert scheduler.propose(session, clock, slots[1], person, 'America/Denver')['error']
    with pytest.raises(ValueError, match='atomic exact split review'):
        session.add(m.Assignment(shift_id=slots[0].id, volunteer_id=person.id, status='confirmed',
            source='admin', created_at=clock.now(), updated_at=clock.now()))
        session.flush()


def test_final_apply_is_atomic_when_second_helper_loses_consent(session, clock, prepared, make_volunteer):
    slots = partition(session, clock, prepared)
    people = accept_children(session, clock, prepared, make_volunteer, slots)
    booking = split.stage_booking(session, OWNER, prepared.parent.id, clock.now())
    people[-1].sms_opt_in = False
    session.flush()
    with pytest.raises(ValueError, match='authorized'):
        split.decide(session, booking.id, OWNER, booking.payload['content_hash'], True, clock.now())
    assert session.scalar(select(m.Assignment)) is None
    assert booking.status == 'pending' and not split.coverage(session, prepared.parent)['fully_covered']


@pytest.mark.parametrize('change', ['new_input', 'offer_body', 'qualification', 'split_disabled', 'expired'])
def test_final_apply_revalidates_actual_source_and_eligibility(session, clock, prepared, make_volunteer, change):
    slots = partition(session, clock, prepared)
    people = accept_children(session, clock, prepared, make_volunteer, slots)
    booking = split.stage_booking(session, OWNER, prepared.parent.id, clock.now())
    if change == 'new_input':
        session.add(m.Message(direction='in', volunteer_id=people[-1].id, phone=people[-1].phone,
            body='Actually I cannot do that', kind='inbound', status='received', created_at=clock.now()))
    elif change == 'offer_body':
        binding = booking.payload['children'][-1]
        session.get(m.Message, binding['outgoing_id']).body = 'Changed time offer'
    elif change == 'qualification':
        slots[-1].role.required_qualifications = ['fictional_admin_clearance']
    elif change == 'split_disabled':
        session.get(m.Policy, f'split_role:{prepared.parent.role_id}').value = {'value': False}
    else:
        clock.set_time(split.instant(booking.payload['expires_at']))
    session.flush()
    with pytest.raises(ValueError):
        split.decide(session, booking.id, OWNER, booking.payload['content_hash'], True, clock.now())
    assert session.scalar(select(m.Assignment)) is None
    assert booking.status == 'pending'


def test_cancel_reopens_only_existing_child_interval_and_parent_reports_exact_gap(
    session, clock, prepared, make_volunteer
):
    slots = partition(session, clock, prepared)
    people = accept_children(session, clock, prepared, make_volunteer, slots)
    booking = split.stage_booking(session, OWNER, prepared.parent.id, clock.now())
    split.decide(session, booking.id, OWNER, booking.payload['content_hash'], True, clock.now())
    interval = (slots[0].starts_at, slots[0].ends_at)
    result = handle_inbound(session, clock, prepared.ctx.provider, people[0].phone, 'I must cancel my interval',
        parser_returning(intent='cancel', shift_hint='Fictional Interval Hospitality'), ctx=prepared.ctx)
    assert result.routed_to == 'fill_agent'
    coverage = split.coverage(session, prepared.parent)
    assert not coverage['fully_covered'] and [g['shift_id'] for g in coverage['gaps']] == [slots[0].id]
    assert (slots[0].starts_at, slots[0].ends_at) == interval
    assert session.scalar(select(m.FillRequest).where(m.FillRequest.shift_id == slots[0].id,
        m.FillRequest.cancelled_assignment_id.is_not(None))) is not None
    assert session.scalar(select(m.Assignment).where(m.Assignment.shift_id == slots[1].id)).status == 'confirmed'
    assert not notifications.staffing_snapshot(session, prepared.parent.event)['fully_staffed']


def test_child_interval_controls_window_overlap_and_reminder_source(session, clock, prepared, make_volunteer, make_shift):
    slots = partition(session, clock, prepared)
    person = make_volunteer(prefs={'recurring_windows': [{'weekday':6, 'role_ids':[prepared.parent.role_id],
        'role_label':prepared.parent.role.name, 'any_role':False, 'start_time':'10:00', 'end_time':'11:00',
        'all_day':False, 'event_context':None}]})
    assert not eligibility.check(session, person, slots[0])
    assert eligibility.check(session, person, slots[1])
    source = reminders.assignment_source(SimpleNamespace(id=99, shift=slots[1], shift_id=slots[1].id, volunteer_id=person.id), 'reminder')
    assert split.instant(source['starts_at']) == slots[1].starts_at
    assert split.instant(source['ends_at']) == slots[1].ends_at
    concurrent = make_shift('Fictional Different Duty', starts=prepared.parent.starts_at, minutes=60)
    session.add(m.Assignment(shift_id=concurrent.id, volunteer_id=person.id, status='confirmed',
        source='admin', created_at=clock.now(), updated_at=clock.now()))
    session.flush()
    assert eligibility.check(session, person, slots[1])  # Touching boundary is not overlap.
    concurrent.event.ends_at += timedelta(seconds=1)
    assert not eligibility.check(session, person, slots[1])


@pytest.mark.parametrize('change', ['no_opt_in', 'pco', 'source_body', 'parent_time', 'wrong_owner', 'hash'])
def test_partition_review_is_source_bound_and_refuses_unsafe_parent(session, clock, prepared, change):
    if change == 'no_opt_in':
        session.get(m.Policy, f'split_role:{prepared.parent.role_id}').value = {'value': False}
    elif change == 'pco':
        prepared.parent.event.gcal_event_id = 'pco:fictional-import'
    elif change == 'source_body':
        session.get(m.Message, prepared.facts['incoming_id']).body = 'Different actual reply'
    elif change == 'parent_time':
        prepared.parent.event.ends_at += timedelta(minutes=15)
    session.flush()
    with pytest.raises(ValueError):
        split.decide(session, prepared.review.id, 'different-owner' if change == 'wrong_owner' else OWNER,
            'changed-hash' if change == 'hash' else prepared.review.payload['content_hash'], True, clock.now())
    assert not split.children(session, prepared.parent.id) and session.scalar(select(m.Assignment)) is None
    assert prepared.review.status == 'pending'


@pytest.mark.parametrize('result', [None, {'start':None, 'end':None}, {'start':'09:00', 'end':'10:00'},
    {'start':'2026-10-04T09:00:00', 'end':'2026-10-04T10:00:00'},
    {'start':'2026-10-04T09:00:00-06:00', 'end':'2026-10-04T12:00:00-06:00'}])
def test_gloo_ambiguity_or_invalid_interval_never_creates_partition(session, clock, prepared, result):
    fake = SimpleNamespace(create_response=lambda **kw: SimpleNamespace(output_text=json.dumps(result)))
    with pytest.raises(ValueError):
        split.extract_partial(fake, prepared.facts)
    assert not split.children(session, prepared.parent.id)


@pytest.mark.parametrize('change',['duplicate','overlap','gap'])
def test_even_rehashed_partition_refuses_invalid_exact_cover(session,clock,prepared,change):
    payload=deepcopy(prepared.review.payload)
    if change=='duplicate':payload['intervals'].append(deepcopy(payload['intervals'][0]))
    elif change=='overlap':payload['intervals'][1]['start']=payload['intervals'][0]['start']
    else:payload['intervals'].pop()
    payload['content_hash']=split.digest(payload)
    prepared.review.payload=payload;session.flush()
    with pytest.raises(ValueError,match='overlap, duplicate'):
        split.decide(session,prepared.review.id,OWNER,payload['content_hash'],True,clock.now())
    assert not split.children(session,prepared.parent.id)


def test_incomplete_child_copy_is_refused_before_dispatch(session,clock,prepared,make_volunteer):
    child=partition(session,clock,prepared)[0]
    person=make_volunteer('Fictional Bad Interval Copy')
    fill=session.scalar(select(m.FillRequest).where(m.FillRequest.shift_id==child.id))
    fill.state='in_progress'
    outreach=m.Outreach(fill_request_id=fill.id,volunteer_id=person.id,tranche=1)
    session.add(outreach);session.flush()
    meta=offer_windows.prepare(session,outreach,'Could you serve at the full event?',clock.now())
    message=m.Message(direction='out',volunteer_id=person.id,phone=person.phone,body=meta.body,kind='ai',
        purpose='outreach',status='queued',created_at=clock.now())
    session.add(message);session.flush()
    assert 'both exact dated' in offer_windows.dispatch(session,outreach,message,clock.now())
    assert meta.state=='offer_revoked' and session.scalar(select(m.Assignment)) is None


def test_partition_outreach_is_explicit_source_bound_and_receipt_expiry_reopens_same_child(
    session,clock,prepared,make_volunteer
):
    slots=partition(session,clock,prepared)
    fills=session.scalars(select(m.FillRequest).where(m.FillRequest.shift_id.in_([c.id for c in slots]))).all()
    assert all(f.state=='open' and f.next_action_at is None for f in fills)
    with pytest.raises(ValueError):split.start_outreach(session,prepared.parent.id,'other-owner',prepared.review.payload['content_hash'],clock.now())
    split.start_outreach(session,prepared.parent.id,OWNER,prepared.review.payload['content_hash'],clock.now())
    assert all(f.state=='in_progress' for f in fills)
    accept_children(session,clock,prepared,make_volunteer,slots)
    booking=split.stage_booking(session,OWNER,prepared.parent.id,clock.now())
    clock.set_time(split.instant(booking.payload['expires_at']))
    split.expire_acceptances(session,clock.now())
    assert booking.status=='expired' and all(f.state=='in_progress' for f in fills)
    assert {f.shift_id for f in fills}=={c.id for c in slots}
    assert session.scalar(select(m.Assignment)) is None and not prepared.ctx.provider.sent


def test_ongoing_native_scope_accepts_null_expiry_but_rejects_new_session(session,clock,prepared):
    incoming=session.get(m.Message,prepared.facts['incoming_id'])
    outgoing=session.get(m.Message,prepared.facts['outgoing_id'])
    source=SimpleNamespace(id='fictional-current',starts_at=clock.now()-timedelta(days=2),expires_at=None,
        outbound_prefix='MACfictional-current:',active=lambda now:True)
    incoming.purpose='test:'+source.id
    outgoing.provider_sid=source.outbound_prefix+'historical'
    session.info['split_sessions']={incoming.phone:source}
    facts=split.partial_facts(session,prepared.parent.id,prepared.facts['outreach_id'],incoming.id,clock.now())
    assert facts['session']['expires_at'] is None
    source.id='different-session'
    with pytest.raises(ValueError,match='current active'):
        split.partial_facts(session,prepared.parent.id,prepared.facts['outreach_id'],incoming.id,clock.now())


def test_timely_original_partial_is_not_discarded_by_arbitrary_receipt_age(session,clock,prepared):
    clock.set_time(clock.now()+timedelta(hours=3))
    facts=split.partial_facts(session,prepared.parent.id,prepared.facts['outreach_id'],prepared.facts['incoming_id'],clock.now())
    assert facts['incoming_id']==prepared.facts['incoming_id']
    # A new human proposal has a bounded review lifetime; the original actual
    # receipt must be timely relative to its offer, rather than relative to today.
    review=split.stage_partition(session,OWNER,facts,prepared.normalized,clock.now())
    assert review.status=='pending'


def test_child_pco_enqueue_holds_before_mapping_or_external_write(session,clock,prepared,make_volunteer):
    from app.integrations.planning_center import PCOBase,PlanningCenterError
    from app.integrations.planning_center_staffing import enqueue_staffing_intent
    from tests.test_planning_center import CONFIG
    PCOBase.metadata.create_all(session.get_bind())
    slots=partition(session,clock,prepared)
    accept_children(session,clock,prepared,make_volunteer,slots)
    review=split.stage_booking(session,OWNER,prepared.parent.id,clock.now())
    split.decide(session,review.id,OWNER,review.payload['content_hash'],True,clock.now())
    assignment=session.scalar(select(m.Assignment))
    with pytest.raises(PlanningCenterError,match='partial'):
        enqueue_staffing_intent(session,CONFIG,assignment_id=assignment.id,action='accept',now=clock.now())


def restage_at(session,clock,prepared,start,end,cut):
    prepared.parent.event.starts_at,prepared.parent.event.ends_at=start,end
    source=prepared.facts
    metadata=offer_windows.metadata(session,session.get(m.Outreach,source['outreach_id']))
    metadata.detail={**metadata.detail,'snapshot':offer_windows.snapshot(prepared.parent)}
    session.flush()
    facts=split.partial_facts(session,prepared.parent.id,source['outreach_id'],source['incoming_id'],clock.now())
    normalized=split.extract_partial(SimpleNamespace(create_response=lambda **kw:SimpleNamespace(output_text=json.dumps({
        'start':start.isoformat(),'end':cut.isoformat()}))),facts)
    prepared.review=split.stage_partition(session,OWNER,facts,normalized,clock.now())
    return partition(session,clock,prepared)


def test_midnight_child_uses_its_own_month_in_python_and_sql(session,clock,prepared,make_volunteer):
    start=split.instant('2026-10-31T23:00:00-06:00')
    cut=split.instant('2026-11-01T00:00:00-06:00')
    end=split.instant('2026-11-01T01:00:00-06:00')
    slots=restage_at(session,clock,prepared,start,end,cut)
    assert [s.id for s in scheduler.shifts_for(session,'2026-10')]==[slots[0].id]
    assert [s.id for s in scheduler.shifts_for(session,'2026-11')]==[slots[1].id]
    people=accept_children(session,clock,prepared,make_volunteer,slots)
    booking=split.stage_booking(session,OWNER,prepared.parent.id,clock.now())
    split.decide(session,booking.id,OWNER,booking.payload['content_hash'],True,clock.now())
    assert scheduler.load(session,people[1].id,slots[0].interval_event,'America/Denver')==0
    assert scheduler.load(session,people[1].id,slots[1].interval_event,'America/Denver')==1


def test_dst_fold_invitation_names_both_offsets_and_exact_elapsed_intervals(session,clock,prepared):
    start=split.instant('2026-11-01T01:00:00-06:00')
    cut=split.instant('2026-11-01T01:00:00-07:00')
    end=split.instant('2026-11-01T02:00:00-07:00')
    first,second=restage_at(session,clock,prepared,start,end,cut)
    label=offer_windows.interval_label(session,first)
    assert 'MDT (UTC-0600)' in label and 'MST (UTC-0700)' in label
    assert first.ends_at-first.starts_at==second.ends_at-second.starts_at==timedelta(hours=1)
    assert not split.coverage(session,prepared.parent)['fully_covered']


def test_child_native_reminder_rechecks_parent_review_scope(session,clock,prepared,make_volunteer):
    slots=partition(session,clock,prepared)
    people=accept_children(session,clock,prepared,make_volunteer,slots)
    booking=split.stage_booking(session,OWNER,prepared.parent.id,clock.now())
    split.decide(session,booking.id,OWNER,booking.payload['content_hash'],True,clock.now())
    clock.set_time(prepared.parent.starts_at-timedelta(days=1))
    row=session.scalar(select(m.Assignment).where(m.Assignment.shift_id==slots[0].id))
    source=reminders.assignment_source(row,'reminder')
    assert reminders.source_problem(session,people[0],source,clock.now()) is None
    assert offer_windows.interval_label(session,slots[0]) in reminders.day_before_copy(row,__import__('zoneinfo').ZoneInfo('America/Denver'))
    prepared.parent.event.ends_at+=timedelta(minutes=1);session.flush()
    assert 'no longer eligible' in reminders.source_problem(session,people[0],source,clock.now())
    assert split.coverage(session,prepared.parent)['held']


def test_actual_replacement_acceptance_restores_only_canceled_child(session,clock,prepared,make_volunteer):
    slots=partition(session,clock,prepared)
    people=accept_children(session,clock,prepared,make_volunteer,slots)
    review=split.stage_booking(session,OWNER,prepared.parent.id,clock.now())
    split.decide(session,review.id,OWNER,review.payload['content_hash'],True,clock.now())
    sibling=session.scalar(select(m.Assignment).where(m.Assignment.shift_id==slots[1].id))
    sibling_id=sibling.id
    handle_inbound(session,clock,prepared.ctx.provider,people[0].phone,'I need to cancel',
        parser_returning(intent='cancel',shift_hint='Fictional Interval Hospitality'),ctx=prepared.ctx)
    replacement=make_volunteer('Fictional Exact Interval Replacement')
    fill=session.scalar(select(m.FillRequest).where(m.FillRequest.shift_id==slots[0].id,
        m.FillRequest.cancelled_assignment_id.is_not(None)))
    delivered_child_offer(session,clock,replacement,fill)
    result=handle_inbound(session,clock,prepared.ctx.provider,replacement.phone,'Yes, I can cover that interval',
        parser_returning(intent='accept'),ctx=prepared.ctx)
    assert 'filled' in result.notes
    assert split.coverage(session,prepared.parent)['fully_covered']
    assert session.get(m.Assignment,sibling_id).volunteer_id==people[1].id
    assert session.scalar(select(m.Assignment).where(m.Assignment.shift_id==slots[0].id,
        m.Assignment.status=='confirmed')).volunteer_id==replacement.id
    assert session.scalar(select(m.Assignment).where(m.Assignment.shift_id==prepared.parent.id)) is None


def test_child_cannot_be_silently_converted_back_to_whole_slot(session,clock,prepared):
    child=partition(session,clock,prepared)[0]
    child.parent_shift_id=child.interval_starts_at=child.interval_ends_at=child.coverage_review_id=None
    session.info['record_authorized']=True
    with pytest.raises(ValueError,match='cannot be changed independently'):
        session.flush()
