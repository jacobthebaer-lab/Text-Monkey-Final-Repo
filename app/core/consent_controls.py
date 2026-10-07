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
    text = re.sub(r'^(?:thanks|thank you)(?:\s*[,!.]\s*|\s+)', '', text, flags=re.I)
    courteous_word = re.sub(r'^please\s+', '', text, flags=re.I).upper().rstrip('.!')
    if courteous_word in STOP_WORDS:
        return 'stop'
    patterns = (
        r'(?:please\s+)?(?:stop|quit)\s+(?:texting|messaging|contacting)\s+me(?:\s+(?:now|anymore|from now on))?',
        r'(?:please\s+)?(?:do not|don\'t)\s+(?:text|message|contact)\s+me(?:\s+(?:now|again|anymore|from now on))?',
        r'i\s+(?:do not|don\'t)\s+want\s+(?:any\s+)?(?:more\s+)?(?:texts|text messages|messages)(?:\s+(?:from you|anymore))?',
        r'(?:please\s+)?(?:remove|unsubscribe)\s+me\s+from\s+(?:your|the)\s+(?:text|texting|messaging)\s+list',
        r'(?:please\s+)?unsubscribe\s+me',
        r'(?:please\s+)?(?:cancel|stop)\s+(?:my|all|these|your)\s+(?:texts|text messages|messages)',
        r'i\s+(?:withdraw|revoke)\s+(?:my\s+)?consent\s+(?:to|for)\s+(?:texts|texting|text messages)',
    )
    if any(re.fullmatch(pattern+r'[.!]*', text, re.I) for pattern in patterns):
        return 'stop'
    # An independently clear first clause keeps its meaning when the sender
    # adds courtesy or an explanation. Never treat mentions or conditional,
    # reported, quoted, or limited-role requests as global withdrawal.
    tail_guard = (
        r'\b(?:do not|don\'t|never)\s+(?:stop|quit)\s+(?:texting|messaging|contacting)\s+me\b'
        r'|\b(?:keep|continue|still)\s+(?:sending|texting|messaging|contacting)\b'
        r'|\b(?:i|we)\s+(?:never|did not|didn\'t)\s+(?:say|said|ask(?:ed)?|request(?:ed)?|want(?:ed)?|mean|meant)\s+'
        r'(?:(?:for\s+)?(?:that|this|it)(?=\s*(?:[.!?,;:]|$))|(?:you\s+)?to\s+(?:stop|quit)\s+(?:texting|messaging|contacting)|(?:a|the)\s+(?:stop|withdrawal|opt[ -]?out)\s+request)\b'
        r'|\b(?:that|this|it|those|these)\s+(?:is|was|were|are)\s+(?:(?:just|only)\s+)?(?:an?\s+|the\s+)?(?:example|quote|words|hypothetical)\b'
        r'|\b(?:is|was|were|that\'s)\s+(?:what|(?:(?:just|only)\s+)?an?\s+(?:example|quote)|(?:her|his|their)\s+words)\b'
    )
    qualifier = r'^(?:(?:actually|instead)\s*[,]?\s*)?(?:if|unless|except|only|maybe|about|regarding|hypothetically)\b|\bi\s+(?:might|would)\s+(?:say|ask|request)\b'
    attribution = r'^(?:(?:he|she|they|you|(?:my|the)\s+\w+)\s+)?(?:said|says|asked|asks)\b|^i\s+said[.!]*$'
    keyword = r'(?:please\s+)?(?:' + '|'.join(re.escape(word) for word in STOP_WORDS) + ')'
    for pattern in (*patterns, keyword):
        match = re.fullmatch(pattern + r'(?P<join>\s*[,;.!:]\s*|\s+(?:because|since)\s+|\s+(?=(?:please|thanks|thank you)\b))(?P<tail>.+)', text, re.I | re.S)
        if not match:
            continue
        tail = match['tail'].strip()
        explanatory = re.search(r'\b(?:because|since)\b', match['join'], re.I)
        if (not re.search(tail_guard, tail, re.I) and not re.search(qualifier, tail, re.I)
                and (explanatory or not re.search(attribution, tail, re.I))):
            return 'stop'
    return None


def prior_disclosed_consent(session, volunteer):
    """Stored flags alone cannot turn an imported contact into an SMS subscriber."""
    from app.integrations.google_voice_demo import RECIPIENT_KEY, registered_consent_provenance
    if session.get(m.Policy, RECIPIENT_KEY + volunteer.phone):
        return registered_consent_provenance(session, volunteer)
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
