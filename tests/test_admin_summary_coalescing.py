"""Same-event admin update regressions, synthetic roster and delivery only."""
from datetime import timedelta
import json
import pytest
from sqlalchemy import select
from app.agents.fill_agent import FillContext
from app.core import confirmations, notifications
from app.db import models as m
from app.jobs import process_due_fill_requests
from app.llm.gloo_client import GlooUnavailableError
from tests.test_pre_event_updates import SyntheticGloo


def setup(session, clock, provider, make_volunteer, make_shift, assign, exact=False):
    clock.set_time(clock.now().replace(day=3, hour=22))
    admin = make_volunteer('Fictional Coordinator', coordinator=True)
    shift = make_shift('Greeter', starts=clock.now().replace(day=4, hour=9))
    assign(make_volunteer('Fictional Helper'), shift, status='confirmed')
    ctx = FillContext(session, clock, provider, SyntheticGloo())
    notifications.queue_staffing(ctx, shift.event)
    digest = session.scalar(select(m.Notification).where(m.Notification.key.startswith('staffing:')))
    assert digest.due_at < shift.starts_at - timedelta(hours=3)
    clock.set_time(clock.now().replace(day=4, hour=6))
    session.info[confirmations.MODE_KEY] = exact
    return ctx, admin, shift, digest


@pytest.mark.parametrize('exact', [False, True], ids=['automatic', 'exact_review'])
def test_quiet_release_coalesces_older_digest_before_any_send_or_second_review(
    session, clock, provider, make_volunteer, make_shift, assign, exact
):
    ctx, admin, shift, digest = setup(session, clock, provider, make_volunteer, make_shift, assign, exact)
    process_due_fill_requests(ctx)
    assert not provider.sent
    clock.set_time(clock.now().replace(hour=7))
    process_due_fill_requests(ctx)
    pre = session.scalar(select(m.Notification).where(m.Notification.key.startswith('pre-event:')))
    if exact:
        reviews = session.scalars(select(m.Approval).where(m.Approval.kind == 'confirm_text')).all()
        assert len(reviews) == 1
        review = reviews[0]
        assert pre.state == 'awaiting_approval' and review.payload['pre_event_source']['notification_key'] == pre.key
        body, digest_hash = review.payload['body'], review.payload['content_hash']
        confirmations.decide(session, ctx.gate, review, approve=True, actor='fictional-admin@example.test',
            expected=digest_hash, now=clock.now())
        assert review.payload['body'] == body and review.payload['content_hash'] == digest_hash
    assert len(provider.sent) == 1 and provider.sent[0].to == admin.phone
    assert provider.sent[0].body.startswith('Pre-event update: All set:')
    assert '1 required spots' in provider.sent[0].body and '\u2014' not in provider.sent[0].body
    assert pre.state == 'sent' and pre.event_id == shift.event_id and digest.state == 'unchanged'
    assert digest.detail['last_snapshot'] == pre.detail['pending_snapshot']
    calls = len(ctx.gloo.calls)
    assert all(json.loads(call['input'])['approved_message'].startswith('Pre-event update:') for call in ctx.gloo.calls)
    session.commit(); session.expire_all()
    process_due_fill_requests(ctx)
    assert len(provider.sent) == 1 and len(ctx.gloo.calls) == calls


def test_pre_event_composition_retry_keeps_sibling_digest_pending_without_fallback(
    session, clock, provider, make_volunteer, make_shift, assign
):
    ctx, _, _, digest = setup(session, clock, provider, make_volunteer, make_shift, assign)
    clock.set_time(clock.now().replace(hour=7))
    class OnceUnavailable(SyntheticGloo):
        unavailable = True
        def create_response(self, **kwargs):
            if self.unavailable:
                self.calls.append(kwargs)
                assert json.loads(kwargs['input'])['approved_message'].startswith('Pre-event update:')
                raise GlooUnavailableError('Synthetic temporary outage')
            return super().create_response(**kwargs)
    ctx.gloo = OnceUnavailable()
    process_due_fill_requests(ctx)
    assert not provider.sent and digest.state == 'pending'
    assert len(ctx.gloo.calls) == 1
    ctx.gloo.unavailable = False
    clock.advance(timedelta(minutes=2))
    process_due_fill_requests(ctx)
    assert len(provider.sent) == 1 and provider.sent[0].body.startswith('Pre-event update:')
    assert digest.state == 'unchanged' and len(ctx.gloo.calls) == 2


def test_pending_review_holds_digest_until_rejected_without_rewriting_copy(
    session, clock, provider, make_volunteer, make_shift, assign
):
    ctx, _, _, digest = setup(session, clock, provider, make_volunteer, make_shift, assign, exact=True)
    clock.set_time(clock.now().replace(hour=7))
    process_due_fill_requests(ctx)
    pre = session.scalar(select(m.Notification).where(m.Notification.key.startswith('pre-event:')))
    review = session.get(m.Approval, pre.detail['approval_id'])
    payload = dict(review.payload)
    clock.advance(timedelta(minutes=20))
    process_due_fill_requests(ctx)
    assert not provider.sent and len(ctx.gloo.calls) == 1 and digest.state == 'pending'
    assert review.payload == payload
    confirmations.decide(session, ctx.gate, review, approve=False, actor='fictional-admin@example.test',
                         expected=payload['content_hash'], now=clock.now())
    session.info[confirmations.MODE_KEY] = False
    clock.advance(timedelta(minutes=2))
    process_due_fill_requests(ctx)
    assert len(provider.sent) == 1 and provider.sent[0].body.startswith('Fully staffed:')
    assert review.payload == payload and review.status == 'rejected'


@pytest.mark.parametrize('stale', ['event_start', 'expired_review', 'missing_review', 'expired_notice'])
def test_stale_pre_event_cannot_hold_digest_indefinitely(
    session, clock, provider, make_volunteer, make_shift, assign, stale
):
    ctx, _, shift, digest = setup(session, clock, provider, make_volunteer, make_shift, assign, exact=True)
    clock.set_time(clock.now().replace(hour=7))
    process_due_fill_requests(ctx)
    pre = session.scalar(select(m.Notification).where(m.Notification.key.startswith('pre-event:')))
    review = session.get(m.Approval, pre.detail['approval_id'])
    if stale == 'event_start':
        shift.event.starts_at += timedelta(hours=1)
    elif stale == 'expired_review':
        review.payload = {**review.payload, 'expires_at': clock.now().isoformat()}
        review.payload = {**review.payload, 'content_hash': confirmations.digest(review.payload)}
    elif stale == 'missing_review':
        pre.detail = {**pre.detail, 'approval_id': None}
    else:
        pre.expires_at = clock.now()
    session.info[confirmations.MODE_KEY] = False
    clock.advance(timedelta(minutes=2))
    # Exercise existing notices, without creating a replacement for a new start.
    notifications.flush_due(ctx)
    assert len(provider.sent) == 1 and provider.sent[0].body.startswith('Fully staffed:')
    assert digest.state == 'sent'


def test_preexisting_unsent_staffing_review_expires_only_after_pre_event_queues(
    session, clock, provider, make_volunteer, make_shift, assign
):
    ctx, _, _, digest = setup(session, clock, provider, make_volunteer, make_shift, assign, exact=True)
    clock.set_time(clock.now().replace(day=3, hour=19))
    digest.due_at = clock.now()
    notifications.flush_due(ctx)
    old = session.get(m.Approval, digest.detail['approval_id'])
    payload = dict(old.payload)
    clock.set_time(clock.now().replace(day=4, hour=7))
    process_due_fill_requests(ctx)
    pre = session.scalar(select(m.Notification).where(m.Notification.key.startswith('pre-event:')))
    assert old.status == 'pending' and old.payload == payload and not provider.sent
    review = session.get(m.Approval, pre.detail['approval_id'])
    confirmations.decide(session, ctx.gate, review, approve=True, actor='fictional-admin@example.test',
                         expected=review.payload['content_hash'], now=clock.now())
    assert len(provider.sent) == 1 and digest.state == 'unchanged'
    assert old.status == 'expired' and old.payload == payload


@pytest.mark.parametrize('status', ['queued', 'dispatching', 'submitted', 'uncertain'])
@pytest.mark.parametrize('link', ['notification', 'review'])
def test_coalescing_preserves_sibling_already_in_delivery(
    session, clock, provider, make_volunteer, make_shift, assign, status, link
):
    ctx, admin, _, digest = setup(session, clock, provider, make_volunteer, make_shift, assign)
    notifications.queue_pre_event_updates(ctx)
    pre = session.scalar(select(m.Notification).where(m.Notification.key.startswith('pre-event:')))
    pre.detail = {**pre.detail, 'pending_snapshot': {'covered': 1, 'required': 1}}
    message = m.Message(direction='out', volunteer_id=admin.id, phone=admin.phone,
                        body='Fictional reviewed summary.', kind='ai', purpose='coordinator_notify',
                        status=status, created_at=clock.now())
    session.add(message); session.flush()
    digest.state = 'sent'
    review = confirmations.stage(session, clock.now(), {'action': 'send_text', 'body': message.body,
        'phone': admin.phone, 'volunteer_id': admin.id, 'purpose': 'coordinator_notify'})
    review.status = 'approved'
    if link == 'notification':
        digest.message_id = message.id
    else:
        review.payload = {**review.payload, 'message_id': message.id}
    digest.detail = {**digest.detail, 'approval_id': review.id}
    saved_detail, saved_payload = dict(digest.detail), dict(review.payload)
    notifications.coalesce_pre_event_digest(session, pre, clock.now())
    assert digest.state == 'sent' and digest.detail == saved_detail
    assert review.status == 'approved' and review.payload == saved_payload and message.status == status


def test_reused_digest_with_completed_receipt_still_coalesces_without_changing_history(
    session, clock, provider, make_volunteer, make_shift, assign
):
    ctx, admin, shift, digest = setup(session, clock, provider, make_volunteer, make_shift, assign)
    clock.set_time(clock.now().replace(day=3, hour=19))
    digest.due_at = clock.now()
    notifications.flush_due(ctx)
    previous_id = digest.message_id
    previous = session.get(m.Message, previous_id)
    previous_body = previous.body
    notifications.queue_staffing(ctx, shift.event)
    clock.set_time(clock.now().replace(day=4, hour=7))
    process_due_fill_requests(ctx)
    assert len(provider.sent) == 2 and provider.sent[-1].body.startswith('Pre-event update:')
    assert digest.state == 'unchanged' and digest.message_id == previous_id
    assert previous.status == 'sent' and previous.body == previous_body and previous.phone == admin.phone
    clock.advance(timedelta(minutes=15))
    process_due_fill_requests(ctx)
    assert len(provider.sent) == 2


def test_current_unsent_review_expires_despite_historical_digest_receipt(
    session, clock, provider, make_volunteer, make_shift, assign
):
    ctx, _, shift, digest = setup(session, clock, provider, make_volunteer, make_shift, assign)
    clock.set_time(clock.now().replace(day=3, hour=19))
    digest.due_at = clock.now()
    notifications.flush_due(ctx)
    historical_id = digest.message_id
    historical = session.get(m.Message, historical_id)
    historical_body = historical.body
    session.scalar(select(m.Assignment).where(m.Assignment.shift_id == shift.id)).status = 'cancelled'
    notifications.queue_staffing(ctx, shift.event)
    session.info[confirmations.MODE_KEY] = True
    clock.set_time(clock.now().replace(day=4, hour=7))
    notifications.flush_due(ctx)
    current = session.get(m.Approval, digest.detail['approval_id'])
    assert current.status == 'pending' and current.payload.get('message_id') is None
    assert digest.message_id == historical_id and historical.status == 'sent'
    payload = dict(current.payload)
    process_due_fill_requests(ctx)
    pre = session.scalar(select(m.Notification).where(m.Notification.key.startswith('pre-event:')))
    ready = session.get(m.Approval, pre.detail['approval_id'])
    assert current.status == 'pending' and current.payload == payload
    confirmations.decide(session, ctx.gate, ready, approve=True, actor='fictional-admin@example.test',
                         expected=ready.payload['content_hash'], now=clock.now())
    assert digest.state == 'unchanged' and current.status == 'expired'
    assert len(provider.sent) == 2 and current.payload == payload
    with pytest.raises(ValueError, match='already reviewed'):
        confirmations.decide(session, ctx.gate, current, approve=True, actor='fictional-admin@example.test',
                             expected=payload['content_hash'], now=clock.now())
    assert len(provider.sent) == 2
    assert historical.status == 'sent' and historical.body == historical_body
    assert digest.message_id == historical_id


@pytest.mark.parametrize('status', ['queued', 'dispatching', 'submitted', 'uncertain', 'sent', 'delivered'])
def test_linked_current_review_preserves_delivery_and_historical_digest_receipt(
    session, clock, provider, make_volunteer, make_shift, assign, status
):
    ctx, admin, _, digest = setup(session, clock, provider, make_volunteer, make_shift, assign)
    notifications.queue_pre_event_updates(ctx)
    pre = session.scalar(select(m.Notification).where(m.Notification.key.startswith('pre-event:')))
    pre.detail = {**pre.detail, 'pending_snapshot': {'covered': 1, 'required': 1}}
    historical = m.Message(direction='out', volunteer_id=admin.id, phone=admin.phone,
        body='Historical fictional summary.', kind='ai', purpose='coordinator_notify',
        status='sent', created_at=clock.now()-timedelta(hours=1))
    current_message = m.Message(direction='out', volunteer_id=admin.id, phone=admin.phone,
        body='Current fictional reviewed summary.', kind='ai', purpose='coordinator_notify',
        status=status, created_at=clock.now())
    session.add_all([historical, current_message]); session.flush()
    review = confirmations.stage(session, clock.now(), {'action': 'send_text', 'body': current_message.body,
        'phone': admin.phone, 'volunteer_id': admin.id, 'purpose': 'coordinator_notify'})
    review.status = 'approved'
    review.payload = {**review.payload, 'message_id': current_message.id}
    digest.message_id, digest.state = historical.id, 'awaiting_approval'
    digest.detail = {**digest.detail, 'approval_id': review.id}
    saved_detail, saved_payload = dict(digest.detail), dict(review.payload)
    notifications.coalesce_pre_event_digest(session, pre, clock.now())
    assert review.status == 'approved' and review.payload == saved_payload
    assert historical.status == 'sent' and digest.message_id == historical.id
    assert current_message.status == status and current_message.body == saved_payload['body']
    if status in {'sent', 'delivered'}:
        assert digest.state == 'unchanged'
    else:
        assert digest.state == 'awaiting_approval' and digest.detail == saved_detail


def test_approved_legacy_review_without_own_link_keeps_completed_receipt_and_copy(
    session, clock, provider, make_volunteer, make_shift, assign
):
    ctx, admin, _, digest = setup(session, clock, provider, make_volunteer, make_shift, assign)
    notifications.queue_pre_event_updates(ctx)
    pre = session.scalar(select(m.Notification).where(m.Notification.key.startswith('pre-event:')))
    pre.detail = {**pre.detail, 'pending_snapshot': {'covered': 1, 'required': 1}}
    message = m.Message(direction='out', volunteer_id=admin.id, phone=admin.phone,
        body='Completed fictional reviewed summary.', kind='ai', purpose='coordinator_notify',
        status='sent', created_at=clock.now())
    session.add(message); session.flush()
    review = confirmations.stage(session, clock.now(), {'action': 'send_text', 'body': message.body,
        'phone': admin.phone, 'volunteer_id': admin.id, 'purpose': 'coordinator_notify'})
    review.status = 'approved'
    digest.message_id, digest.state = message.id, 'sent'
    digest.detail = {**digest.detail, 'approval_id': review.id}
    saved_payload = dict(review.payload)
    notifications.coalesce_pre_event_digest(session, pre, clock.now())
    assert digest.state == 'unchanged' and digest.message_id == message.id
    assert review.status == 'approved' and review.payload == saved_payload
    assert message.status == 'sent' and message.body == saved_payload['body']


def test_coalescing_matches_both_event_and_admin_and_other_digests_still_send(
    session, clock, provider, make_volunteer, make_shift, assign
):
    ctx, first_admin, first_shift, digest = setup(session, clock, provider, make_volunteer, make_shift, assign)
    other_admin = make_volunteer('Other Fictional Coordinator', coordinator=True)
    other_shift = make_shift('Usher', starts=first_shift.starts_at + timedelta(hours=1), title='Other Fictional Event')
    assign(make_volunteer('Other Fictional Helper'), other_shift, status='confirmed')
    notifications.queue_staffing(ctx, first_shift.event)
    notifications.queue_staffing(ctx, other_shift.event)
    clock.set_time(clock.now().replace(hour=7))
    notifications.queue_pre_event_updates(ctx)
    pre = session.scalar(select(m.Notification).where(m.Notification.key.startswith('pre-event:'),
        m.Notification.event_id == first_shift.event_id, m.Notification.volunteer_id == first_admin.id))
    notifications._dispatch(ctx, pre)
    assert digest.state == 'unchanged'
    others = session.scalars(select(m.Notification).where(m.Notification.key.startswith('staffing:'),
                                                       m.Notification.key != digest.key)).all()
    assert len(others) == 3 and all(row.state == 'pending' for row in others)
    # End the other notices' holds to check that unrelated digests remain usable.
    for notice in session.scalars(select(m.Notification).where(m.Notification.key.startswith('pre-event:'),
                                                             m.Notification.key != pre.key)):
        notice.state = 'blocked'
    clock.advance(timedelta(minutes=15))
    notifications.flush_due(ctx)
    clock.advance(timedelta(minutes=15))
    notifications.flush_due(ctx)
    assert len(provider.sent) == 4 and all(row.state == 'sent' for row in others)
    assert sum(message.to == other_admin.phone for message in provider.sent) == 2
