"""Factual replies use only synthetic records and never deliver real texts."""
import json
from datetime import timedelta
from types import SimpleNamespace
import pytest
from sqlalchemy import select
from app.core.inbound import handle_inbound
from app.agents.fill_agent import FillContext
from app.db import models as m
from app.llm.parser import ParsedMessage
from app.config import Settings
from tests.test_mac_messages import mac_app, PHONE, post, exact_manual_gloo  # noqa: F401


class ExactGloo:
    settings = Settings(gloo_signup_replies=True)
    def __init__(self):
        self.calls = []
        self.fail = False
    def create_response(self, **kwargs):
        from app.llm.gloo_client import GlooUnavailableError
        self.calls.append(kwargs)
        if self.fail:
            raise GlooUnavailableError('Synthetic outage')
        return SimpleNamespace(output_text=json.loads(kwargs['input'])['approved_message'])


def ask(session, clock, provider, volunteer, gloo, body='Are there any other opportunities I can help with?'):
    return handle_inbound(session, clock, provider, volunteer.phone, body,
        lambda _: pytest.fail('A clear opportunity question must precede classification'),
        ctx=FillContext(session, clock, provider, gloo))


def test_actual_open_option_is_answered_without_new_booking(session, clock, provider, make_volunteer, make_shift):
    volunteer = make_volunteer(prefs={'onboarding_stage': 'complete', 'interested_roles': ['usher'], 'max_per_month': 2})
    shift = make_shift()
    # Same event/role has two available slots but is one presented option.
    session.add(m.Shift(event_id=shift.event_id, role_id=shift.role_id, slot_index=1)); session.flush()
    gloo = ExactGloo()
    result = ask(session, clock, provider, volunteer, gloo)
    assert result.routed_to == 'booking_status'
    assert len(provider.sent) == len(gloo.calls) == 1
    body = provider.sent[0].body
    assert 'usher at Sunday Service' in body and 'openings, not bookings' in body
    facts = json.loads(gloo.calls[0]['input'])
    assert facts['exact_copy'] and len(facts['schedule_context']['schedule']['opportunities']['eligible_open_shifts']) == 1
    assert not session.scalar(select(m.Assignment)) and volunteer.preferences['max_per_month'] == 2


def test_monthly_cap_qualification_unavailable_and_occupied_all_filter(session, clock, provider, make_volunteer, make_shift, assign):
    volunteer = make_volunteer(prefs={'onboarding_stage': 'complete', 'interested_roles': ['usher', 'Production'], 'max_per_month': 2})
    assign(volunteer, make_shift())
    assign(volunteer, make_shift(starts=clock.now() + timedelta(days=10)))
    make_shift(starts=clock.now() + timedelta(days=17))  # Cap reached in October.
    make_shift(role_name='Production', required=['sound_training'], starts=clock.now() + timedelta(days=38))
    make_shift(starts=clock.now() + timedelta(days=66))  # December absence.
    session.add(m.Availability(volunteer_id=volunteer.id, month='2026-12', available_dates=[], unavailable_dates=['2026-12-06']))
    assign(make_volunteer(), make_shift(starts=clock.now() + timedelta(days=45)))
    gloo = ExactGloo()
    ask(session, clock, provider, volunteer, gloo)
    assert len(provider.sent) == 1
    body = provider.sent[0].body
    assert 'limit is 2 per month' in body and '2 recorded for October 2026' in body
    assert "couldn't find additional open shifts" in body
    assert len(session.scalars(select(m.Assignment)).all()) == 3
    assert volunteer.preferences['max_per_month'] == 2


def test_future_month_option_remains_possible_after_current_month_cap(session, clock, provider, make_volunteer, make_shift, assign):
    volunteer = make_volunteer(prefs={'onboarding_stage': 'complete', 'interested_roles': ['usher'], 'max_per_month': 1})
    assign(volunteer, make_shift())
    make_shift(starts=clock.now() + timedelta(days=17))
    next_month = make_shift(starts=clock.now() + timedelta(days=38))
    gloo = ExactGloo(); ask(session, clock, provider, volunteer, gloo)
    options = json.loads(gloo.calls[0]['input'])['schedule_context']['schedule']['opportunities']['eligible_open_shifts']
    assert [x['shift_id'] for x in options] == [next_month.id]
    assert volunteer.preferences['max_per_month'] == 1


def test_invalid_saved_frequency_gets_clarification_without_invented_openings(session, clock, provider, make_volunteer, make_shift):
    volunteer = make_volunteer(prefs={'onboarding_stage': 'complete', 'max_per_month': 'unknown'})
    make_shift()
    gloo = ExactGloo(); ask(session, clock, provider, volunteer, gloo)
    assert len(gloo.calls) == len(provider.sent) == 1
    assert 'Could you clarify how often' in provider.sent[0].body
    assert volunteer.preferences['max_per_month'] == 'unknown' and not session.scalar(select(m.Assignment))


def test_outage_retries_fresh_options_and_deduplicates(session, clock, provider, make_volunteer, make_shift, assign):
    from app.core.booking_status import reply
    from app.core.notifications import flush_due
    from app.core.send_gate import SendGate
    volunteer = make_volunteer(prefs={'onboarding_stage': 'complete', 'interested_roles': ['usher'], 'max_per_month': 2})
    shift = make_shift()
    gloo = ExactGloo(); gloo.fail = True
    ask(session, clock, provider, volunteer, gloo)
    notice = session.scalar(select(m.Notification).where(m.Notification.purpose == 'booking_status'))
    assert notice.state == 'pending' and not provider.sent
    gate = SendGate(session, clock, provider); gate.reply_to_message_id = notice.detail['reply_id']
    assert reply(session, clock, gate, volunteer, gloo) is notice and len(gloo.calls) == 1
    assign(make_volunteer(), shift)
    gloo.fail = False; clock.set_time(notice.due_at)
    flush_due(FillContext(session, clock, provider, gloo))
    assert len(provider.sent) == 1 and "couldn't find additional open shifts" in provider.sent[0].body
    flush_due(FillContext(session, clock, provider, gloo)); assert len(provider.sent) == 1


@pytest.mark.parametrize('body', ['How can I help?', 'Can I volunteer more?', 'Any open shifts next month?', 'What other roles are available?'])
def test_natural_opportunity_questions_precede_wrong_parser_route(session, clock, provider, make_volunteer, body):
    volunteer = make_volunteer(prefs={'onboarding_stage': 'complete'})
    gloo = ExactGloo(); ask(session, clock, provider, volunteer, gloo, body)
    assert len(provider.sent) == len(gloo.calls) == 1


@pytest.mark.parametrize('change', ['unchanged', 'occupied', 'cap'])
def test_native_claim_requires_current_opportunity_facts(mac_app, change):
    from fastapi.testclient import TestClient
    from app.integrations.mac_models import MacDeliveryClaim
    clock = mac_app.state.clock
    gloo = exact_manual_gloo(mac_app)
    with mac_app.state.session_factory() as session:
        volunteer = session.scalar(select(m.Volunteer))
        volunteer.preferences = {'onboarding_stage': 'complete', 'interested_roles': ['Greeter'], 'max_per_month': 2}
        event = m.Event(title='Sunday Service', starts_at=clock.now()+timedelta(days=3), ends_at=clock.now()+timedelta(days=3, hours=1), status='scheduled')
        role = m.Role(name='Greeter', ministry='Welcome', required_qualifications=[], criticality='standard', fill_policy='auto')
        session.add_all([event, role]); session.flush()
        shift = m.Shift(event_id=event.id, role_id=role.id, slot_index=0); session.add(shift); session.flush()
        session.info['mac_test_session'] = mac_app.state.provider.test_sessions[PHONE]
        ask(session, clock, mac_app.state.provider, volunteer, gloo)
        message = session.scalar(select(m.Message).where(m.Message.direction == 'out'))
        ident = message.id
        assert message.status == 'queued'
        if change == 'occupied':
            session.add(m.Assignment(shift_id=shift.id, volunteer_id=volunteer.id, status='approved', source='manual', created_at=clock.now(), updated_at=clock.now()))
        elif change == 'cap':
            volunteer.preferences = {**volunteer.preferences, 'max_per_month': 1}
        session.commit()
    with TestClient(mac_app) as client:
        pulled = post(client, '/mac/outbound/pull').json()['messages']
        assert bool(pulled) == (change == 'unchanged')
    with mac_app.state.session_factory() as session:
        if change != 'unchanged':
            assert session.get(MacDeliveryClaim, ident) is None
