"""Conservative sender-owned texting controls, independent of model output."""
import re
from datetime import datetime, timedelta
from sqlalchemy import select
from app.db import models as m

STOP_WORDS = {'STOP', 'STOPALL', 'UNSUBSCRIBE', 'QUIT', 'END', 'REVOKE', 'OPT OUT', 'OPTOUT'}
START_WORDS = {'START', 'UNSTOP'}


def control_action(body):
    """Only an actual direct withdrawal qualifies; quotes/hypotheticals do not."""
    text = body.strip().replace('’', "'")
    word = text.upper().rstrip('.!')
    if word in STOP_WORDS:
        return 'stop'
    if word in START_WORDS:
        return 'start'
    patterns = (
        r'(?:please\s+)?(?:stop|quit)\s+(?:texting|messaging|contacting)\s+me(?:\s+(?:anymore|from now on))?',
        r'(?:please\s+)?(?:do not|don\'t)\s+(?:text|message|contact)\s+me(?:\s+(?:again|anymore|from now on))?',
        r'i\s+(?:do not|don\'t)\s+want\s+(?:any\s+)?(?:more\s+)?(?:texts|text messages|messages)(?:\s+(?:from you|anymore))?',
        r'(?:please\s+)?(?:remove|unsubscribe)\s+me\s+from\s+(?:your|the)\s+(?:text|texting|messaging)\s+list',
        r'(?:please\s+)?(?:cancel|stop)\s+(?:my|all|these|your)\s+(?:texts|text messages|messages)',
        r'i\s+(?:withdraw|revoke)\s+(?:my\s+)?consent\s+(?:to|for)\s+(?:texts|texting|text messages)',
    )
    if any(re.fullmatch(pattern+r'[.!]*', text, re.I) for pattern in patterns):
        return 'stop'
    return None


def prior_disclosed_consent(session, volunteer):
    """Stored flags alone cannot turn an imported contact into an SMS subscriber."""
    prefs = volunteer.preferences or {}
    source = prefs.get('consent_source')
    if source not in {'sms_name_reply_to_exact_invitation', 'sms_name_and_yes', 'sms_reply'}:
        return False
    try:
        when = datetime.fromisoformat(prefs['consent_at'])
        if when.tzinfo is None:
            return False
    except (KeyError, ValueError, TypeError):
        return False
    from app.core.signup_copy import WELCOME, LEGACY_WELCOME
    replies = session.scalars(select(m.Message).where(m.Message.phone == volunteer.phone,
        m.Message.direction == 'in', m.Message.status == 'received',
        m.Message.created_at >= when-timedelta(minutes=1), m.Message.created_at <= when))
    for reply in replies:
        if source == 'sms_name_reply_to_exact_invitation':
            action = re.fullmatch(r'\s*(?:(?:join|my name is|i am|i\'m)\s+)?'+re.escape(volunteer.name)+r'[.!]?\s*', reply.body, re.I)
            copy = WELCOME
        else:
            action = (reply.body.strip().upper() in {'YES', 'Y'} if source == 'sms_reply' else
                re.fullmatch(r'\s*'+re.escape(volunteer.name)+r'\s*[,;]?\s+(?:YES|Y)[.!]?\s*', reply.body, re.I))
            copy = LEGACY_WELCOME
        if action and session.scalar(select(m.Message.id).where(m.Message.phone == volunteer.phone,
                m.Message.direction == 'out', m.Message.body == copy, m.Message.purpose == 'signup_reply',
                m.Message.status.in_(('sent', 'submitted')), m.Message.id < reply.id,
                m.Message.created_at >= reply.created_at-timedelta(hours=24), m.Message.created_at <= reply.created_at)):
            return True
    return False


def acknowledgement_problem(session, *, purpose, volunteer, phone, body, key, message=None):
    """Only the persisted, Gloo-composed control receipt grants send authority."""
    import hashlib
    row = session.get(m.Notification, key) if isinstance(key, str) and key.startswith('control:') else None
    if (row is None or row.purpose != purpose or volunteer is None or
            row.volunteer_id != volunteer.id or volunteer.phone != phone or
            row.state not in {'pending', 'awaiting_approval', 'sent'} or
            row.detail.get('gloo_body_hash') != hashlib.sha256(body.encode()).hexdigest()):
        return 'Control acknowledgement requires its recorded Gloo composition'
    if message is not None:
        if row.message_id != message.id or message.kind != 'ai':
            return 'Control acknowledgement delivery differs from its recorded source'
    elif row.message_id is not None:
        return 'Control acknowledgement already consumed'
    return None
