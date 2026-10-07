"""One Gloo clarification for an actual ordinary input, never proactive chatter."""
from datetime import timedelta
import hashlib
import re
from sqlalchemy import select
from app.db import models as m
from app.core.booking_status import session_binding


def binding(session, volunteer, key, now):
    from app.core.conversation import scope
    from app.core.privacy import safe_message_history
    from app.llm.parser import keyword_sensitive
    session.flush()
    selected = session.info.get('mac_test_session')
    row = session.get(m.Notification, key, populate_existing=True) if isinstance(key, str) else None
    if (not row or not key.startswith('ordinary-reply:') or row.purpose != 'signup_reply'
            or row.state not in {'pending', 'awaiting_approval', 'sent'} or not volunteer
            or row.volunteer_id != volunteer.id or not row.expires_at or row.expires_at <= now):
        return None
    volunteer = session.get(m.Volunteer, volunteer.id, populate_existing=True)
    if (not volunteer or volunteer.status != 'active' or not volunteer.sms_opt_in
            or (volunteer.preferences or {}).get('onboarding_stage') not in {None, 'complete'}
            or (volunteer.preferences or {}).get('consent_pending')):
        return None
    incoming = session.scalar(scope(select(m.Message), selected).where(m.Message.id == row.detail.get('reply_id')).execution_options(populate_existing=True))
    if (not incoming or incoming.direction != 'in' or incoming.status != 'received'
            or incoming.phone != volunteer.phone or incoming.volunteer_id != volunteer.id
            or not incoming.body.strip() or not timedelta(0) <= now-incoming.created_at < timedelta(days=2)
            or row.detail.get('session_scope') != session_binding(selected)
            or row.detail.get('input_hash') != hashlib.sha256(incoming.body.encode()).hexdigest()
            or (selected and not selected.active(now)) or keyword_sensitive(incoming.body)
            or incoming.body.strip().upper() in {'STOP', 'START', 'HELP'}
            or not safe_message_history(session, [incoming])):
        return None
    review_id = row.detail.get('review_escalation_id')
    if review_id is not None:
        review = session.get(m.Escalation, review_id, populate_existing=True)
        if (not review or review.category not in {'unclear', 'system_error'}
                or review.status not in {'open', 'acknowledged'}
                or review.related_ids.get('volunteer_id') != volunteer.id
                or review.related_ids.get('message_id') != incoming.id):
            return None
    return {'notification_key': key, 'reply_id': incoming.id, 'session_scope': session_binding(selected),
        'input_hash': hashlib.sha256(incoming.body.encode()).hexdigest(), 'name': volunteer.name,
        'phone': volunteer.phone, 'volunteer_id': volunteer.id, 'review_escalation_id': review_id,
        'thanks': bool(re.fullmatch(r"(?:thanks|thank you|thankyou|ok|okay|got it)(?: so much)?[!.\s]*", incoming.body.strip(), re.I))}


def copy_for(facts):
    name = facts['name'].split()[0]
    if facts['review_escalation_id'] is not None:
        return f"Thanks, {name}! I've recorded your message for your coordinator to review. No schedule changes have been made."
    if facts['thanks']:
        return f"You're welcome, {name}! Let me know if you need help with your schedule or availability."
    return f"Thanks, {name}! Could you tell me which role or event you mean? I can help with your schedule and availability."


def reply(session, clock, gate, volunteer, *, review_escalation_id=None):
    from app.agents.fill_agent import FillContext
    from app.core.notifications import _dispatch
    reply_id = gate.reply_to_message_id
    key = f'ordinary-reply:{reply_id}'
    existing = session.get(m.Notification, key)
    if existing is not None:
        return existing
    incoming = session.get(m.Message, reply_id) if reply_id else None
    if not incoming:
        return None
    if review_escalation_id is not None:
        review = session.get(m.Escalation, review_escalation_id)
        if review is not None:
            review.related_ids = {**review.related_ids, 'message_id': reply_id}
    row = m.Notification(key=key, volunteer_id=volunteer.id, purpose='signup_reply', body='', state='pending',
        due_at=clock.now(), created_at=clock.now(), expires_at=incoming.created_at+timedelta(days=2),
        detail={'reply_id': reply_id, 'session_scope': session_binding(session.info.get('mac_test_session')),
            'input_hash': hashlib.sha256(incoming.body.encode()).hexdigest(),
            'review_escalation_id': review_escalation_id, 'conversation': {'ordinary_reply': key}})
    session.add(row); session.flush()
    facts = binding(session, volunteer, key, clock.now())
    if facts is None:
        row.state = 'blocked_policy'
        row.detail = {**row.detail, 'reason': 'Ordinary reply requires its original safe sender input'}
        return row
    row.body = copy_for(facts)
    _dispatch(FillContext(session, clock, gate.provider, getattr(gate, 'gloo', None), reply_to_message_id=reply_id), row)
    return row
