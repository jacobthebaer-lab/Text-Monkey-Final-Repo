"""Recipient and timing acceptance tests with fictional data and no live transport."""
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

import pytest
from sqlalchemy import select

from app.agents import fill_agent
from app.core import algorithm_outreach as adapter, clyde_algorithm as algo, offer_windows as offers
from app.core.send_gate import SendStatus
from app.db import models as m
from app.llm.tools import fill_agent_tools, replacement_pool
from tests.test_fill_agent import ScriptedAgentGloo


def enable(session, *, buffer=0.5):
    session.add(m.Policy(key="algorithm_outreach_enabled", value={"value": True}))
    session.add(m.Policy(key="recipient_algorithm", value={"value": {
        "k": 100.0, "timescale_minutes": 120.0, "maximum_buffer": buffer}}))
    session.flush()


def open_fill(session, clock, shift):
    row = m.FillRequest(shift_id=shift.id, urgency="normal", state="in_progress", created_at=clock.now())
    session.add(row); session.flush()
    return row


def context(session, clock, provider):
    return fill_agent.FillContext(session, clock, provider, ScriptedAgentGloo())


def start(ctx, fill):
    from app.llm.agent_loop import RunLogger
    logger = RunLogger(ctx.session, ctx.clock, agent="fill_agent", trigger="synthetic algorithm acceptance")
    result = fill_agent._open_tranche(ctx, fill, logger=logger)
    logger.close(result.action)
    return result


def rows(session, fill):
    return session.scalars(select(m.Outreach).where(m.Outreach.fill_request_id == fill.id).order_by(m.Outreach.id)).all()


def measured_offer(session, clock, person, shift, *, age, response, response_minutes=None, status="sent"):
    fill = open_fill(session, clock, shift)
    when = clock.now() - timedelta(minutes=age)
    message = m.Message(direction="out", volunteer_id=person.id, phone=person.phone, body="Synthetic request",
        purpose="outreach", kind="ai", status=status, created_at=when)
    session.add(message); session.flush()
    outreach = m.Outreach(fill_request_id=fill.id, volunteer_id=person.id, tranche=1, message_id=message.id,
        response=response, responded_at=when+timedelta(minutes=response_minutes) if response_minutes is not None else None)
    session.add(outreach); session.flush()
    meta = offers.prepare(session, outreach, "Synthetic request", when)
    meta.detail = {**meta.detail, "dispatched_at": when.isoformat()}
    meta.expires_at = when+timedelta(minutes=120)
    fill.state = "filled" if response == "yes" else "escalated"
    session.flush()
    return outreach


def test_exact_supplied_score_and_shortest_prefix():
    now = datetime(2026, 10, 6, tzinfo=timezone.utc)
    pool = [algo.Volunteer("slow", .5, 120, now-timedelta(days=1)),
            algo.Volunteer("fast", .8, 30, now-timedelta(days=1)),
            algo.Volunteer("rare", .1, 10, now-timedelta(days=1))]
    config = algo.ScoringConfig(100)
    assert algo.volunteer_score(pool[1], now, config) == pytest.approx(10*38.4/138.4)
    plan = algo.plan_initial_batch(pool, 1, now, config, deadline=now+timedelta(hours=2))
    assert [v.volunteer_id for v in plan.volunteers] == ["fast", "rare", "slow"]
    assert plan.expected_acceptances == pytest.approx(1.4) and plan.target == 1.25
    assert plan.target_met
    assert not algo.plan_initial_batch(pool, 2, now, config, deadline=now+timedelta(hours=2)).target_met
    assert not algo.plan_initial_batch(pool, 1, now, config, deadline=now).volunteers


def test_pending_model_is_required_and_contacted_people_are_excluded():
    now = datetime(2026, 10, 6, tzinfo=timezone.utc)
    pool = [algo.create_volunteer(str(i), [], now) for i in range(3)]
    with pytest.raises(TypeError):
        algo.plan_follow_up(pool, [], [], 1, now, now+timedelta(hours=1), algo.ScoringConfig(100))
    plan = algo.plan_follow_up(pool, [algo.PendingRequest(pool[0], now-timedelta(minutes=5))],
        ["1"], 1, now, now+timedelta(hours=1), algo.ScoringConfig(100), lambda v, elapsed, left: .2)
    assert [v.volunteer_id for v in plan.volunteers] == ["2"] and not plan.target_met


def test_real_history_excludes_queue_and_counts_expiry(session, clock, make_volunteer, make_shift):
    person = make_volunteer()
    old_shift = make_shift(starts=clock.now()+timedelta(days=1))
    measured_offer(session, clock, person, old_shift, age=5000, response="yes", response_minutes=20)
    measured_offer(session, clock, person, old_shift, age=3000, response="no", response_minutes=40)
    measured_offer(session, clock, person, old_shift, age=2000, response="expired")
    measured_offer(session, clock, person, old_shift, age=100, response="yes", response_minutes=1, status="queued")
    measured_offer(session, clock, person, old_shift, age=50, response="none", status="uncertain")
    profile = adapter.profile(session, person, clock.now())
    assert profile.acceptance_rate == pytest.approx(1/3)
    assert profile.average_response_minutes == 30
    assert profile.last_requested_at == clock.now()-timedelta(minutes=2000)


def test_enrollment_snapshot_is_fixed_and_elapsed_grows(session, clock, make_volunteer, make_shift):
    peer = make_volunteer(created_at=clock.now()-timedelta(days=5))
    old = make_shift(starts=clock.now()-timedelta(days=2))
    measured_offer(session, clock, peer, old, age=3000, response="yes", response_minutes=20)
    newcomer = make_volunteer(created_at=clock.now())
    first = adapter.profile(session, newcomer, clock.now())
    assert first.last_requested_at is None and first.average_response_minutes == 20
    snapshot = session.get(m.Policy, f"algorithm-enrollment:{newcomer.id}").value
    clock.advance(timedelta(hours=1))
    later = adapter.profile(session, newcomer, clock.now())
    assert session.get(m.Policy, f"algorithm-enrollment:{newcomer.id}").value == snapshot
    assert algo.elapsed_since_request(later, clock.now()) == algo.elapsed_since_request(first, clock.now()-timedelta(hours=1))+60
    assert not session.scalars(select(m.Message).where(m.Message.volunteer_id == newcomer.id)).all()


def test_batch_dispatch_decline_once_and_first_acceptance_wins(session, clock, provider, make_volunteer, make_shift):
    enable(session)
    shift = make_shift()
    people = [make_volunteer(f"Fictional Helper {i}") for i in range(5)]
    fill = open_fill(session, clock, shift)
    ctx = context(session, clock, provider)
    assert start(ctx, fill).action == "tranche_sent"
    asks = rows(session, fill)
    assert [o.volunteer_id for o in asks] == [people[0].id, people[1].id]
    assert len(provider.sent) == 2
    deadlines = [offers.metadata(session, o).expires_at for o in asks]
    assert fill.next_action_at == min(deadlines)
    assert deadlines[0] == clock.now()+timedelta(minutes=120)
    # No invented probability model or early re-request while either ask waits.
    clock.advance(timedelta(minutes=5))
    assert fill_agent.advance_due(ctx) == []
    assert len(rows(session, fill)) == 2
    assert fill_agent.on_outreach_reply(ctx, people[0], asks[0], "decline").action == "tranche_sent"
    assert [o.volunteer_id for o in rows(session, fill)] == [p.id for p in people[:3]]
    assert adapter.receipt(session, fill).detail["decline_ids"] == [asks[0].id]
    fill_agent.on_outreach_reply(ctx, people[0], asks[0], "decline")
    assert len(rows(session, fill)) == 3
    winner = rows(session, fill)[-1]
    assert fill_agent.on_outreach_reply(ctx, people[2], winner, "accept").action == "filled"
    assert asks[1].response == "revoked"
    fill_agent.on_outreach_reply(ctx, people[1], asks[1], "accept")
    bookings = session.scalars(select(m.Assignment).where(m.Assignment.shift_id == shift.id)).all()
    assert len(bookings) == 1 and bookings[0].volunteer_id == people[2].id
    assert fill.next_action_at is None


def test_expiration_contacts_next_batch_once(session, clock, provider, make_volunteer, make_shift):
    enable(session)
    shift = make_shift()
    people = [make_volunteer() for _ in range(5)]
    fill = open_fill(session, clock, shift); ctx = context(session, clock, provider)
    start(ctx, fill)
    deadline = fill.next_action_at
    clock.set_time(deadline-timedelta(microseconds=1))
    assert not fill_agent.advance_due(ctx)
    clock.set_time(deadline)
    assert fill_agent.advance_due(ctx)[0].action == "tranche_sent"
    assert [o.response for o in rows(session, fill)[:2]] == ["expired", "expired"]
    assert [o.volunteer_id for o in rows(session, fill)] == [p.id for p in people[:4]]
    fill_agent.advance_due(ctx)
    assert len(rows(session, fill)) == 4


def test_selection_filters_and_no_repeat_across_event(session, clock, provider, make_volunteer, make_shift):
    enable(session, buffer=0)
    shift = make_shift(required=("trained",))
    unqualified = make_volunteer()
    opted_out = make_volunteer(opt_in=False, quals=[("trained", "verified", None)])
    stopped = make_volunteer(quals=[("trained", "verified", None)])
    session.add(m.Policy(key="sms_opt_out:"+stopped.phone, value={"value": True}))
    good = [make_volunteer(quals=[("trained", "verified", None)]) for _ in range(2)]
    fill = open_fill(session, clock, shift); ctx = context(session, clock, provider)
    start(ctx, fill)
    assert [o.volunteer_id for o in rows(session, fill)] == [good[0].id]
    another = m.Shift(event_id=shift.event_id, role_id=shift.role_id, slot_index=1)
    session.add(another); session.flush()
    second = open_fill(session, clock, another)
    start(ctx, second)
    assert [o.volunteer_id for o in rows(session, second)] == [good[1].id]
    assert not any(provider.sent_to(v.phone) for v in (unqualified, opted_out, stopped))


def test_model_cannot_substitute_and_source_changes_block_delivery(session, clock, provider, make_volunteer, make_shift):
    enable(session, buffer=0)
    shift = make_shift()
    people = [make_volunteer() for _ in range(2)]
    fill = open_fill(session, clock, shift); fill.current_tranche = 1
    pool = replacement_pool(session, fill, clock.now(), "America/Denver")
    plan = adapter.plan(session, fill, pool, clock.now())
    adapter.reserve(session, fill, plan, clock.now())
    ctx = context(session, clock, provider)
    tools = fill_agent_tools(session, clock, ctx.gate, fill, max_candidates=1)
    result = tools['choose_replacements'].handler({'volunteer_ids': [people[1].id], 'reason': 'Choose someone else'})
    assert 'error' in result and [o.volunteer_id for o in rows(session, fill)] == [people[0].id]
    shift.event.starts_at += timedelta(hours=1)
    result = ctx.gate.send(body='Could you help?', purpose='outreach', volunteer=people[0], kind='ai',
                          role=shift.role, fill_request_id=fill.id)
    assert result.status == SendStatus.BLOCKED_POLICY and not provider.sent


def test_quiet_hours_and_short_notice_cutoff(session, clock, provider, make_volunteer, make_shift):
    enable(session)
    clock.set_time(clock.now().replace(hour=22))
    shift = make_shift(starts=clock.now()+timedelta(days=2))
    make_volunteer()
    fill = open_fill(session, clock, shift); ctx = context(session, clock, provider)
    assert start(ctx, fill).action == 'waiting_quiet'
    assert fill.next_action_at.astimezone(ZoneInfo('America/Denver')).hour == 7
    assert not rows(session, fill) and not provider.sent
    clock.set_time(fill.next_action_at)
    assert fill_agent.advance_due(ctx)[0].action == 'tranche_sent'
    clock.set_time(offers.cutoff(session, shift.event.starts_at))
    assert fill_agent.advance_due(ctx)[0].action == 'escalated'
    tiny = make_shift(starts=clock.now()+timedelta(minutes=11))
    short = open_fill(session, clock, tiny)
    assert start(ctx, short).action == 'escalated'
    assert not rows(session, short)


def test_default_hold_keeps_transport_inactive(session, clock, provider, make_volunteer, make_shift):
    shift = make_shift(); make_volunteer(); make_volunteer()
    fill = open_fill(session, clock, shift)
    assert start(context(session, clock, provider), fill).action == 'offer_blocked'
    assert len(rows(session, fill)) == 2 and not provider.sent


def test_filled_position_closes_pending_batch_before_reply_deadline(session, clock, provider, make_volunteer, make_shift, assign):
    enable(session)
    shift = make_shift()
    people = [make_volunteer() for _ in range(3)]
    fill = open_fill(session, clock, shift); ctx = context(session, clock, provider)
    start(ctx, fill)
    assign(people[2], shift, status='confirmed')
    assert fill_agent.advance_due(ctx)[0].action == 'already_filled'
    assert all(o.response == 'revoked' for o in rows(session, fill))
    assert fill.state == 'filled' and fill.next_action_at is None
    assert len([message for message in provider.sent if 'Could you cover' in message.body]) == 2


def test_invalid_algorithm_settings_hold_instead_of_sending(session, clock, provider, make_volunteer, make_shift):
    enable(session)
    session.get(m.Policy, 'recipient_algorithm').value = {'value': {'k': 0}}
    shift = make_shift(); make_volunteer()
    fill = open_fill(session, clock, shift)
    assert start(context(session, clock, provider), fill).action == 'escalated_system'
    assert not rows(session, fill) and not provider.sent


def test_elapsed_time_uses_utc_across_dst_fold():
    zone = ZoneInfo('America/Denver')
    before = datetime(2026, 11, 1, 1, 30, tzinfo=zone, fold=0)
    after = datetime(2026, 11, 1, 1, 30, tzinfo=zone, fold=1)
    assert algo.minutes_between(after, before) == 60
    person = algo.Volunteer('fold', 1, 60, before)
    assert algo.volunteer_score(person, after, algo.ScoringConfig(100)) == pytest.approx(10/101)


def test_simultaneous_batch_acceptances_have_one_winner(tmp_path, clock):
    from concurrent.futures import ThreadPoolExecutor
    from threading import Barrier
    from sqlalchemy import create_engine
    from sqlalchemy.orm import sessionmaker
    from app.sms.mock_provider import MockSMSProvider
    engine = create_engine('sqlite:///'+str(tmp_path/'synthetic-race.sqlite'),
                           connect_args={'check_same_thread': False, 'timeout': 10})
    m.Base.metadata.create_all(engine)
    factory = sessionmaker(bind=engine, expire_on_commit=False)
    with factory() as session:
        enable(session)
        role = m.Role(name='Fictional usher', ministry='Test', required_qualifications=[],
                      criticality='standard', fill_policy='auto')
        event = m.Event(title='Fictional service', starts_at=clock.now()+timedelta(days=2),
                        ends_at=clock.now()+timedelta(days=2, hours=1), status='scheduled')
        session.add_all([role, event]); session.flush()
        shift = m.Shift(role_id=role.id, event_id=event.id, slot_index=0)
        session.add(shift); session.flush()
        for i in range(2):
            session.add(m.Volunteer(name=f'Fictional Racer {i}', phone=f'+1555020990{i}',
                sms_opt_in=True, status='active', preferences={}, created_at=clock.now()))
        session.flush()
        fill = open_fill(session, clock, shift)
        start(context(session, clock, MockSMSProvider()), fill)
        ids = [(o.volunteer_id, o.id) for o in rows(session, fill)]
        shift_id = shift.id
        session.commit()
    barrier = Barrier(2)
    def accept(pair):
        with factory() as session:
            volunteer = session.get(m.Volunteer, pair[0])
            outreach = session.get(m.Outreach, pair[1])
            barrier.wait(timeout=5)
            outcome = fill_agent.on_outreach_reply(context(session, clock, MockSMSProvider()), volunteer, outreach, 'accept')
            session.commit()
            return outcome.action
    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(accept, ids))
    assert sorted(results) == ['filled', 'offer_closed']
    with factory() as session:
        assert len(session.scalars(select(m.Assignment).where(m.Assignment.shift_id == shift_id)).all()) == 1
    engine.dispose()


def test_external_placement_revokes_selected_winners_unanswered_offer(session, clock, provider, make_volunteer, make_shift, assign):
    enable(session)
    shift = make_shift(); person = make_volunteer(); make_volunteer()
    fill = open_fill(session, clock, shift); ctx = context(session, clock, provider)
    start(ctx, fill)
    assign(person, shift, status='approved')
    fill_agent.advance_due(ctx)
    assert all(o.response == 'revoked' for o in rows(session, fill))
    assert fill.state == 'filled'


def test_decline_during_quiet_hours_waits_without_losing_unique_replacement(session, clock, provider, make_volunteer, make_shift):
    enable(session)
    clock.set_time(clock.now().replace(hour=21, minute=29))
    shift = make_shift(starts=clock.now()+timedelta(hours=12))
    people = [make_volunteer() for _ in range(3)]
    fill = open_fill(session, clock, shift); ctx = context(session, clock, provider)
    start(ctx, fill)
    asks = rows(session, fill)
    clock.advance(timedelta(minutes=2))
    assert fill_agent.on_outreach_reply(ctx, people[0], asks[0], 'decline').action == 'waiting_quiet'
    assert len(rows(session, fill)) == 2
    clock.set_time(fill.next_action_at)  # pending offer expires during quiet hours
    fill_agent.advance_due(ctx)
    assert asks[1].response == 'expired' and len(rows(session, fill)) == 2
    assert fill.state == 'waiting_quiet'
    clock.set_time(fill.next_action_at)  # quiet hours end
    fill_agent.advance_due(ctx)
    assert len(rows(session, fill)) == 3
    assert rows(session, fill)[-1].volunteer_id == people[2].id
    assert len(provider.sent) == 3
    fill_agent.on_outreach_reply(ctx, people[0], asks[0], 'decline')
    assert len(rows(session, fill)) == 3


def test_gloo_failure_never_dispatches_reserved_batch(session, clock, provider, make_volunteer, make_shift):
    from tests.test_fill_agent import FailingGloo
    enable(session)
    shift = make_shift(); make_volunteer(); make_volunteer()
    fill = open_fill(session, clock, shift); ctx = context(session, clock, provider); ctx.gloo = FailingGloo()
    assert start(ctx, fill).action == 'escalated_system'
    assert len(rows(session, fill)) == 2
    assert not provider.sent
    assert all(o.message_id is None for o in rows(session, fill))


def test_native_queue_creation_does_not_start_cooldown_or_month_budget(session, clock, make_volunteer, make_shift):
    from app.llm.tools import replacement_pool
    person = make_volunteer(); other = make_volunteer()
    old = make_shift(starts=clock.now()-timedelta(days=1))
    # Created last month, dispatched only two hours ago, now expired.
    message = m.Message(direction='out', volunteer_id=person.id, phone=person.phone, body='Synthetic ask',
        purpose='outreach', kind='ai', status='submitted', created_at=clock.now()-timedelta(days=4))
    session.add(message); session.flush()
    past_fill = open_fill(session, clock, old); past_fill.state = 'escalated'
    past = m.Outreach(fill_request_id=past_fill.id, volunteer_id=person.id, tranche=1,
        response='expired', message_id=message.id)
    session.add(past); session.flush()
    now = clock.now()
    session.add(m.Notification(key=f'offer:{past.id}', volunteer_id=person.id, event_id=old.event_id,
        purpose='offer_window', state='offer_expired', body='', created_at=message.created_at,
        due_at=now, expires_at=now, detail={'dispatched_at': (now-timedelta(hours=2)).isoformat()}))
    session.flush()
    upcoming = make_shift()
    fill = open_fill(session, clock, upcoming)
    pool = replacement_pool(session, fill, now, 'America/Denver')
    plan = adapter.plan(session, fill, pool, now)
    assert [v.volunteer_id for v in plan.volunteers] == [str(other.id)]
    assert adapter.ask_problem(session, person.id, now) == 'outreach cooldown reached'
    session.add(m.Policy(key='outreach_cooldown_hours', value={'value': 0}))
    session.add(m.Policy(key='monthly_ask_budget_per_volunteer', value={'value': 1}))
    session.flush()
    assert adapter.ask_problem(session, person.id, now) == 'monthly ask budget reached'
