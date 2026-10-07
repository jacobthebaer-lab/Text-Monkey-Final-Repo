"""One Gloo clarification for an actual ordinary input, never proactive chatter."""
from datetime import timedelta, timezone
import hashlib
import re
from sqlalchemy import select
from app.db import models as m
from app.core.booking_status import session_binding



def acknowledgment(body):
    text = body.strip().lower().replace('’', "'")
    if re.fullmatch(r"(?:thanks|thank you|thankyou)(?: so much)?[!.\s]*",text):
        return 'thanks'
    if re.fullmatch(r"(?:ok|okay|got it|understood)[!.\s]*",text):
        return 'received'
    if re.fullmatch(r"(?:omw|(?:i'm |i am )?on (?:my|the) way)(?: now)?[!.\s]*",text):
        return 'on_the_way'
    return None

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
    from app.core.policies import PolicyStore
    coordinator_review = bool(row.detail.get('coordinator_review'))
    if coordinator_review and not volunteer.is_coordinator:
        return None
    confirmed_id = row.detail.get('confirmed_assignment_id')
    confirmed = None
    if confirmed_id is not None:
        from app.core.schedule_messages import shift_facts
        assignment = session.get(m.Assignment, confirmed_id, populate_existing=True)
        if assignment:
            session.refresh(assignment.shift)
            session.refresh(assignment.shift.event)
            session.refresh(assignment.shift.role)
        if (not assignment or assignment.volunteer_id != volunteer.id or assignment.status != 'confirmed'
                or assignment.shift.event.status != 'scheduled' or assignment.shift.starts_at <= now
                or assignment.updated_at < incoming.created_at
                or assignment.updated_at.astimezone(timezone.utc).isoformat() != row.detail.get('confirmed_at')):
            return None
        confirmed = shift_facts(assignment.shift)
    ack = acknowledgment(incoming.body)
    completion = row.detail.get('signup_completion')
    if completion is not None:
        from app.core.signup_completion import binding as completion_binding
        completion = completion_binding(session, volunteer, incoming, completion, now)
        if completion is None:
            return None
    facts = {'notification_key': key, 'reply_id': incoming.id, 'session_scope': session_binding(selected),
        'input_hash': hashlib.sha256(incoming.body.encode()).hexdigest(), 'name': volunteer.name,
        'phone': volunteer.phone, 'volunteer_id': volunteer.id, 'review_escalation_id': review_id,
        'thanks': ack is not None or confirmed_id is not None or coordinator_review or completion is not None, 'acknowledgment': ack, 'coordinator_review': coordinator_review,
        'confirmed_assignment_id': confirmed_id, 'confirmed': confirmed,
        'timezone': str(PolicyStore(session).church_tz())}
    if completion is not None:
        facts['signup_completion'] = completion
    return facts


def copy_for(facts):
    name = facts['name'].split()[0]
    if facts.get('signup_completion') is not None:
        return f"Thanks, {name}! Your volunteer preferences are saved. This update hasn't changed any bookings."
    if facts['confirmed'] is not None:
        from app.core.schedule_messages import describe
        from zoneinfo import ZoneInfo
        return f"Hi {name}! You're confirmed for {describe(facts['confirmed'], ZoneInfo(facts['timezone']))}. Thank you!"
    if facts['review_escalation_id'] is not None:
        return f"Thanks, {name}! I've recorded your message for your coordinator to review. No schedule changes have been made."
    if facts['acknowledgment'] == 'received':
        return f"Got it, {name}! I've received your message. No schedule or approval changes have been made."
    if facts['acknowledgment'] == 'on_the_way':
        return f"Thanks, {name}! I've received your on-my-way update. No schedule or approval changes have been made."
    if facts['coordinator_review']:
        return f"Thanks, {name}! Please review exact actions in your signed-in Text Monkey dashboard. Your text did not approve or change any schedule."
    if facts['thanks']:
        return f"You're welcome, {name}! Let me know if you need help with your schedule or availability."
    return f"Thanks, {name}! Could you tell me which role or event you mean? I can help with your schedule and availability."


def reply(session, clock, gate, volunteer, *, review_escalation_id=None, coordinator_review=False, confirmed_assignment=None, signup_completion=None):
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
            'review_escalation_id': review_escalation_id, 'coordinator_review': coordinator_review,
            'confirmed_assignment_id': confirmed_assignment.id if confirmed_assignment else None,
            'confirmed_at': confirmed_assignment.updated_at.astimezone(timezone.utc).isoformat() if confirmed_assignment else None,
            'signup_completion': signup_completion,
            'conversation': {'ordinary_reply': key}})
    session.add(row); session.flush()
    facts = binding(session, volunteer, key, clock.now())
    if facts is None:
        row.state = 'blocked_policy'
        row.detail = {**row.detail, 'reason': 'Ordinary reply requires its original safe sender input'}
        return row
    row.body = copy_for(facts)
    if signup_completion is not None and session.info.get('defer_signup_completion'):
        # Mac progress must commit saved facts and the publisher snapshot before
        # composition. Its replay boundary otherwise rolls those facts back.
        return row
    _dispatch(FillContext(session, clock, gate.provider, getattr(gate, 'gloo', None), reply_to_message_id=reply_id), row)
    return row
