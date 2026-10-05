"""Automatic admin summaries use existing roster and fake delivery only."""
from datetime import timedelta
import json
from types import SimpleNamespace

import pytest
from sqlalchemy import select

from app.agents.fill_agent import FillContext
from app.core.notifications import flush_due, queue_pre_event_updates, pre_event_delivery_problem
from app.db import models as m
from app.jobs import process_due_fill_requests
from app.config import Settings
from app.llm.gloo_client import GlooUnavailableError


class SyntheticGloo:
    settings = Settings()
    def __init__(self):
        self.calls = []
    def create_response(self, **kwargs):
        self.calls.append(kwargs)
        return SimpleNamespace(output_text=json.loads(kwargs['input'])['approved_message'])


def context(session, clock, provider):
    return FillContext(session, clock, provider, SyntheticGloo())


def test_tick_sends_even_without_staffing_changes_and_dedupes_after_restart(
    session, clock, provider, make_volunteer, make_shift, assign
):
    admin = make_volunteer(coordinator=True)
    shift = make_shift(starts=clock.now()+timedelta(hours=3, seconds=1))
    assign(make_volunteer(), shift, status='confirmed')
    ctx = context(session, clock, provider)
    process_due_fill_requests(ctx)
    assert not provider.sent
    clock.advance(timedelta(seconds=1))
    process_due_fill_requests(ctx)
    session.commit()
    session.expire_all()
    process_due_fill_requests(context(session, clock, provider))
    assert len(provider.sent_to(admin.phone)) == 1
    body = provider.sent[0].body
    assert 'All set:' in body and 'No action needed' in body and '1 required spots' in body
    assert len(ctx.gloo.calls) == 1


def test_gap_and_exact_approval_are_clear(session, clock, provider, make_volunteer, make_shift):
    make_volunteer(coordinator=True)
    shift = make_shift('Kids check-in', starts=clock.now()+timedelta(hours=2))
    fill = m.FillRequest(shift_id=shift.id, state='waiting_approval', urgency='high', created_at=clock.now())
    session.add(fill); session.flush()
    session.add(m.Approval(kind='send_outreach', payload={'fill_request_id':fill.id},
                           status='pending', requested_at=clock.now()))
    process_due_fill_requests(context(session, clock, provider))
    body = provider.sent[0].body
    assert '0/1' in body and 'Kids check-in (1)' in body and 'Reply YES A' in body
    assert 'No action needed' not in body


def test_missing_plan_is_not_all_set(session, clock, provider, make_volunteer):
    make_volunteer(coordinator=True)
    session.add(m.Event(title='No staffing plan', starts_at=clock.now()+timedelta(hours=2),
                        ends_at=clock.now()+timedelta(hours=3), status='scheduled'))
    process_due_fill_requests(context(session, clock, provider))
    assert 'No required staffing plan' in provider.sent[0].body
    assert 'All set' not in provider.sent[0].body


@pytest.mark.parametrize('change', ['cancelled', 'completed', 'started', 'rescheduled', 'recipient'])
def test_pending_update_expires_when_event_or_admin_changes(
    session, clock, provider, make_volunteer, make_shift, change
):
    admin = make_volunteer(coordinator=True)
    event = make_shift(starts=clock.now()+timedelta(hours=2)).event
    ctx = context(session, clock, provider)
    queue_pre_event_updates(ctx)
    row = session.scalar(select(m.Notification))
    if change in ('cancelled', 'completed'):
        event.status = change
    elif change == 'started':
        clock.set_time(event.starts_at)
    elif change == 'recipient':
        admin.is_coordinator = False
    else:
        event.starts_at += timedelta(days=1)
    assert pre_event_delivery_problem(session, row, clock.now())
    flush_due(ctx)
    assert not provider.sent and row.state == 'expired'


def test_quiet_hours_retry_uses_fresh_staffing(session, clock, provider, make_volunteer, make_shift, assign):
    make_volunteer(coordinator=True)
    clock.set_time(clock.now().replace(hour=6))
    shift = make_shift(starts=clock.now()+timedelta(hours=3))
    ctx = context(session, clock, provider)
    process_due_fill_requests(ctx)
    row = session.scalar(select(m.Notification))
    assert row.state == 'pending' and not provider.sent
    assign(make_volunteer(), shift, status='confirmed')
    clock.set_time(clock.now().replace(hour=7))
    process_due_fill_requests(ctx)
    assert 'All set' in provider.sent[0].body


def test_opted_out_and_inactive_admins_are_not_contacted(session, clock, provider, make_volunteer, make_shift):
    make_volunteer(coordinator=True, opt_in=False)
    make_volunteer(coordinator=True, status='inactive')
    make_shift(starts=clock.now()+timedelta(hours=2))
    process_due_fill_requests(context(session, clock, provider))
    assert not provider.sent


def test_exact_review_mode_is_preserved_without_repeated_approvals(session, clock, provider, make_volunteer, make_shift):
    from app.core import confirmations
    admin = make_volunteer(coordinator=True)
    event = make_shift(starts=clock.now()+timedelta(hours=2)).event
    # A scheduler reads committed UTC event records in a fresh session.
    session.commit()
    session.expire_all()
    session.info['competition_confirmation_required'] = True
    ctx = context(session, clock, provider)
    process_due_fill_requests(ctx)
    row = session.scalar(select(m.Notification).where(m.Notification.key.startswith('pre-event:')))
    proposal = session.scalar(select(m.Approval).where(m.Approval.kind == 'confirm_text'))
    assert row.state == 'awaiting_approval'
    assert row.detail['approval_id'] == proposal.id and proposal.status == 'pending'
    assert row.event_id == event.id and row.detail['event_start'] == event.starts_at.isoformat()
    assert proposal.payload['phone'] == admin.phone and proposal.payload['volunteer_id'] == admin.id
    assert proposal.payload['body'] == row.body and proposal.payload['purpose'] == 'coordinator_notify'
    assert proposal.payload['kind'] == 'ai' and proposal.payload['urgent'] is True
    assert confirmations.valid(proposal, clock.now())
    reviewed_hash = proposal.payload['content_hash']
    reviewed_body = proposal.payload['body']
    session.commit()
    session.expire_all()
    process_due_fill_requests(ctx)
    assert not provider.sent
    assert session.scalar(select(m.Message)) is None
    assert len(session.scalars(select(m.Approval).where(m.Approval.kind=='confirm_text')).all()) == 1
    assert len(ctx.gloo.calls) == 1 and proposal.payload['content_hash'] == reviewed_hash
    assert row.state == 'awaiting_approval' and row.detail['approval_id'] == proposal.id
    confirmations.decide(session, ctx.gate, proposal, approve=True, actor='fictional-admin@example.test',
                         expected=reviewed_hash, now=clock.now())
    assert proposal.status == 'approved' and proposal.payload['content_hash'] == reviewed_hash
    assert len(provider.sent) == 1 and provider.sent[0].to == admin.phone
    assert provider.sent[0].body == reviewed_body and len(ctx.gloo.calls) == 1
    proof = session.get(m.Notification, f"confirmation:{proposal.payload['message_id']}")
    assert proof.detail == {'approval_id': proposal.id, 'content_hash': reviewed_hash}
    process_due_fill_requests(ctx)
    assert len(provider.sent) == 1 and len(ctx.gloo.calls) == 1


def test_gloo_outage_never_sends_a_template_fallback(session, clock, provider, make_volunteer, make_shift):
    make_volunteer(coordinator=True)
    make_shift(starts=clock.now()+timedelta(hours=2))
    class UnavailableGloo(SyntheticGloo):
        def create_response(self, **kwargs):
            raise GlooUnavailableError('Synthetic outage')
    ctx = FillContext(session, clock, provider, UnavailableGloo())
    for attempt in range(3):
        process_due_fill_requests(ctx)
        clock.advance(timedelta(minutes=2))
    assert not provider.sent
    assert session.scalar(select(m.Notification)).state == 'blocked'


@pytest.mark.parametrize('review_minute, expected_delivery', [(0, False), (30, True)])
def test_pre_event_review_still_enforces_urgent_quiet_hours(
    session, clock, provider, make_volunteer, make_shift, review_minute, expected_delivery
):
    from app.core import confirmations
    clock.set_time(clock.now().replace(day=4, hour=6))  # Sunday 06:00, event at 09:00.
    admin = make_volunteer(coordinator=True)
    make_shift(starts=clock.now()+timedelta(hours=3))
    session.commit()
    session.expire_all()
    session.info['competition_confirmation_required'] = True
    ctx = context(session, clock, provider)
    process_due_fill_requests(ctx)
    row = session.scalar(select(m.Notification).where(m.Notification.key.startswith('pre-event:')))
    proposal = session.get(m.Approval, row.detail['approval_id'])
    assert row.state == 'awaiting_approval' and proposal.status == 'pending'
    assert not provider.sent and proposal.payload['phone'] == admin.phone
    original_body, original_hash = proposal.payload['body'], proposal.payload['content_hash']
    clock.advance(timedelta(minutes=review_minute))
    confirmations.decide(session, ctx.gate, proposal, approve=True, actor='fictional-admin@example.test',
                         expected=original_hash, now=clock.now())
    assert bool(provider.sent) is expected_delivery
    assert proposal.status == ('approved' if expected_delivery else 'expired')
    assert proposal.payload['body'] == original_body and proposal.payload['content_hash'] == original_hash
    if expected_delivery:
        assert provider.sent[0].body == original_body
    # A quiet-hour rejection cannot release later or silently create another review.
    clock.set_time(clock.now().replace(hour=7, minute=0))
    process_due_fill_requests(ctx)
    assert len(provider.sent) == int(expected_delivery) and len(ctx.gloo.calls) == 1
    assert len(session.scalars(select(m.Approval).where(m.Approval.kind == 'confirm_text')).all()) == 1


@pytest.mark.parametrize('restriction', ['opt_out', 'sensitive', 'inactive', 'transport'])
def test_pre_event_real_blockers_do_not_become_pending_reviews(
    session, clock, provider, make_volunteer, make_shift, monkeypatch, restriction
):
    admin = make_volunteer(coordinator=True)
    make_shift(starts=clock.now()+timedelta(hours=3))
    session.commit()
    session.expire_all()
    session.info['competition_confirmation_required'] = True
    ctx = context(session, clock, provider)
    queue_pre_event_updates(ctx)
    row = session.scalar(select(m.Notification).where(m.Notification.key.startswith('pre-event:')))
    # Install already-authorized saved restrictions, rather than staging record edits.
    session.info['record_authorized'] = True
    if restriction == 'opt_out':
        admin.sms_opt_in = False
    elif restriction == 'sensitive':
        session.add(m.Escalation(category='sensitive', severity='normal', summary='Fictional internal hold',
                               related_ids={'volunteer_id': admin.id}, status='open', created_at=clock.now()))
    elif restriction == 'inactive':
        admin.status = 'inactive'
    else:
        monkeypatch.setattr(provider, 'allows', lambda _phone: False, raising=False)
    session.flush()
    session.info.pop('record_authorized')
    flush_due(ctx)
    assert row.state in {'blocked', 'expired'}
    assert 'approval_id' not in row.detail and not provider.sent
    assert session.scalar(select(m.Approval)) is None and session.scalar(select(m.Message)) is None


@pytest.mark.parametrize('status, reason, expected_state', [
    ('blocked_transport', 'uncertain transport; no automatic retry', 'blocked'),
    ('blocked_policy', 'This conversation notification is already reserved', 'blocked_policy'),
])
def test_pre_event_nonapproval_gate_receipts_stay_blocked(
    session, clock, provider, make_volunteer, make_shift, monkeypatch, status, reason, expected_state
):
    from app.core.send_gate import SendGate, SendOutcome, SendStatus
    make_volunteer(coordinator=True)
    make_shift(starts=clock.now()+timedelta(hours=3))
    session.commit()
    session.expire_all()
    session.info['competition_confirmation_required'] = True
    monkeypatch.setattr(SendGate, 'send', lambda *a, **kw: SendOutcome(SendStatus(status), reason=reason))
    ctx = context(session, clock, provider)
    process_due_fill_requests(ctx)
    row = session.scalar(select(m.Notification).where(m.Notification.key.startswith('pre-event:')))
    assert row.state == expected_state and row.detail['reason'] == reason
    assert 'approval_id' not in row.detail and not provider.sent
    assert session.scalar(select(m.Approval)) is None
    process_due_fill_requests(ctx)
    assert len(ctx.gloo.calls) == 1 and not provider.sent
