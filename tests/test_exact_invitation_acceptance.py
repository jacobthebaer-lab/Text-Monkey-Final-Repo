"""Invitation proof must bind the actual recorded reply, not caller/model claims."""
import pytest

from app.core.signup_copy import WELCOME, delivered_exact_invitation
from app.db import models as m
from tests.test_concise_signup import PHONE


@pytest.mark.parametrize('defect', ['sender', 'body', 'direction'])
def test_other_recorded_message_cannot_authorize_name_reply(session, clock, defect):
    invitation = m.Message(phone=PHONE, direction='out', body=WELCOME,
        purpose='signup_reply', kind='ai', status='submitted', created_at=clock.now())
    session.add(invitation)
    session.flush()
    reply = m.Message(phone=PHONE, direction='in', body='Alex Example',
        purpose='inbound', kind='ai', status='received', created_at=clock.now())
    if defect == 'sender':
        reply.phone = '+12025550191'
    elif defect == 'body':
        reply.body = 'Someone Else'
    else:
        reply.direction = 'out'
    session.add(reply)
    session.flush()
    assert not delivered_exact_invitation(session, clock, PHONE,
        reply_message_id=reply.id, body='Alex Example')
