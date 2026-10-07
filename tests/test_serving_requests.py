from sqlalchemy import select
from types import SimpleNamespace
import json
from datetime import timedelta
import pytest

from app.config import Settings
from app.core.serving_requests import save_serving_request
from app.core.send_gate import SendGate
from app.db import models as m
from app.llm.parser import ParsedMessage
from tests.test_mac_messages import mac_app, PHONE, post, exact_manual_gloo  # noqa: F401


def incoming(session, clock, volunteer, body):
    row = m.Message(direction='in', phone=volunteer.phone, volunteer_id=volunteer.id,
        body=body, status='received', kind='inbound', created_at=clock.now())
    session.add(row); session.flush()
    return row


class ExactGloo:
    settings = Settings(gloo_signup_replies=True)
    def __init__(self):
        self.calls = []
        self.fail = False
    def create_response(self, **kwargs):
        from app.llm.gloo_client import GlooUnavailableError
        self.calls.append(kwargs)
        if self.fail:
            raise GlooUnavailableError('Unavailable')
        return SimpleNamespace(output_text=json.loads(kwargs['input'])['approved_message'])


def test_request_is_saved_once_for_review_without_role_or_assignment(session, clock, gate, provider, make_volunteer):
    volunteer = make_volunteer()
    body = 'I want to lead Sunday service'
    row = incoming(session, clock, volunteer, body)
    gloo = ExactGloo()
    args = (session, clock, gate, gloo, volunteer, body,
            ParsedMessage(intent="availability", dates=["2026-10-04"]), row.id)
    assert save_serving_request(*args)
    assert not save_serving_request(*args)
    assert volunteer.preferences["serving_requests"][0]["message_id"] == row.id
    assert len(session.scalars(select(m.Escalation)).all()) == 1
    assert len(gloo.calls) == 1 and len(provider.sent) == 1
    assert 'coordinator to review' in provider.sent[0].body
    assert "hasn't changed any assignments" in provider.sent[0].body
    assert not session.scalars(select(m.Assignment)).all()
    assert volunteer.is_coordinator is False
    assert volunteer.qualifications == []


def test_whole_month_saved_before_one_gloo_ack_and_preserves_profile(session, clock, provider, make_volunteer):
    from app.agents.fill_agent import FillContext
    from app.core.inbound import handle_inbound
    volunteer = make_volunteer(prefs={'onboarding_stage': 'complete', 'max_per_month': 2, 'interested_roles': ['Greeter']})
    session.add(m.Availability(volunteer_id=volunteer.id, month='2026-12', available_dates=['2026-12-06'], unavailable_dates=['2026-12-13']))
    gloo = ExactGloo()
    parsed = ParsedMessage(intent='availability', dates=['December'], confidence=.99)
    result = handle_inbound(session, clock, provider, volunteer.phone, "I'm away all December", lambda _: parsed,
        ctx=FillContext(session, clock, provider, gloo))
    assert result.routed_to == 'availability'
    availability = session.scalar(select(m.Availability))
    assert len(availability.unavailable_dates) == 31 and availability.available_dates == []
    assert availability.raw_reply == "I'm away all December"
    item = volunteer.preferences['serving_requests'][0]
    assert item['status'] == 'recorded_unavailable' and item['availability_id'] == availability.id
    assert volunteer.preferences['max_per_month'] == 2 and volunteer.preferences['interested_roles'] == ['Greeter']
    assert not session.scalar(select(m.Assignment)) and not session.scalar(select(m.Escalation))
    assert len(gloo.calls) == len(provider.sent) == 1
    assert 'all of December 2026 as unavailable' in provider.sent[0].body
    assert json.loads(gloo.calls[0]['input'])['exact_copy']


@pytest.mark.parametrize('body', ["I might be away December", "I'm away December 6", "I'm away December except the first week", "Am I unavailable in December?", "I'm away November and December", "I'm away for a day in December", "I'm not unavailable in December", "My friend is away December", "I don't think I'll be away December"])
def test_ambiguous_month_is_retained_for_review_and_acknowledged(session, clock, gate, provider, make_volunteer, body):
    volunteer = make_volunteer()
    row = incoming(session, clock, volunteer, body)
    gloo = ExactGloo()
    save_serving_request(session, clock, gate, gloo, volunteer, body, ParsedMessage(intent='availability', dates=['December']), row.id)
    assert not session.scalar(select(m.Availability))
    assert len(provider.sent) == len(gloo.calls) == 1
    assert 'coordinator to review' in provider.sent[0].body
    assert 'marked all' not in provider.sent[0].body


def test_unavailable_gloo_retries_durably_without_template_or_duplicate(session, clock, gate, provider, make_volunteer):
    from app.core.notifications import flush_due
    from app.agents.fill_agent import FillContext
    volunteer = make_volunteer()
    body = "I won't be here during December"
    row = incoming(session, clock, volunteer, body)
    gloo = ExactGloo(); gloo.fail = True
    args = (session, clock, gate, gloo, volunteer, body, ParsedMessage(intent='availability', dates=['December']), row.id)
    assert save_serving_request(*args) and not save_serving_request(*args)
    notification = session.get(m.Notification, f'availability-followup:{row.id}')
    assert notification.state == 'pending' and notification.detail['gloo_attempts'] == 1
    assert len(gloo.calls) == 1 and not provider.sent
    assert len(session.scalar(select(m.Availability)).unavailable_dates) == 31
    clock.advance(timedelta(minutes=2)); gloo.fail = False
    flush_due(FillContext(session, clock, provider, gloo))
    assert notification.state == 'sent' and len(provider.sent) == 1
    flush_due(FillContext(session, clock, provider, gloo))
    assert len(provider.sent) == 1


def test_saved_dates_changed_before_retry_hold_ack(session, clock, gate, provider, make_volunteer):
    from app.core.notifications import flush_due
    from app.agents.fill_agent import FillContext
    volunteer = make_volunteer()
    body = "I'm unavailable in December"
    row = incoming(session, clock, volunteer, body)
    gloo = ExactGloo(); gloo.fail = True
    save_serving_request(session, clock, gate, gloo, volunteer, body, ParsedMessage(intent='availability'), row.id)
    availability = session.scalar(select(m.Availability)); availability.unavailable_dates = []
    clock.advance(timedelta(minutes=2)); gloo.fail = False
    flush_due(FillContext(session, clock, provider, gloo))
    assert not provider.sent and len(gloo.calls) == 1
    assert session.get(m.Notification, f'availability-followup:{row.id}').state == 'blocked_policy'


def test_input_from_other_person_cannot_authorize_ack(session, clock, gate, provider, make_volunteer):
    volunteer, other = make_volunteer(), make_volunteer()
    body = "I'm away December"
    row = incoming(session, clock, other, body)
    gloo = ExactGloo()
    assert not save_serving_request(session, clock, gate, gloo, volunteer, body, ParsedMessage(intent='availability'), row.id)
    assert not provider.sent and not gloo.calls and not session.scalar(select(m.Availability))


def test_confirmation_mode_keeps_exact_review_and_sender_authority(session, clock, provider, make_volunteer):
    from app.core import confirmations
    from app.core.inbound import handle_inbound
    from app.agents.fill_agent import FillContext
    volunteer = make_volunteer(prefs={'onboarding_stage': 'complete'})
    session.info[confirmations.MODE_KEY] = True
    gloo = ExactGloo()
    handle_inbound(session, clock, provider, volunteer.phone, "I'm away December", lambda _: ParsedMessage(intent='availability', confidence=.99),
        ctx=FillContext(session, clock, provider, gloo))
    assert not provider.sent and len(gloo.calls) == 1
    assert len(session.scalar(select(m.Availability)).unavailable_dates) == 31
    approval = session.scalar(select(m.Approval).where(m.Approval.kind == 'confirm_text'))
    assert approval is not None
    notification = session.scalar(select(m.Notification).where(m.Notification.key.startswith('availability-followup:')))
    assert notification.state == 'awaiting_approval' and notification.detail['approval_id'] == approval.id
    confirmations.decide(session, gate=SendGate(session, clock, provider),
        approval=approval, approve=True, actor='admin@example.test', expected=approval.payload['content_hash'], now=clock.now())
    assert len(provider.sent) == 1


def test_collection_dates_still_save_and_get_truthful_ack(session, clock, gate, provider, make_volunteer):
    volunteer = make_volunteer()
    body = 'I can serve 2026-10-04'
    row = incoming(session, clock, volunteer, body)
    gloo = ExactGloo()
    save_serving_request(session, clock, gate, gloo, volunteer, body,
        ParsedMessage(intent='availability', dates=['2026-10-04']), row.id)
    assert session.scalar(select(m.Availability)).available_dates == ['2026-10-04']
    assert 'saved your availability for October 2026' in provider.sent[0].body


@pytest.mark.parametrize(('body', 'month', 'count'), [
    ("I'm away February", '2027-02', 28),
    ("I'm away February 2028", '2028-02', 29),
    ("I'm unavailable December next year", '2027-12', 31),
    ("I'm unavailable December this year", '2026-12', 31),
])
def test_year_and_month_length_follow_actual_input(clock, body, month, count):
    from app.core.serving_requests import whole_month_absence
    resolved, dates = whole_month_absence(body, clock.now())
    assert resolved == month and len(dates) == count


def test_longer_gloo_outage_reports_once_and_can_eventually_ack(session, clock, gate, provider, make_volunteer):
    from app.core.notifications import flush_due
    from app.agents.fill_agent import FillContext
    volunteer = make_volunteer()
    body = "I'm unavailable December"
    row = incoming(session, clock, volunteer, body)
    gloo = ExactGloo(); gloo.fail = True
    save_serving_request(session, clock, gate, gloo, volunteer, body, ParsedMessage(intent='availability'), row.id)
    notice = session.get(m.Notification, f'availability-followup:{row.id}')
    ctx = FillContext(session, clock, provider, gloo)
    for _ in range(3):
        clock.set_time(notice.due_at); flush_due(ctx)
    assert notice.state == 'pending' and notice.detail['gloo_attempts'] == 4
    assert len(session.scalars(select(m.Escalation).where(m.Escalation.category == 'system_error')).all()) == 1
    assert not provider.sent
    gloo.fail = False; clock.set_time(notice.due_at); flush_due(ctx)
    assert notice.state == 'sent' and len(provider.sent) == 1


def test_opt_out_during_outage_still_blocks_retry(session, clock, gate, provider, make_volunteer):
    from app.core.notifications import flush_due
    from app.agents.fill_agent import FillContext
    volunteer = make_volunteer()
    body = "I'm away December"
    row = incoming(session, clock, volunteer, body)
    gloo = ExactGloo(); gloo.fail = True
    save_serving_request(session, clock, gate, gloo, volunteer, body, ParsedMessage(intent='availability'), row.id)
    volunteer.sms_opt_in = False
    clock.advance(timedelta(minutes=2)); gloo.fail = False
    flush_due(FillContext(session, clock, provider, gloo))
    assert not provider.sent and len(gloo.calls) == 1


@pytest.mark.parametrize('change', ['unchanged', 'dates', 'session'])
def test_mac_queue_rechecks_exact_month_ack_before_native_claim(mac_app, change):
    from app.core.inbound import handle_inbound
    from app.agents.fill_agent import FillContext
    from fastapi.testclient import TestClient
    from app.integrations.mac_models import MacDeliveryClaim
    from dataclasses import replace
    gloo = exact_manual_gloo(mac_app)
    clock = mac_app.state.clock
    with mac_app.state.session_factory() as session:
        volunteer = session.scalar(select(m.Volunteer))
        volunteer.preferences = {'onboarding_stage': 'complete'}
        selected = mac_app.state.provider.test_sessions[PHONE]
        session.info['mac_test_session'] = selected
        handle_inbound(session, clock, mac_app.state.provider, PHONE, "I'm away all December", lambda _: ParsedMessage(intent='availability', dates=['December'], confidence=.99),
            ctx=FillContext(session, clock, mac_app.state.provider, gloo))
        message = session.scalar(select(m.Message).where(m.Message.direction == 'out'))
        assert message.status == 'queued' and len(gloo.calls) == 1
        ident = message.id
        if change == 'dates':
            session.scalar(select(m.Availability)).unavailable_dates = []
        session.commit()
    if change == 'session':
        mac_app.state.provider.test_sessions[PHONE] = replace(selected, id='different-synthetic-session')
    with TestClient(mac_app) as client:
        pulled = post(client, '/mac/outbound/pull').json()['messages']
        assert bool(pulled) == (change == 'unchanged')
    with mac_app.state.session_factory() as session:
        if change != 'unchanged':
            assert session.get(MacDeliveryClaim, ident) is None


def test_changed_gloo_copy_is_held_with_saved_dates_intact(session, clock, gate, provider, make_volunteer):
    volunteer = make_volunteer()
    body = "I'm away December"
    row = incoming(session, clock, volunteer, body)
    gloo = ExactGloo()
    gloo.create_response = lambda **_: SimpleNamespace(output_text='Changed\u2014copy.')
    save_serving_request(session, clock, gate, gloo, volunteer, body, ParsedMessage(intent='availability'), row.id)
    assert not provider.sent and len(session.scalar(select(m.Availability)).unavailable_dates) == 31
    assert session.get(m.Notification, f'availability-followup:{row.id}').state == 'pending'


def test_recorded_assignment_notice_still_composes_through_gloo(session, clock, provider, make_volunteer, make_shift, assign):
    import json
    from app.agents.fill_agent import FillContext
    from app.core.notifications import deliver
    volunteer = make_volunteer()
    assignment = assign(volunteer, make_shift())
    calls = []
    def generate(**kwargs):
        calls.append(kwargs)
        return SimpleNamespace(output_text=json.loads(kwargs["input"])["approved_message"])
    gloo = SimpleNamespace(settings=Settings(gloo_signup_replies=True), create_response=generate)
    ctx = FillContext(session, clock, provider, gloo)
    notice = deliver(ctx, key=f"scheduled-test:{assignment.id}", body="Your recorded shift is scheduled.",
        purpose="confirmation", volunteer=volunteer,
        conversation={"assignment_id":assignment.id,"notice":"scheduled"})
    assert notice.state == "sent" and len(calls) == 1 and len(provider.sent) == 1
    expected = "Hi Test! You're scheduled for usher at Sunday Service 9:00 on Sun Oct 4, 9:00AM MDT. Thank you!"
    assert provider.sent[0].body == expected
    facts = json.loads(calls[0]['input'])
    assert facts['approved_message'] == expected and facts['exact_copy']
    source = facts['schedule_context']['assignment']
    assert source['assignment_id'] == assignment.id
    assert source['role_name'] == 'usher' and source['event_title'] == 'Sunday Service 9:00'
    assert source['starts_at'] == '2026-10-04T15:00:00+00:00'
