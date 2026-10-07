"""Mixed requests change one actual booking and answer from current facts."""
import json
import pytest
from datetime import timedelta
from sqlalchemy import select
from app.db import models as m
from app.core.inbound import handle_inbound, _schedule_instruction
from app.core.cancellation_scope import explicit_target
from app.agents.fill_agent import FillContext
from app.llm.parser import ParsedMessage
from tests.test_opportunities_reply import ExactGloo


def setup(session, clock, make_volunteer, make_shift, assign):
    volunteer = make_volunteer(prefs={'onboarding_stage': 'complete', 'interested_roles': ['Greeter'], 'max_per_month': 2})
    first = assign(volunteer, make_shift('Greeter', starts=clock.now()+timedelta(days=10)))
    second = assign(volunteer, make_shift('Greeter', starts=clock.now()+timedelta(days=17)))
    extra = make_shift('Greeter', starts=clock.now()+timedelta(days=24))
    return volunteer, first, second, extra


def route(session, clock, provider, volunteer, gloo, body):
    return handle_inbound(session, clock, provider, volunteer.phone, body,
        lambda _: ParsedMessage(intent='cancel', confidence=.99, shift_hint='October 18'),
        ctx=FillContext(session, clock, provider, gloo))


def test_combined_cancel_and_service_dates_preserves_other_booking_and_cap(session, clock, provider, make_volunteer, make_shift, assign):
    volunteer, first, second, extra = setup(session,clock,make_volunteer,make_shift,assign)
    gloo = ExactGloo()
    result = route(session,clock,provider,volunteer,gloo,"I can't make October 18, but could you show me additional service dates?")
    assert result.routed_to == 'fill_agent'
    assert first.status == 'approved' and second.status == 'cancelled'
    assert volunteer.preferences['max_per_month'] == 2
    assert len(session.scalars(select(m.Assignment)).all()) == 2
    replies = provider.sent_to(volunteer.phone)
    assert len(replies) == 1
    assert 'booking has been cancelled' in replies[0].body and 'Sun Oct 18' in replies[0].body
    assert 'Sun Oct 25' in replies[0].body and 'openings, not bookings' in replies[0].body
    row = session.get(m.Notification, f'cancellation-reply:{session.scalar(select(m.Message.id).where(m.Message.direction=="in"))}')
    options = row.detail['conversation_meta']['binding']['options']['opportunities']['eligible_open_shifts']
    assert [x['shift_id'] for x in options] == [extra.id]
    assert '\u2014' not in replies[0].body


def test_ambiguous_date_and_weekday_acknowledge_without_cancelling(session,clock,provider,make_volunteer,make_shift,assign):
    volunteer, first, second, _ = setup(session,clock,make_volunteer,make_shift,assign)
    same_day = assign(volunteer,make_shift('Usher', starts=second.shift.starts_at))
    assert explicit_target([first,second,same_day], 'Cancel October 18', clock.now().tzinfo) is None
    assert explicit_target([first,second], 'Cancel Sunday', clock.now().tzinfo) is None
    assert explicit_target([first,second,same_day], 'Cancel Usher October 18', clock.now().tzinfo) is same_day
    assert explicit_target([first,second], 'Cancel Usher October 18', clock.now().tzinfo,role_names=['Greeter','Usher']) is None
    route(session,clock,provider,volunteer,ExactGloo(),"I can't make October 18")
    assert first.status == second.status == same_day.status == 'approved'
    assert len(provider.sent_to(volunteer.phone)) == 1
    assert 'No schedule changes' in provider.sent_to(volunteer.phone)[0].body


def test_year_is_authoritative_and_hypothetical_question_is_not_permission(session,clock,make_volunteer,make_shift,assign):
    _, first, second, _ = setup(session,clock,make_volunteer,make_shift,assign)
    assert explicit_target([first,second], 'Cancel October 18, 2027', clock.now().tzinfo) is None
    assert _schedule_instruction("I can't make October 18?") is None
    assert _schedule_instruction("What if I can't make October 18, could you show me other dates?") is None
    assert _schedule_instruction("I can't make October 18, but could you show me other dates?") == 'cancel'


def test_gloo_outage_keeps_saved_cancellation_and_durable_factual_retry(session,clock,provider,make_volunteer,make_shift,assign):
    from app.core.notifications import flush_due
    volunteer, first, second, _ = setup(session,clock,make_volunteer,make_shift,assign)
    gloo = ExactGloo(); gloo.fail = True
    route(session,clock,provider,volunteer,gloo,"I can't make October 18")
    row = session.scalar(select(m.Notification).where(m.Notification.key.like('cancellation-reply:%')))
    assert first.status == 'approved' and second.status == 'cancelled'
    assert row.state == 'pending' and not provider.sent_to(volunteer.phone)
    gloo.fail = False; clock.set_time(row.due_at)
    flush_due(FillContext(session,clock,provider,gloo))
    assert len(provider.sent_to(volunteer.phone)) == 1 and 'cancelled' in provider.sent_to(volunteer.phone)[0].body
    flush_due(FillContext(session,clock,provider,gloo))
    assert len(provider.sent_to(volunteer.phone)) == 1



from types import SimpleNamespace
from fastapi.testclient import TestClient
from tests.test_mac_progress import (progress_app, schedule_bookings, post, incoming, submit_ack, wait_worker, SCHEDULE_BODY)  # noqa: E402,F401
from app.integrations import mac_progress


@pytest.mark.parametrize('change',['unchanged','source','profile','booking'])
def test_actual_mac_pipeline_queues_ack_then_saved_result_and_deduplicates(progress_app,change):
    schedule_bookings(progress_app)
    gloo = progress_app.state.gloo
    original = gloo.create_response
    def classify_or_compose(**kwargs):
        if kwargs['input'] == SCHEDULE_BODY:
            return SimpleNamespace(output_text=json.dumps({'intent':'cancel','confidence':.99,'shift_hint':'October 18'}))
        return original(**kwargs)
    gloo.create_response = classify_or_compose
    with TestClient(progress_app) as client:
        accepted = post(client,'/mac/inbound',incoming('synthetic-saved-cancellation',SCHEDULE_BODY)).json()
        assert accepted['progress_state'] == 'waiting_ack'
        with progress_app.state.session_factory() as session:
            assert [a.status for a in session.scalars(select(m.Assignment).order_by(m.Assignment.id))] == ['approved','approved']
        submit_ack(client,mac_progress.SCHEDULE_ACK_TEXT)
        post(client,'/mac/progress/tick'); wait_worker(progress_app)
        with progress_app.state.session_factory() as session:
            job = session.get(m.Notification,accepted['progress_key'])
            assert job.state == 'done', job.detail
            assert [a.status for a in session.scalars(select(m.Assignment).order_by(m.Assignment.id))] == ['approved','cancelled']
            assert len(session.scalars(select(m.Message).where(m.Message.direction=='in')).all()) == 1
            assert len(session.scalars(select(m.Message).where(m.Message.direction=='out')).all()) == 2
            assert session.scalar(select(m.Volunteer)).preferences['max_per_month'] == 2
        if change != 'unchanged':
            with progress_app.state.session_factory() as session:
                if change == 'source':
                    session.scalar(select(m.Message).where(m.Message.direction=='in')).body = 'Changed original source'
                elif change == 'profile':
                    volunteer = session.scalar(select(m.Volunteer))
                    volunteer.preferences = {**volunteer.preferences,'max_per_month':1}
                else:
                    session.scalar(select(m.Assignment).where(m.Assignment.status=='cancelled')).status = 'approved'
                session.commit()
            assert post(client,'/mac/outbound/pull').json()['messages'] == []
            with progress_app.state.session_factory() as session:
                assert session.scalar(select(m.Message.status).where(m.Message.id==3)) == 'blocked_policy'
            return
        final = post(client,'/mac/outbound/pull').json()['messages']
        with progress_app.state.session_factory() as session:
            diagnostics = [(x.key,x.detail.get('reason')) for x in session.scalars(select(m.Notification)) if x.detail.get('reason')]
        assert len(final) == 1 and 'booking has been cancelled' in final[0]['body'], diagnostics
        assert post(client,f"/mac/outbound/{final[0]['id']}/verify",{'token':final[0]['token']}).status_code == 200
        assert post(client,f"/mac/outbound/{final[0]['id']}/ack",{'token':final[0]['token'],'outcome':'submitted'}).status_code == 200
        assert post(client,'/mac/inbound',incoming('synthetic-saved-cancellation',SCHEDULE_BODY)).json()['duplicate']
        post(client,'/mac/progress/tick'); wait_worker(progress_app)
        assert post(client,'/mac/outbound/pull').json()['messages'] == []


def test_explicit_unmatched_date_never_cancels_sole_remaining_booking(session,clock,provider,make_volunteer,make_shift,assign):
    volunteer = make_volunteer(prefs={'onboarding_stage':'complete'})
    booked = assign(volunteer,make_shift('Greeter',starts=clock.now()+timedelta(days=10)))
    result = route(session,clock,provider,volunteer,ExactGloo(),"I can't make October 18")
    assert result.routed_to == 'cancellation_review' and booked.status == 'approved'
    assert len(provider.sent_to(volunteer.phone)) == 1
    assert 'No schedule changes' in provider.sent_to(volunteer.phone)[0].body


@pytest.mark.parametrize('body',[
    'Cancel Greeter Sunday October 18, 2027',
    'Cancel Greeter Sunday October 25',
    'Cancel Greeter Sunday 2027-10-18',
    'Cancel Greeter Sunday 2026-10-25',
    'Cancel Greeter Sunday 2027',
])
def test_explicit_calendar_conflict_never_falls_back_to_matching_weekday(session,clock,make_volunteer,make_shift,assign,body):
    volunteer = make_volunteer(prefs={'onboarding_stage':'complete'})
    booking = assign(volunteer,make_shift('Greeter',starts=clock.now()+timedelta(days=17)))
    assert explicit_target([booking],body,clock.now().tzinfo) is None


@pytest.mark.parametrize('body',[
    'Cancel Greeter Sunday October 18, 2026',
    'Cancel Greeter Sunday October 18',
    'Cancel Greeter Sunday 2026-10-18',
    'Cancel Greeter Sunday',
])
def test_legitimate_date_and_weekday_keep_role_and_uniqueness_guard(session,clock,make_volunteer,make_shift,assign,body):
    volunteer = make_volunteer(prefs={'onboarding_stage':'complete'})
    booking = assign(volunteer,make_shift('Greeter',starts=clock.now()+timedelta(days=17)))
    assert explicit_target([booking],body,clock.now().tzinfo) is booking
    second = assign(volunteer,make_shift('Greeter',starts=booking.shift.starts_at))
    assert explicit_target([booking,second],body,clock.now().tzinfo) is None
    assert explicit_target([booking],body.replace('Greeter','Usher'),clock.now().tzinfo,role_names=['Greeter','Usher']) is None


@pytest.mark.parametrize('body',[
    'Cancel Greeter Sunday October 18, 2027',
    'Cancel Greeter Sunday October 25',
])
def test_calendar_conflict_clarifies_and_preserves_sole_booking(session,clock,provider,make_volunteer,make_shift,assign,body):
    volunteer = make_volunteer(prefs={'onboarding_stage':'complete'})
    booking = assign(volunteer,make_shift('Greeter',starts=clock.now()+timedelta(days=17)))
    result = route(session,clock,provider,volunteer,ExactGloo(),body)
    assert result.routed_to == 'cancellation_review' and booking.status == 'approved'
    assert session.scalar(select(m.FillRequest)) is None
    assert len(provider.sent_to(volunteer.phone)) == 1
    assert 'No schedule changes' in provider.sent_to(volunteer.phone)[0].body


@pytest.mark.parametrize('body',["I can't make it","I can’t make it"])
def test_generic_cancel_without_bookings_gets_one_factual_gloo_reply(session,clock,provider,make_volunteer,body):
    volunteer = make_volunteer(prefs={'onboarding_stage':'complete'})
    gloo = ExactGloo()
    result = route(session,clock,provider,volunteer,gloo,body)
    assert result.routed_to == 'cancellation_review'
    assert not session.scalar(select(m.Assignment)) and not session.scalar(select(m.FillRequest))
    replies = provider.sent_to(volunteer.phone)
    assert len(replies) == len(gloo.calls) == 1
    assert "couldn't find a current upcoming booking" in replies[0].body
    assert 'No schedule changes' in replies[0].body


@pytest.mark.parametrize('body',[
    'Cancel Greeter Wednesday',
    "I can't make tomorrow",
    'Cancel Greeter next Sunday',
    'Cancel Greeter Sunday 2027',
])
def test_explicit_day_hint_cannot_fall_back_to_sole_booking(session,clock,provider,make_volunteer,make_shift,assign,body):
    volunteer = make_volunteer(prefs={'onboarding_stage':'complete'})
    booking = assign(volunteer,make_shift('Greeter',starts=clock.now()+timedelta(days=10)))
    result = route(session,clock,provider,volunteer,ExactGloo(),body)
    assert result.routed_to == 'cancellation_review' and booking.status == 'approved'
    assert not session.scalar(select(m.FillRequest))
    assert len(provider.sent_to(volunteer.phone)) == 1
    assert 'No schedule changes' in provider.sent_to(volunteer.phone)[0].body


def test_undated_generic_cancel_can_still_cancel_sole_booking(session,clock,provider,make_volunteer,make_shift,assign):
    volunteer = make_volunteer(prefs={'onboarding_stage':'complete'})
    booking = assign(volunteer,make_shift('Greeter',starts=clock.now()+timedelta(days=10)))
    result = route(session,clock,provider,volunteer,ExactGloo(),"I can't make it")
    assert result.routed_to == 'fill_agent' and booking.status == 'cancelled'
    assert len(provider.sent_to(volunteer.phone)) == 1
    assert 'booking has been cancelled' in provider.sent_to(volunteer.phone)[0].body


@pytest.mark.parametrize('body',['Cancel Production','Cancel Unknown Ministry'])
def test_qualified_cancel_cannot_fall_back_to_sole_other_role(session,clock,provider,make_volunteer,make_shift,assign,body):
    volunteer = make_volunteer(prefs={'onboarding_stage':'complete'})
    booking = assign(volunteer,make_shift('Greeter',starts=clock.now()+timedelta(days=10)))
    make_shift('Production',starts=clock.now()+timedelta(days=17))
    result = route(session,clock,provider,volunteer,ExactGloo(),body)
    assert result.routed_to == 'cancellation_review' and booking.status == 'approved'
    assert not session.scalar(select(m.FillRequest))
    assert len(provider.sent_to(volunteer.phone)) == 1
    assert 'No schedule changes' in provider.sent_to(volunteer.phone)[0].body


def test_no_bookings_mac_pipeline_acknowledges_before_factual_reply(progress_app):
    body = "I can't make it"
    with progress_app.state.session_factory() as session:
        person = session.scalar(select(m.Volunteer))
        person.preferences = {**person.preferences,'onboarding_stage':'complete'}
        session.commit()
    gloo = progress_app.state.gloo
    original = gloo.create_response
    def classify_or_compose(**kwargs):
        if kwargs['input'] == body:
            return SimpleNamespace(output_text=json.dumps({'intent':'cancel','confidence':.99}))
        return original(**kwargs)
    gloo.create_response = classify_or_compose
    with TestClient(progress_app) as client:
        accepted = post(client,'/mac/inbound',incoming('synthetic-empty-bookings',body)).json()
        assert accepted['progress_state'] == 'waiting_ack'
        with progress_app.state.session_factory() as session:
            job = session.get(m.Notification,accepted['progress_key'])
            assert job.detail['bookings'] == []
            assert len(session.scalars(select(m.Message).where(m.Message.direction=='out')).all()) == 1
        submit_ack(client,mac_progress.SCHEDULE_ACK_TEXT)
        post(client,'/mac/progress/tick'); wait_worker(progress_app)
        final = post(client,'/mac/outbound/pull').json()['messages']
        assert len(final) == 1 and "couldn't find a current upcoming booking" in final[0]['body']
        assert 'No schedule changes' in final[0]['body']
        assert post(client,f"/mac/outbound/{final[0]['id']}/verify",{'token':final[0]['token']}).status_code == 200
        with progress_app.state.session_factory() as session:
            assert not session.scalar(select(m.Assignment)) and not session.scalar(select(m.FillRequest))
            assert session.get(m.Notification,accepted['progress_key']).state == 'done'
