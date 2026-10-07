"""Stateful feature-universe scheduling probes, synthetic records only."""
from datetime import timedelta
import itertools
import pytest

from app.core import ranking, scheduler, clyde_algorithm as algo
from app.core.send_gate import UNSENT_STATUSES
from app.db import models as m


@pytest.mark.parametrize('status', UNSENT_STATUSES)
def test_unsent_reviews_do_not_penalize_fairness(session, clock, make_volunteer, make_shift, status):
    shift = make_shift()
    first = make_volunteer()
    second = make_volunteer()
    session.add(m.Message(direction='out', volunteer_id=first.id, phone=first.phone,
        body='Synthetic held invitation', kind='ai', purpose='outreach',
        status=status, created_at=clock.now()-timedelta(days=2)))
    session.flush()
    result = ranking.rank_candidates(session, shift, clock.now())
    assert [c.volunteer.id for c in result] == [first.id, second.id]
    assert 'asked_recently' not in result[0].breakdown


def test_draft_never_books_an_already_started_slot(session, clock, make_volunteer, make_shift):
    make_volunteer()
    past = make_shift(starts=clock.now()-timedelta(hours=1))
    future = make_shift(starts=clock.now()+timedelta(days=1))
    report = scheduler.draft(session, clock, '2026-10')
    assert scheduler.occupied(session, past) is None
    assert scheduler.occupied(session, future) is not None
    assert past.id in report['gaps']


def test_month_validation_counts_completed_service_history(session, clock, make_volunteer, make_shift, assign):
    person = make_volunteer(prefs={'max_per_month': 1})
    past = make_shift(starts=clock.now()-timedelta(hours=2))
    past.event.status = 'completed'
    assign(person, past, status='completed')
    future = make_shift(starts=clock.now()+timedelta(days=1))
    assign(person, future)
    report = scheduler.validate(session, '2026-10')
    assert any(v.get('volunteer_id') == person.id and v.get('reason') == 'monthly maximum'
        and v.get('count') == 2 for v in report['violations'])


def test_month_validation_does_not_count_cancelled_or_other_month_history(
        session, clock, make_volunteer, make_shift, assign):
    person = make_volunteer(prefs={'max_per_month': 1})
    old = make_shift(starts=clock.now()-timedelta(days=1))
    old.event.status = 'completed'
    assign(person, old, status='completed')
    cancelled = make_shift(starts=clock.now()+timedelta(days=1))
    assign(person, cancelled, status='cancelled')
    future = make_shift(starts=clock.now()+timedelta(days=2))
    assign(person, future)
    assert scheduler.validate(session, '2026-10')['violations'] == []


def test_critical_constrained_slot_precedes_less_constrained_slot(
        session, clock, make_volunteer, make_shift):
    broad = make_shift('General', criticality='critical')
    narrow = make_shift('Restricted', required=('training',), criticality='critical')
    specialist = make_volunteer(quals=[('training', 'verified', None)])
    generalist = make_volunteer()
    report = scheduler.draft(session, clock, '2026-10')
    assert not report['gaps'] and not report['violations']
    assert scheduler.occupied(session, narrow).volunteer_id == specialist.id
    assert scheduler.occupied(session, broad).volunteer_id == generalist.id


@pytest.mark.parametrize('offset', [timedelta(0), -timedelta(microseconds=1)])
def test_direct_proposal_cannot_bypass_started_slot_guard(session, clock, make_volunteer, make_shift, offset):
    person = make_volunteer()
    shift = make_shift(starts=clock.now()+offset)
    assert scheduler.propose(session, clock, shift, person, 'America/Denver') == {
        'error': 'shift has already started'}
    assert scheduler.occupied(session, shift) is None


def test_future_message_never_changes_present_fairness(session, clock, make_volunteer, make_shift):
    first, second = make_volunteer(), make_volunteer()
    shift = make_shift()
    session.add(m.Message(direction='out', volunteer_id=first.id, phone=first.phone,
        body='Synthetic future history', kind='ai', purpose='outreach', status='sent',
        created_at=clock.now()+timedelta(days=1)))
    session.flush()
    assert [c.volunteer.id for c in ranking.rank_candidates(session, shift, clock.now())] == [first.id, second.id]


def test_original_k100_is_order_invariant_and_urgency_selects_minimal_prefix(clock):
    now = clock.now()
    people = [algo.Volunteer('a', .8, 30, now-timedelta(days=1)),
        algo.Volunteer('b', .4, 60, now-timedelta(days=2)),
        algo.Volunteer('c', .2, 120, now-timedelta(days=3)),
        algo.Volunteer('d', .1, 120, now-timedelta(days=1))]
    config = algo.ScoringConfig(100)
    previous = 0
    for lead in [timedelta(days=7), timedelta(hours=2), timedelta(minutes=13)]:
        results = [algo.plan_initial_batch(pool, 1, now, config, deadline=now+lead)
            for pool in itertools.permutations(people)]
        plan = results[0]
        assert all(p == plan for p in results)
        assert len(plan.volunteers) >= previous
        previous = len(plan.volunteers)
        if plan.target_met:
            assert sum(p.acceptance_rate for p in plan.volunteers[:-1]) < plan.target
        assert plan.expected_acceptances == pytest.approx(sum(p.acceptance_rate for p in plan.volunteers))
    assert algo.volunteer_score(people[0], now, config) == pytest.approx(10*38.4/138.4)


def test_timed_expansion_uses_only_supplied_probability_and_never_recontacts(clock):
    now = clock.now()
    people = [algo.create_volunteer(str(i), [], now) for i in range(6)]
    deadline = now+timedelta(hours=2)
    pending = [algo.PendingRequest(people[0], now-timedelta(minutes=30))]
    calls = []
    def probability(person, elapsed, remaining):
        calls.append((person.volunteer_id, elapsed, remaining))
        return .1 if elapsed else .5
    plan = algo.plan_follow_up(people, pending, ['1', '2'], 1, now, deadline,
        algo.ScoringConfig(100), probability)
    assert [p.volunteer_id for p in plan.volunteers] == ['3', '4', '5']
    assert calls == [('0', 30, 120), ('3', 0, 120), ('4', 0, 120), ('5', 0, 120)]
    assert plan.expected_acceptances == pytest.approx(1.6)
    assert plan.target_met


def test_accelerated_batch_cycle_rechecks_consent_expires_then_first_winner(
        session, clock, provider, make_volunteer, make_shift):
    from app.agents import fill_agent
    from app.core import offer_windows as offers, algorithm_outreach as adapter
    from tests.test_clyde_algorithm import enable, open_fill, context, start, rows
    enable(session)
    shift = make_shift(required=('training',))
    people = [make_volunteer(quals=[('training', 'verified', None)]) for _ in range(6)]
    unqualified = make_volunteer()
    fill = open_fill(session, clock, shift)
    ctx = context(session, clock, provider)
    assert start(ctx, fill).action == 'tranche_sent'
    first, second = rows(session, fill)
    assert [first.volunteer_id, second.volunteer_id] == [p.id for p in people[:2]]
    people[0].sms_opt_in = False
    assert fill_agent.on_outreach_reply(ctx, people[0], first, 'accept').action == 'request_closed'
    assert len(rows(session, fill)) == 2
    session.commit()  # A subsequent worker context consumes durable records.
    ctx = context(session, clock, provider)
    clock.set_time(offers.metadata(session, second).expires_at)
    assert fill_agent.advance_due(ctx)[0].action == 'tranche_sent'
    assert second.response == 'expired'
    third, fourth = rows(session, fill)[-2:]
    assert [third.volunteer_id, fourth.volunteer_id] == [p.id for p in people[2:4]]
    assert fill_agent.on_outreach_reply(ctx, people[2], third, 'decline').action == 'tranche_sent'
    fifth = rows(session, fill)[-1]
    assert fifth.volunteer_id == people[4].id
    decline_receipt = adapter.receipt(session, fill)
    assert decline_receipt.detail['decline_ids'] == [third.id]
    assert fill_agent.on_outreach_reply(ctx, people[4], fifth, 'accept').action == 'filled'
    assert fourth.response == 'revoked'
    fill_agent.on_outreach_reply(ctx, people[2], third, 'decline')
    fill_agent.on_outreach_reply(ctx, people[3], fourth, 'accept')
    assert scheduler.occupied(session, shift).volunteer_id == people[4].id
    assert fill.next_action_at is None and not fill_agent.advance_due(ctx)
    assert len(rows(session, fill)) == 5
    assert len({o.volunteer_id for o in rows(session, fill)}) == 5
    assert not provider.sent_to(unqualified.phone)
    assert all('\u2014' not in msg.body for msg in provider.sent)


def test_cancellation_after_split_review_preserves_no_booking_and_expires_only_receipts(
        session, clock, provider, make_volunteer, make_shift, tmp_path):
    from sqlalchemy import select
    from app.core import split_coverage as split
    from tests.test_reviewed_split_coverage import prepare_partial, partition, accept_children, OWNER
    prepared = prepare_partial(session, clock, provider, make_shift, make_volunteer, tmp_path)
    children = partition(session, clock, prepared)
    people = accept_children(session, clock, prepared, make_volunteer, children)
    approval = split.stage_booking(session, OWNER, prepared.parent.id, clock.now())
    session.commit()
    session.add(m.Message(direction='in', volunteer_id=people[1].id, phone=people[1].phone,
        body='I cannot serve that interval after all', status='received', kind='inbound',
        created_at=clock.now()))
    session.flush()
    with pytest.raises(ValueError):
        split.decide(session, approval.id, OWNER, approval.payload['content_hash'], True, clock.now())
    assert not session.scalar(select(m.Assignment))
    assert len(split.children(session, prepared.parent.id)) == 2
    assert approval.status == 'pending'
    receipts = session.scalars(select(m.Notification).where(m.Notification.purpose=='split_acceptance')).all()
    clock.set_time(max(r.expires_at for r in receipts))
    split.expire_acceptances(session, clock.now())
    assert all(r.state == 'expired' for r in receipts)
    assert all(session.scalar(select(m.FillRequest).where(m.FillRequest.shift_id==c.id)).state == 'in_progress'
        for c in children)
    assert not session.scalar(select(m.Assignment))
