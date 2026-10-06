"""Distinct actual event replies create internal concerns, never messages."""
from datetime import timedelta
from types import SimpleNamespace

import pytest
from sqlalchemy import select

from app.core import repeated_declines as declines
from app.db import models as m


@pytest.fixture
def established(session, clock, make_volunteer, make_shift, assign):
    person = make_volunteer('Fictional Volunteer')
    for days in (120, 100, 80):
        shift = make_shift(starts=clock.now()-timedelta(days=days))
        shift.event.status = 'completed'
        assign(person, shift, status='completed')
    session.flush()
    return person


def offer(session, clock, person, make_shift, days, *, status='submitted', body='No, I cannot serve then.',
          response='no', event=None):
    replied = clock.now()-timedelta(days=days)
    shift = make_shift(starts=replied+timedelta(days=2))
    if event is not None: shift.event_id=event.id
    session.flush()
    fill=m.FillRequest(shift_id=shift.id, state='in_progress', urgency='normal',
        created_at=replied-timedelta(hours=1),current_tranche=1)
    session.add(fill);session.flush()
    outgoing=m.Message(direction='out',phone=person.phone,volunteer_id=person.id,body='A synthetic event offer.',
        kind='ai',purpose='outreach',status=status,provider_sid='MAC'+'a'*32+':synthetic',
        created_at=replied-timedelta(minutes=10))
    incoming=m.Message(direction='in',phone=person.phone,volunteer_id=person.id,body=body,
        kind='mac_test_in',purpose='test:'+'a'*32,status='received',created_at=replied)
    session.add_all([outgoing,incoming]);session.flush()
    outreach=m.Outreach(fill_request_id=fill.id,volunteer_id=person.id,tranche=1,message_id=outgoing.id,
        response=response,responded_at=replied)
    session.add(outreach);session.flush()
    window=m.Notification(key=f'offer:{outreach.id}',purpose='offer_window',state='offer_active',
        volunteer_id=person.id,event_id=shift.event_id,message_id=outgoing.id,body=outgoing.body,
        due_at=replied+timedelta(hours=1),expires_at=replied+timedelta(hours=1),created_at=outgoing.created_at,
        detail={'fill_request_id':fill.id,'shift_id':shift.id,'dispatched_at':outgoing.created_at.isoformat()})
    session.add(window);session.flush()
    return SimpleNamespace(outreach=outreach,incoming=incoming,outgoing=outgoing,window=window,shift=shift)


def pattern(session, clock, person, make_shift):
    samples=[offer(session,clock,person,make_shift,days) for days in (40,25,10)]
    for sample in samples:
        assert declines.record_decline(session,sample.outreach,sample.incoming.id,clock.now())
    return samples


def test_real_declines_distinct_events_deduplicate_and_never_contact(session,clock,established,make_shift):
    samples=pattern(session,clock,established,make_shift)
    before=len(session.scalars(select(m.Message)).all())
    result=declines.refresh(session,clock.now())
    assert result['held'] is None and len(result['flags'])==1
    flag=result['flags'][0];session.flush();identifier=flag.id
    assert flag.type=='repeated_declines' and flag.status=='open'
    assert [item['outreach_id'] for item in flag.evidence['declines']]==[s.outreach.id for s in samples]
    assert all(item['delivery_evidence']=='submitted_offer_with_actual_scoped_decline' for item in flag.evidence['declines'])
    assert len(flag.evidence['completed_history_event_ids'])==3
    assert len(declines.refresh(session,clock.now())['flags'])==1
    assert session.scalar(select(m.Flag)).id==identifier
    assert len(session.scalars(select(m.Message)).all())==before
    assert declines.record_decline(session,samples[0].outreach,samples[0].incoming.id,clock.now())
    assert len(session.scalars(select(m.Notification).where(m.Notification.purpose=='decline_evidence')).all())==3


@pytest.mark.parametrize('status',['queued','dispatching','failed','rejected','uncertain','blocked_policy'])
def test_unsent_failed_or_uncertain_offer_is_not_a_decline(session,clock,established,make_shift,status):
    sample=offer(session,clock,established,make_shift,10,status=status)
    assert declines.record_decline(session,sample.outreach,sample.incoming.id,clock.now()) is None
    assert not declines.refresh(session,clock.now())['flags']


@pytest.mark.parametrize('body,response', [('STOP','no'),('Please stop texting me.','no'),
    ('Yes, I can serve.','yes'),('I can serve half.','partial')])
def test_controls_and_nondeclines_do_not_count(session,clock,established,make_shift,body,response):
    sample=offer(session,clock,established,make_shift,10,body=body,response=response)
    assert declines.record_decline(session,sample.outreach,sample.incoming.id,clock.now()) is None


def test_bare_legacy_no_codes_are_not_replayed_or_inferred(session,clock,established,make_shift):
    for days in (40,25,10):offer(session,clock,established,make_shift,days)
    assert declines.refresh(session,clock.now())['flags']==[]
    assert not session.scalars(select(m.Notification).where(m.Notification.purpose=='decline_evidence')).all()


def test_multiple_roles_on_one_event_are_one_observation(session,clock,established,make_shift):
    first=offer(session,clock,established,make_shift,40)
    for sample in [first]+[offer(session,clock,established,make_shift,days,event=first.shift.event) for days in (25,10)]:
        # The single event is after all response times; still one event.
        sample.shift.event.starts_at=clock.now()+timedelta(days=2)
        sample.shift.event.ends_at=clock.now()+timedelta(days=2,hours=1)
        assert declines.record_decline(session,sample.outreach,sample.incoming.id,clock.now())
    assert not declines.refresh(session,clock.now())['flags']


def test_new_volunteer_or_concentrated_replies_cannot_establish_pattern(session,clock,make_volunteer,make_shift):
    person=make_volunteer('New Fictional',created_at=clock.now()-timedelta(days=2))
    pattern(session,clock,person,make_shift)
    assert not declines.refresh(session,clock.now())['flags']


def test_thresholds_are_configurable_and_resolution_is_current(session,clock,established,make_shift):
    pattern(session,clock,established,make_shift)
    flag=declines.refresh(session,clock.now())['flags'][0]
    assert not declines.refresh(session,clock.now(),config={'minimum_events':4})['flags']
    assert flag.status=='resolved' and flag.evidence['current'] is False
    assert declines.refresh(session,clock.now())['flags'][0] is flag and flag.status=='open'
    flag.status='dismissed';session.flush()
    assert not declines.refresh(session,clock.now())['flags'] and flag.status=='dismissed'


def test_optout_resolves_existing_flag_without_pressure(session,clock,established,make_shift):
    pattern(session,clock,established,make_shift)
    flag=declines.refresh(session,clock.now())['flags'][0]
    established.sms_opt_in=False;session.flush()
    assert not declines.refresh(session,clock.now())['flags'] and flag.status=='resolved'


@pytest.mark.parametrize('defect',['body','scope','event','offer','source','chronology'])
def test_altered_proof_is_not_reused(session,clock,established,make_shift,defect):
    samples=pattern(session,clock,established,make_shift)
    sample=samples[0]
    if defect=='body':sample.incoming.body='An unrelated later reply'
    if defect=='scope':sample.incoming.purpose='test:wrong'
    if defect=='event':sample.shift.event.starts_at+=timedelta(hours=1)
    if defect=='offer':sample.outgoing.body='Changed invitation'
    if defect=='source':sample.incoming.volunteer_id=None
    if defect=='chronology':sample.outreach.responded_at=sample.outgoing.created_at-timedelta(seconds=1)
    session.flush()
    assert not declines.refresh(session,clock.now())['flags']


@pytest.mark.parametrize('config',[{'minimum_events':True},{'unknown':1},{'minimum_events':1},
    {'window_days':1,'minimum_span_days':14}])
def test_invalid_policy_holds_without_resolving_current_flags(session,clock,established,make_shift,config):
    pattern(session,clock,established,make_shift)
    flag=declines.refresh(session,clock.now())['flags'][0]
    assert declines.refresh(session,clock.now(),config=config)['held']=='invalid_repeated_decline_policy'
    assert flag.status=='open'


def test_bounded_scan_holds_without_resolving_current_flags(session,clock,established,make_shift):
    pattern(session,clock,established,make_shift)
    flag=declines.refresh(session,clock.now())['flags'][0]
    assert declines.refresh(session,clock.now(),config={'maximum_records':1})['held']=='repeated_decline_evidence_limit'
    assert flag.status=='open'


def test_declines_must_span_time_not_one_busy_day(session,clock,established,make_shift):
    for days in (3,2,1):
        sample=offer(session,clock,established,make_shift,days)
        assert declines.record_decline(session,sample.outreach,sample.incoming.id,clock.now())
    assert not declines.refresh(session,clock.now())['flags']


def test_uncompleted_history_cannot_establish_prior_serving(session,clock,established,make_shift):
    pattern(session,clock,established,make_shift)
    for assignment in session.scalars(select(m.Assignment)):
        assignment.shift.event.status='cancelled'
    session.flush()
    assert not declines.refresh(session,clock.now())['flags']


def test_expired_window_resolves_without_new_flag_or_message(session,clock,established,make_shift):
    pattern(session,clock,established,make_shift)
    flag=declines.refresh(session,clock.now())['flags'][0]
    clock.advance(timedelta(days=60))
    assert not declines.refresh(session,clock.now())['flags']
    assert flag.status=='resolved' and len(session.scalars(select(m.Flag)).all())==1
