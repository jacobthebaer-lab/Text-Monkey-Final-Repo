"""Scoped ordinary clarification, with synthetic Gloo and native queues only."""
from datetime import timedelta
from types import SimpleNamespace
import pytest
from sqlalchemy import select
from app.agents.fill_agent import FillContext
from app.core.inbound import handle_inbound
from app.core.ordinary_reply import reply
from app.core.notifications import flush_due
from app.core.send_gate import SendGate, SendStatus
from app.db import models as m
from app.llm.parser import ParsedMessage
from tests.test_opportunities_reply import ExactGloo
from tests.test_mac_messages import mac_app, PHONE, post, exact_manual_gloo  # noqa: F401


def receive(session, clock, provider, volunteer, gloo, body='Can you explain that?', parsed=None):
    return handle_inbound(session, clock, provider, volunteer.phone, body,
        lambda _: parsed or ParsedMessage(intent='question', confidence=.99),
        ctx=FillContext(session, clock, provider, gloo))


def test_first_unknown_and_repeated_unclear_both_get_truthful_gloo_reply(session, clock, provider, make_volunteer):
    volunteer = make_volunteer(prefs={'onboarding_stage': 'complete'})
    gloo = ExactGloo()
    first = receive(session, clock, provider, volunteer, gloo)
    second = receive(session, clock, provider, volunteer, gloo, body='Still not sure what that means')
    assert first.routed_to == 'clarify' and second.routed_to == 'escalated_unclear'
    assert len(provider.sent) == len(gloo.calls) == 2
    assert 'which role or event' in provider.sent[0].body
    assert 'recorded your message for your coordinator to review' in provider.sent[1].body
    review = session.get(m.Escalation, second.escalation_id)
    assert review.related_ids['message_id'] == session.scalars(select(m.Message.id).where(m.Message.direction == 'in').order_by(m.Message.id.desc())).first()
    assert not session.scalar(select(m.Assignment)) and volunteer.preferences == {'onboarding_stage': 'complete'}


def test_polite_ack_does_not_count_as_prior_clarification(session, clock, provider, make_volunteer):
    volunteer = make_volunteer(prefs={'onboarding_stage': 'complete'})
    gloo = ExactGloo()
    receive(session, clock, provider, volunteer, gloo, 'Thank you!')
    result = receive(session, clock, provider, volunteer, gloo, 'Where do I go?')
    assert "You're welcome" in provider.sent[0].body
    assert result.routed_to == 'clarify' and not session.scalar(select(m.Escalation))
    assert len(gloo.calls) == len(provider.sent) == 2


def test_no_parser_result_gets_review_ack_without_guessing(session, clock, provider, make_volunteer):
    volunteer = make_volunteer(prefs={'onboarding_stage': 'complete'})
    gloo = ExactGloo()
    result = receive(session, clock, provider, volunteer, gloo, parsed=ParsedMessage(intent='unclear', parse_error=True))
    assert result.routed_to == 'escalated_unclear' and len(provider.sent) == 1
    assert 'coordinator to review' in provider.sent[0].body
    assert not session.scalar(select(m.Assignment))


def test_outage_survives_three_failures_and_deduplicates(session, clock, provider, make_volunteer):
    volunteer = make_volunteer(prefs={'onboarding_stage': 'complete'})
    gloo = ExactGloo(); gloo.fail = True
    receive(session, clock, provider, volunteer, gloo)
    notice = session.scalar(select(m.Notification).where(m.Notification.key.startswith('ordinary-reply:')))
    gate = SendGate(session, clock, provider); gate.reply_to_message_id = notice.detail['reply_id']; gate.gloo = gloo
    assert reply(session, clock, gate, volunteer) is notice and len(gloo.calls) == 1
    for _ in range(3):
        clock.set_time(notice.due_at); flush_due(FillContext(session, clock, provider, gloo))
    assert notice.state == 'pending' and not provider.sent
    assert len(session.scalars(select(m.Escalation).where(m.Escalation.category == 'system_error')).all()) == 1
    gloo.fail = False; clock.set_time(notice.due_at)
    flush_due(FillContext(session, clock, provider, gloo))
    assert notice.state == 'sent' and len(provider.sent) == 1


@pytest.mark.parametrize('change', ['opt_out', 'incoming_body', 'review_resolved'])
def test_retry_respects_changed_sender_source_or_review(session, clock, provider, make_volunteer, change):
    volunteer = make_volunteer(prefs={'onboarding_stage': 'complete'})
    gloo = ExactGloo(); gloo.fail = True
    parsed = ParsedMessage(intent='unclear', parse_error=True) if change == 'review_resolved' else None
    result = receive(session, clock, provider, volunteer, gloo, parsed=parsed)
    notice = session.scalar(select(m.Notification).where(m.Notification.key.startswith('ordinary-reply:')))
    if change == 'opt_out': volunteer.sms_opt_in = False
    elif change == 'incoming_body': session.get(m.Message, notice.detail['reply_id']).body = 'A changed source'
    else: session.get(m.Escalation, result.escalation_id).status = 'closed'
    gloo.fail = False; clock.set_time(notice.due_at)
    flush_due(FillContext(session, clock, provider, gloo))
    assert not provider.sent and len(gloo.calls) == 1 and notice.state == 'blocked_policy'


def test_sensitive_and_stop_keep_their_existing_handlers(session, clock, provider, make_volunteer):
    volunteer = make_volunteer(prefs={'onboarding_stage': 'complete'})
    gloo = ExactGloo()
    receive(session, clock, provider, volunteer, gloo, 'I feel depressed', ParsedMessage(intent='question', sensitive=True, confidence=.99))
    assert not provider.sent and not gloo.calls
    assert session.scalar(select(m.Escalation).where(m.Escalation.category == 'sensitive'))
    assert not session.scalar(select(m.Notification).where(m.Notification.key.startswith('ordinary-reply:'), m.Notification.state == 'sent'))
    other = make_volunteer(prefs={'onboarding_stage': 'complete'})
    result = receive(session, clock, provider, other, gloo, 'STOP')
    assert result.routed_to == 'stop' and not other.sms_opt_in
    assert not session.scalar(select(m.Notification).where(m.Notification.key.startswith('ordinary-reply:'), m.Notification.volunteer_id == other.id))


def test_exact_review_retained_and_nonessential_unsolicited_chatter_still_blocked(session, clock, provider, make_volunteer):
    from app.core import confirmations
    volunteer = make_volunteer(prefs={'onboarding_stage': 'complete'})
    session.info[confirmations.MODE_KEY] = True
    gloo = ExactGloo(); receive(session, clock, provider, volunteer, gloo)
    assert len(gloo.calls) == 1 and not provider.sent
    approval = session.scalar(select(m.Approval).where(m.Approval.kind == 'confirm_text'))
    assert approval is not None
    gate = SendGate(session, clock, provider)
    assert gate.send(body='Unsolicited clarification', purpose='signup_reply', volunteer=volunteer).status == SendStatus.BLOCKED_POLICY
    confirmations.decide(session, gate, approval, approve=True, actor='admin@example.test', expected=approval.payload['content_hash'], now=clock.now())
    assert len(provider.sent) == 1


@pytest.mark.parametrize('change', ['unchanged', 'source', 'phone'])
def test_native_pull_requires_same_original_ordinary_input(mac_app, change):
    from fastapi.testclient import TestClient
    from app.integrations.mac_models import MacDeliveryClaim
    clock = mac_app.state.clock; gloo = exact_manual_gloo(mac_app)
    with mac_app.state.session_factory() as session:
        volunteer = session.scalar(select(m.Volunteer)); volunteer.preferences = {'onboarding_stage': 'complete'}
        session.info['mac_test_session'] = mac_app.state.provider.test_sessions[PHONE]
        receive(session, clock, mac_app.state.provider, volunteer, gloo)
        outgoing = session.scalar(select(m.Message).where(m.Message.direction == 'out'))
        assert outgoing.status == 'queued' and len(gloo.calls) == 1
        ident = outgoing.id
        if change == 'source': session.scalar(select(m.Message).where(m.Message.direction == 'in')).body = 'Altered after review'
        elif change == 'phone': volunteer.phone = '+15555550102'
        session.commit()
    with TestClient(mac_app) as client:
        assert bool(post(client, '/mac/outbound/pull').json()['messages']) == (change == 'unchanged')
    with mac_app.state.session_factory() as session:
        if change != 'unchanged': assert session.get(MacDeliveryClaim, ident) is None
