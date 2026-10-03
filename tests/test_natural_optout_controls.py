"""Synthetic natural withdrawal, no model, native transport, or external calls."""
import pytest
from sqlalchemy import select

from app.agents.fill_agent import FillContext
from app.core.consent_controls import control_action
from app.core.inbound import handle_inbound
from app.db import models as m
from app.llm.parser import ParsedMessage

POSITIVE = [
    'please stop texting me, thanks',
    'stop texting me. I have cancer',
    "Don't text me anymore, thank you.",
    'Please remove me from your texting list; I have too many messages.',
    'I withdraw my consent to texting because I need a break.',
    'STOP, please.',
    'Please stop messaging me! Thank you.',
    'Stop texting me. Too many messages.',
    'Thanks, please stop texting me.',
    'Stop texting me please.',
    'Please stop texting me, I would appreciate it.',
    'Please stop texting me because I might be away for a while.',
    'Stop texting me because my partner asked me to reduce distractions.',
    'Stop texting me, I said this yesterday.',
    'Please stop texting me, I never asked for these messages.',
    'Stop texting me because I never wanted text reminders.',
    'Stop texting me, I never asked for that many messages.',
]
NEGATIVE = [
    "don't stop texting me",
    'there was no stop request',
    '"stop texting me"',
    '“please stop texting me, thanks”',
    'My friend said stop texting me.',
    'Stop texting me, she said.',
    'Stop texting me. That is what she said.',
    "Stop texting me. That's an example of a request.",
    'Stop texting me, said my friend.',
    'Stop texting me. That was just an example.',
    "Stop texting me. Actually, don't stop texting me.",
    "Stop texting me. Actually, I didn't mean that.",
    "Stop texting me. I didn't ask you to stop texting me.",
    'Stop texting me: those are the words I was told to use.',
    'Stop texting me, I never asked that.',
    'If I say stop texting me, what happens?',
    'Stop texting me, if I am unavailable.',
    'Stop texting me, except scheduled reminders.',
    'Stop texting me, only about the Greeter role.',
    'Please stop texting me about my Greeter shift.',
    'Please stop texting me, about my Greeter shift.',
    'Please cancel my Greeter shift, thanks.',
    'Stop texting me. Keep sending scheduled reminders.',
    'Thanks, START',
]


class NeverGloo:
    def create_response(self, **kwargs):
        pytest.fail('Control handling must not call Gloo')


def queued(session, clock, phone, status='queued'):
    row = m.Message(direction='out', phone=phone, body='Synthetic prior question.',
        purpose='signup_reply', kind='ai', status=status, created_at=clock.now())
    session.add(row)
    session.flush()
    return row


@pytest.mark.parametrize('body', POSITIVE)
@pytest.mark.parametrize('known', [False, True])
def test_clear_withdrawal_is_durable_before_model_and_suppresses_queue(
    session, clock, provider, make_volunteer, body, known
):
    volunteer = make_volunteer(prefs={'signup_source':'sms',
        'consent_pending':True, 'onboarding_stage':'availability'}) if known else None
    if volunteer:
        volunteer.phone = '+12025550148'
    phone = volunteer.phone if known else '+12025550148'
    waiting = queued(session, clock, phone)
    claimed = queued(session, clock, phone, 'dispatching')
    other = queued(session, clock, '+12025550149')
    session.add(m.Policy(key='sms_opt_out:'+phone, value={'value':False}))
    review = m.Approval(kind='confirm_text', status='pending', requested_at=clock.now(),
        payload={'phone':phone,'purpose':'signup_reply'})
    session.add(review)
    session.commit()
    ctx = FillContext(session, clock, provider, NeverGloo())
    def never_parse(_):
        pytest.fail('Explicit withdrawal must precede model and sensitive classification')
    for _ in range(2):
        result = handle_inbound(session, clock, provider, phone, body, never_parse,
            ctx=ctx, allow_signup=True)
        session.commit()
        assert result.routed_to == 'stop'
        assert session.get(m.Policy, 'sms_opt_out:'+phone).value['value'] is True
    for row in (waiting, claimed):
        session.refresh(row)
        assert row.status == 'blocked_opt_out'
    session.refresh(other)
    assert other.status == 'queued'
    assert review.status == 'expired'
    if known:
        assert not volunteer.sms_opt_in and not volunteer.preferences['consent_pending']
    controls = session.scalars(select(m.Notification).where(m.Notification.purpose=='stop_confirm')).all()
    assert len(controls) == (1 if known else 0)
    assert all(row.state=='pending' and row.message_id is None for row in controls)
    assert not provider.sent


@pytest.mark.parametrize('body', NEGATIVE)
def test_mentions_negation_and_role_cancellation_do_not_withdraw_consent(
    session, clock, provider, make_volunteer, body
):
    volunteer = make_volunteer()
    volunteer.phone = '+12025550148'
    waiting = queued(session, clock, volunteer.phone)
    ctx = FillContext(session, clock, provider, NeverGloo())
    parsed = []
    def parser(text):
        parsed.append(text)
        return ParsedMessage(intent='question', confidence=1)
    result = handle_inbound(session, clock, provider, volunteer.phone, body, parser, ctx=ctx)
    session.commit()
    assert control_action(body) is None and result.routed_to != 'stop'
    assert parsed == [body] and volunteer.sms_opt_in
    assert session.get(m.Policy, 'sms_opt_out:'+volunteer.phone) is None
    session.refresh(waiting)
    assert waiting.status == 'queued'
    assert not session.scalar(select(m.Notification).where(m.Notification.purpose=='stop_confirm'))
    assert not provider.sent
