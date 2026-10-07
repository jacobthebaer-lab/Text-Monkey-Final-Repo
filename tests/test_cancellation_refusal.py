"""Fictional persisted cancellation intervals, no native or model calls."""
import hashlib
from dataclasses import replace
from datetime import timedelta
from types import SimpleNamespace
import pytest
from sqlalchemy import select
from app.core import cancellation_refusal as refusal, eligibility, ranking, scheduler, confirmations
from app.core.cancellation_scope import snapshot, bookings
from app.agents.fill_agent import FillContext, cancel_recorded_assignment
from app.db import models as m
from app.llm.gloo_client import GlooUnavailableError
from app.sms.mac_provider import MacMessagesProvider
from app.integrations.mac_models import MacInboundReceipt
from tests.conftest import NOW


@pytest.fixture
def example(session, clock, provider, make_volunteer, make_shift, assign):
    person=make_volunteer(prefs={'max_per_month':2})
    shift=make_shift('Greeter'); assignment=assign(person,shift)
    message=m.Message(volunteer_id=person.id,phone=person.phone,direction='in',body="I can't make the Sunday Greeter shift",
        kind='inbound',purpose='reply',status='received',created_at=clock.now())
    session.add(message);session.flush()
    session.info.update(sender_phone=person.phone,sender_schedule_action='cancel')
    ctx=FillContext(session,clock,provider,None,reply_to_message_id=message.id)
    return SimpleNamespace(person=person,shift=shift,assignment=assignment,message=message,ctx=ctx)


def cancel(example,session):
    row=refusal.record(example.ctx,example.person,example.assignment)
    example.assignment.status='cancelled';session.flush();return row


def sibling(session,example):
    shift=m.Shift(event_id=example.shift.event_id,role_id=example.shift.role_id,slot_index=2)
    session.add(shift);session.flush();return shift


def test_persisted_source_refusal_blocks_same_and_sibling_and_all_selection_consumers(example,session):
    row=cancel(example,session);session.commit();session.expire_all()
    peer=sibling(session,example)
    for shift in (example.shift,peer):
        assert not eligibility.check(session,example.person,shift)
        assert example.person.id not in [c.volunteer.id for c in ranking.rank_candidates(session,shift,NOW)]
        assert example.person.id not in [c.volunteer.id for c in scheduler.candidates(session,shift,NOW,'America/Denver')]
        assert scheduler.preview_problem(session,example.person,shift,[],'America/Denver')
    assert row.value['facts']['source_message_id']==example.message.id
    assert row.value['facts']['source_body_hash']==hashlib.sha256(example.message.body.encode()).hexdigest()
    assert 'phone' not in row.value['facts'] and 'body' not in row.value['facts']


def test_other_role_overlapping_interval_blocked_but_other_service_and_dates_allowed(example,session,make_shift):
    cancel(example,session)
    overlap=make_shift('Coffee',starts=example.shift.starts_at+timedelta(minutes=30))
    touching=make_shift('Coffee',starts=example.shift.ends_at)
    later=make_shift('Greeter',starts=example.shift.starts_at+timedelta(hours=2))
    next_week=make_shift('Greeter',starts=example.shift.starts_at+timedelta(days=7))
    assert not eligibility.check(session,example.person,overlap)
    assert eligibility.check(session,example.person,touching)
    assert eligibility.check(session,example.person,later)
    assert eligibility.check(session,example.person,next_week)
    assert session.scalar(select(m.Availability)) is None


def test_rescheduling_original_does_not_move_immutable_refusal(example,session,make_shift):
    row=cancel(example,session);original=row.value['facts']['starts_at']
    old=make_shift('Greeter',starts=example.shift.starts_at)
    example.shift.event.starts_at+=timedelta(days=7);example.shift.event.ends_at+=timedelta(days=7);session.flush()
    assert row.value['facts']['starts_at']==original
    assert not eligibility.check(session,example.person,old)
    assert eligibility.check(session,example.person,example.shift)


def test_plain_cancelled_assignment_and_planner_replan_do_not_create_absence(example,session,make_shift):
    session.info.pop('sender_schedule_action')
    assert refusal.record(example.ctx,example.person,example.assignment) is None
    example.assignment.status='cancelled';session.flush()
    assert eligibility.check(session,example.person,make_shift('Coffee'))
    assert session.scalar(select(m.Policy).where(m.Policy.key.startswith(refusal.PREFIX))) is None


@pytest.mark.parametrize('change',['foreign_person','foreign_phone','future','outgoing','not_received','foreign_kind','source_hash'])
def test_invalid_source_cannot_create_refusal(example,session,change):
    if change=='foreign_person':example.message.volunteer_id=None
    elif change=='foreign_phone':example.message.phone='+12025550199'
    elif change=='future':example.message.created_at=NOW+timedelta(seconds=1)
    elif change=='outgoing':example.message.direction='out'
    elif change=='not_received':example.message.status='draft'
    elif change=='foreign_kind':example.message.kind='google_voice_test_in'
    else:example.ctx.reply_to_message_id=None
    session.flush()
    if change=='source_hash':assert refusal.record(example.ctx,example.person,example.assignment) is None
    else:
        with pytest.raises(ValueError):refusal.record(example.ctx,example.person,example.assignment)
    assert session.scalar(select(m.Policy).where(m.Policy.key.startswith(refusal.PREFIX))) is None


@pytest.mark.parametrize('change',['body','kind','status','interval_hash','receipt_removed'])
def test_source_or_receipt_tamper_holds_overlapping_service(example,session,change):
    row=cancel(example,session)
    if change=='body':example.message.body='Different input'
    elif change=='kind':example.message.kind='draft'
    elif change=='status':example.message.status='superseded'
    elif change=='interval_hash':row.value={**row.value,'facts_hash':'0'*64}
    else:
        facts={**row.value['facts'],'source_guid':'missing','source_fingerprint':'0'*64}
        row.value={**row.value,'facts':facts,'facts_hash':refusal.digest(facts)}
    session.flush()
    assert 'source review' in refusal.problem(session,example.person,example.shift)


def test_idempotent_record_and_explicit_exact_reversal(example,session,make_shift):
    row=refusal.record(example.ctx,example.person,example.assignment)
    assert refusal.record(example.ctx,example.person,example.assignment) is row
    expected=refusal.digest(row.value)
    with pytest.raises(ValueError):refusal.revoke(session,row.key,expected=expected,actor='Admin',reason='Explicit corrected availability',now=NOW)
    session.info['record_authorized']=True
    with pytest.raises(ValueError):refusal.revoke(session,row.key,expected='bad',actor='Admin',reason='Explicit corrected availability',now=NOW)
    refusal.revoke(session,row.key,expected=expected,actor='Admin',reason='Explicit corrected availability for this interval',now=NOW)
    assert refusal.record(example.ctx,example.person,example.assignment) is row and row.value['state']=='revoked'
    example.assignment.status='cancelled';session.flush()
    assert eligibility.check(session,example.person,make_shift('Coffee'))


def test_normal_sender_cancellation_records_before_change_and_no_sender_reoffer(example,session):
    # Actual source permits a record in exact mode. No extra eligible people exist.
    session.info[confirmations.MODE_KEY]=True
    # A truthful prior acknowledgment prevents composition; no provider invocation.
    session.add(m.Notification(key='cancel:'+str(example.assignment.id),volunteer_id=example.person.id,
        purpose='cancellation_ack',body='',state='sent',due_at=NOW,created_at=NOW))
    session.flush();scope=snapshot(bookings(session,example.person,NOW))
    outcome=cancel_recorded_assignment(example.ctx,example.person,example.assignment.id,expected_scope=scope)
    assert outcome.action=='escalated' and example.assignment.status=='cancelled'
    row=session.get(m.Policy,refusal.PREFIX+str(example.person.id)+':'+str(example.assignment.id)+':'+str(example.message.id))
    assert row and not eligibility.check(session,example.person,example.shift)
    assert session.scalar(select(m.Message).where(m.Message.direction=='out')) is None


@pytest.mark.parametrize('review_mode', [False, True])
def test_actual_inbound_records_refusal_in_both_modes_and_restores_context(session,clock,provider,make_volunteer,make_shift,assign,review_mode):
    from app.core.inbound import handle_inbound
    from app.llm.parser import ParsedMessage
    person=make_volunteer(); shift=make_shift('Greeter'); assignment=assign(person,shift)
    session.info[confirmations.MODE_KEY]=review_mode
    session.info.update(sender_phone='outer-context',sender_schedule_action='accept')
    before=dict(session.info)
    session.add(m.Notification(key='cancel:'+str(assignment.id),volunteer_id=person.id,
        purpose='cancellation_ack',body='',state='sent',due_at=NOW,created_at=NOW))
    session.flush()
    ctx=FillContext(session,clock,provider,None)
    result=handle_inbound(session,clock,provider,person.phone,"I can't make the Sunday Greeter shift",
        lambda body:ParsedMessage(intent='cancel',confidence=.99,shift_hint='Sunday Greeter'),ctx)
    assert result.routed_to=='fill_agent' and assignment.status=='cancelled'
    row=session.get(m.Policy,refusal.PREFIX+str(person.id)+':'+str(assignment.id)+':'+str(ctx.reply_to_message_id))
    assert row and row.value['facts']['source_message_id']==ctx.reply_to_message_id
    assert not eligibility.check(session,person,shift)
    assert session.info==before and not provider.sent


def test_inbound_evidence_cleanup_even_when_final_flush_fails(session,clock,provider,monkeypatch):
    import app.core.inbound as inbound
    session.info.update(sender_phone='outer-context',sender_schedule_action='accept')
    before=dict(session.info)
    def route(*args,**kwargs):
        assert session.info['sender_schedule_action']=='cancel'
    def failed_flush():
        raise RuntimeError('fictional flush failure')
    monkeypatch.setattr(inbound,'_handle_inbound',route)
    monkeypatch.setattr(session,'flush',failed_flush)
    with pytest.raises(RuntimeError,match='fictional flush failure'):
        inbound.handle_inbound(session,clock,provider,'+12025550142','Cancel my shift',lambda body:None)
    assert session.info==before


def backfill_fixture(example,session):
    original=snapshot([example.assignment]);example.assignment.status='cancelled';example.assignment.updated_at=NOW
    session.add(m.FillRequest(shift_id=example.shift.id,cancelled_assignment_id=example.assignment.id,
        urgency='normal',state='escalated',current_tranche=0,created_at=NOW))
    session.add(m.Notification(key='cancellation-scope:'+str(example.person.id),volunteer_id=example.person.id,
        purpose='cancellation_scope',body='',state='resolved',due_at=NOW,created_at=NOW,
        detail={'phone':example.person.phone,'session_id':None,'source_message_id':example.message.id,
            'source_body_hash':hashlib.sha256(example.message.body.encode()).hexdigest(),
            'assignment_id':example.assignment.id,'resolved_message_id':example.message.id,'bookings':original}))
    session.flush()


def test_exact_backfill_is_idempotent_preserves_history_and_never_replays(example,session):
    backfill_fixture(example,session)
    before_messages=list(session.scalars(select(m.Message.id)));before_fills=list(session.scalars(select(m.FillRequest.id)))
    row=refusal.backfill(example.ctx,example.person,example.assignment.id,example.message.id)
    assert refusal.backfill(example.ctx,example.person,example.assignment.id,example.message.id) is row
    assert not eligibility.check(session,example.person,example.shift)
    assert list(session.scalars(select(m.Message.id)))==before_messages and list(session.scalars(select(m.FillRequest.id)))==before_fills


@pytest.mark.parametrize('change',['missing_fill','wrong_input','bad_body_hash','missing_interval','unresolved','foreign_person'])
def test_backfill_needs_original_source_booking_and_fill(example,session,change):
    backfill_fixture(example,session)
    hold=session.get(m.Notification,'cancellation-scope:'+str(example.person.id))
    if change=='missing_fill':session.delete(session.scalar(select(m.FillRequest)))
    elif change=='wrong_input':hold.detail={**hold.detail,'resolved_message_id':999}
    elif change=='bad_body_hash':hold.detail={**hold.detail,'source_body_hash':'0'*64}
    elif change=='missing_interval':hold.detail={**hold.detail,'bookings':[]}
    elif change=='unresolved':hold.state='pending'
    else:hold.volunteer_id=None
    session.flush()
    with pytest.raises(ValueError):refusal.backfill(example.ctx,example.person,example.assignment.id,example.message.id)


def test_mac_source_requires_exact_recorded_receipt(example,session,clock):
    from app.integrations.test_sessions import TestSession
    selected=TestSession('a'*32,NOW-timedelta(minutes=5),NOW+timedelta(hours=1))
    provider=SimpleNamespace(test_sessions={example.person.phone:selected},allows=lambda phone:phone==example.person.phone,transport_name='mac_messages')
    example.ctx.provider=provider;session.info['mac_test_session']=selected
    example.message.kind='mac_test_in';example.message.purpose='test:'+selected.id;session.flush()
    with pytest.raises(ValueError):refusal.record(example.ctx,example.person,example.assignment)
    fingerprint=hashlib.sha256((example.person.phone+'\0iMessage\0'+selected.id+'\0'+example.message.body).encode()).hexdigest()
    session.add(MacInboundReceipt(guid='fictional-cancellation-guid',fingerprint=fingerprint,result={}));session.flush()
    row=refusal.record(example.ctx,example.person,example.assignment)
    assert row.value['facts']['source_guid']=='fictional-cancellation-guid'


def test_state_flip_without_exact_reversal_does_not_clear_refusal(example,session):
    row=cancel(example,session);row.value={**row.value,'state':'revoked'};session.flush()
    assert 'source review' in refusal.problem(session,example.person,example.shift)


def test_new_actual_cancellation_after_reversal_preserves_history_and_blocks_again(example,session):
    row=refusal.record(example.ctx,example.person,example.assignment)
    session.info['record_authorized']=True
    refusal.revoke(session,row.key,expected=refusal.digest(row.value),actor='Coordinator',reason='Explicit same service availability correction',now=NOW)
    next_input=m.Message(volunteer_id=example.person.id,phone=example.person.phone,direction='in',body='I still cannot attend that service',kind='inbound',purpose='reply',status='received',created_at=NOW)
    session.add(next_input);session.flush();example.ctx.reply_to_message_id=next_input.id
    fresh=refusal.record(example.ctx,example.person,example.assignment)
    assert fresh.key!=row.key and row.value['state']=='revoked' and fresh.value['state']=='active'
    assert refusal.problem(session,example.person,example.shift)=='Sender cancelled this service interval'


def test_existing_record_proposal_cannot_rebook_cancelled_service(example,session):
    row=refusal.record(example.ctx,example.person,example.assignment)
    example.assignment.status='cancelled';session.flush()
    proposal=confirmations.stage(session,NOW,{'action':'record_change','record':'Assignment','record_id':None,'before':None,
        'after':{'shift_id':example.shift.id,'volunteer_id':example.person.id,'status':'approved','source':'planner'}},record=True)
    with pytest.raises(ValueError,match='no longer eligible'):
        confirmations.decide(session,None,proposal,approve=True,actor='Coordinator',expected=proposal.payload['content_hash'],now=NOW)
    assert row.value['state']=='active'


def test_mac_receipt_session_tamper_holds_original_interval(example,session):
    from app.integrations.test_sessions import TestSession
    selected=TestSession('a'*32,NOW-timedelta(minutes=5),NOW+timedelta(hours=1))
    example.ctx.provider=SimpleNamespace(test_sessions={example.person.phone:selected},allows=lambda phone:phone==example.person.phone,transport_name='mac_messages')
    session.info['mac_test_session']=selected;example.message.kind='mac_test_in';example.message.purpose='test:'+selected.id;session.flush()
    fingerprint=hashlib.sha256((example.person.phone+'\0SMS\0'+selected.id+'\0'+example.message.body).encode()).hexdigest()
    receipt=MacInboundReceipt(guid='fictional-recorded-guid',fingerprint=fingerprint,result={'session_id':selected.id})
    session.add(receipt);session.flush();row=refusal.record(example.ctx,example.person,example.assignment)
    assert row.value['facts']['source_session_id']==selected.id
    receipt.result={'session_id':'b'*32};session.flush()
    assert 'source review' in refusal.problem(session,example.person,example.shift)
