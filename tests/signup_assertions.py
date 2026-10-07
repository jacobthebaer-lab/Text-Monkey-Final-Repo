"""Shared acceptance checks for the reviewed saved-preferences acknowledgment."""
from sqlalchemy import select
from app.db import models as m


def completion_text(first_name):
    return f"Thanks, {first_name}! Your volunteer preferences are saved. This update hasn't changed any bookings."


def assert_saved_completion(session, provider, person, total):
    assert len(provider.sent) == total
    body = completion_text(person.name.split()[0])
    assert provider.sent[-1].to == person.phone and provider.sent[-1].body == body
    outputs = session.scalars(select(m.Message).where(m.Message.volunteer_id == person.id,
        m.Message.direction == 'out', m.Message.body == body)).all()
    assert len(outputs) == 1 and outputs[0].status == 'sent'
    notice = session.scalar(select(m.Notification).where(m.Notification.message_id == outputs[0].id,
        m.Notification.key.startswith('ordinary-reply:')))
    assert notice and notice.detail['signup_completion']['step_id'] > 0
    incoming = session.get(m.Message, notice.detail['reply_id'])
    assert incoming.direction == 'in' and incoming.status == 'received'
    assert incoming.phone == person.phone and incoming.volunteer_id == person.id
