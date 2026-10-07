"""Explicit, recipient-bound welcome before an existing profile's role intake."""
import hashlib
import json
from uuid import uuid4
from sqlalchemy import select
from app.db import models as m
from app.core import confirmations
from app.core.onboarding_copy import DEFAULTS, copy_key, preferred_wording
from app.core.signup_responder import compose_signup_reply
from app.llm.gloo_client import GlooUnavailableError
from app.llm.parser import _extract_json, keyword_sensitive


def digest(body):
    return hashlib.sha256(body.encode()).hexdigest()


def binding(session, person, key, now, *, for_reply=False):
    selected = session.info.get('mac_test_session')
    intent = session.get(m.Notification, key) if isinstance(key, str) else None
    if (not person
            or not selected or not selected.outbound_prefix.startswith('MAC') or not selected.active(now)
            or not intent or intent.purpose != 'volunteer_introduction' or intent.state != 'authorized'
            or intent.volunteer_id != person.id or intent.created_at > now):
        return None
    detail = intent.detail
    if (detail.get('phone') != person.phone or detail.get('name') != person.name
            or detail.get('session_id') != selected.id
            or person.preferences.get('onboarding_stage') != 'welcome_name'
            or person.preferences.get('welcome_introduction') != key):
        return None
    retry = detail.get('welcome_retry')
    if retry and not for_reply:
        from app.core.volunteer_welcome import retry_binding
        if retry_binding(session, person, retry) != retry:
            return None
    return dict(detail)


def start(session, clock, gate, person, gloo, *, copy_owner=None):
    confirmations.authorize_sender_fields(session, person, {'preferences'})
    prefs = {k: v for k, v in person.preferences.items() if k != 'onboarding_copy_owner'}
    if copy_owner is not None:
        prefs['onboarding_copy_owner'] = copy_key(copy_owner).removeprefix('onboarding_copy:')
    key = 'welcome-introduction:' + str(uuid4())
    person.preferences = {**prefs, 'onboarding_stage': 'welcome_name', 'signup_minimal_texts': True,
                          'welcome_introduction': key}
    approved = (preferred_wording(session, 'welcome', person) or DEFAULTS['welcome']).strip()
    selected = session.info.get('mac_test_session')
    detail = {'phone': person.phone, 'name': person.name, 'session_id': selected.id if selected else None,
              'body_hash': digest(approved)}
    if session.info.get('welcome_retry'):
        detail['welcome_retry'] = session.info['welcome_retry']
    session.add(m.Notification(key=key, volunteer_id=person.id, purpose='volunteer_introduction',
        state='authorized', created_at=clock.now(), due_at=clock.now(), detail=detail))
    session.flush()
    if binding(session, person, key, clock.now()) is None:
        raise GlooUnavailableError('Welcome requires its current recipient session')
    body = compose_signup_reply(session, clock, gloo, approved, volunteer=person,
        signup_conversation=True, require_gloo=True, exact_copy=True)
    return gate.send(body=body, purpose='signup_reply', kind='ai', volunteer=person,
                     conversation={'welcome_introduction': key})


def handle(session, clock, gate, person, body, gloo):
    """Name confirmation cannot grant consent or clear qualifications."""
    if keyword_sensitive(body):
        from app.core.care import escalate_sensitive
        escalate_sensitive(session, gate, person, body, clock.now())
        return 'escalated_sensitive'
    from app.core.conversation import inbound_scope
    from app.core.signup_recovery import privacy_hold
    from app.core.send_gate import has_open_sensitive_escalation
    key = person.preferences.get('welcome_introduction')
    proof = binding(session, person, key, clock.now(), for_reply=True)
    selected = session.info.get('mac_test_session')
    optout = session.get(m.Policy, 'sms_opt_out:' + person.phone)
    if (not person.sms_opt_in or person.status != 'active' or not proof or (optout and optout.value.get('value'))
            or privacy_hold(session, person.phone, person)
            or has_open_sensitive_escalation(session, person.id)):
        return 'onboarding_review'
    incoming = session.scalar(select(m.Message).where(m.Message.id == gate.reply_to_message_id,
        inbound_scope(selected), m.Message.volunteer_id == person.id, m.Message.phone == person.phone,
        m.Message.direction == 'in', m.Message.status == 'received', m.Message.created_at <= clock.now(),
        m.Message.created_at >= selected.starts_at, selected.window(m.Message.created_at)))
    latest = session.scalar(select(m.Message.id).where(inbound_scope(selected), m.Message.phone == person.phone,
        m.Message.direction == 'in', m.Message.status == 'received').order_by(m.Message.id.desc()).limit(1))
    if not incoming or incoming.body != body or incoming.id != latest:
        return 'onboarding_review'
    # Require the exact submitted welcome and its conversation receipt. Queued,
    # pending review, dispatching and uncertain texts never establish receipt.
    from app.integrations.mac_models import MacDeliveryClaim
    welcome = session.scalar(select(m.Message).join(MacDeliveryClaim,
        MacDeliveryClaim.message_id == m.Message.id).join(m.Notification,
        m.Notification.message_id == m.Message.id).where(
        m.Notification.key.startswith('conversation-message:'),
        m.Notification.detail['welcome_introduction'].as_string() == key,
        m.Message.volunteer_id == person.id, m.Message.phone == person.phone,
        m.Message.direction == 'out', m.Message.purpose == 'signup_reply', m.Message.status == 'submitted',
        m.Message.provider_sid.startswith(selected.outbound_prefix), m.Message.id < incoming.id,
        m.Message.created_at <= incoming.created_at).order_by(m.Message.id.desc()).limit(1))
    if not welcome or digest(welcome.body) != proof['body_hash']:
        return 'onboarding_review'
    from app.core.privacy import safe_message_history
    if not safe_message_history(session, [incoming]):
        return 'onboarding_review'
    if not gloo or not getattr(gloo.settings, 'gloo_signup_replies', False):
        return 'onboarding_review'
    try:
        from app.integrations.mac_roster import composition_session
        if composition_session(session, gloo.settings, person.phone) != selected:
            return 'onboarding_review'
    except (ValueError, TypeError, KeyError, AttributeError):
        return 'onboarding_review'
    from app.llm.agent_loop import RunLogger
    log = RunLogger(session, clock, agent='onboarding', trigger='Welcome name reply', model=gloo.settings.parser_model)
    try:
        response = gloo.create_response(model=gloo.settings.parser_model,
            instructions='Interpret the actual reply to the welcome. Return JSON with first_name, last_name and sensitive (boolean). Extract only a self-reported full name. Never infer a name from the supplied profile, roles, consent or other messages.',
            input=json.dumps({'stage': 'welcome_name', 'body': body}))
        log.add_usage(getattr(response, 'usage', None))
        values = _extract_json(response.output_text)
    except (GlooUnavailableError, ValueError, TypeError, AttributeError):
        log.close('welcome_name_held')
        return 'onboarding_review'
    if not isinstance(values, dict):
        log.close('welcome_name_held')
        return 'onboarding_review'
    if values.get('sensitive') is True:
        log.close('sensitive')
        from app.core.care import escalate_sensitive
        escalate_sensitive(session, gate, person, body, clock.now())
        return 'escalated_sensitive'
    from app.integrations.google_voice_demo import full_name_matches, meaningful_name_part, normalized_name
    if (values.get('sensitive') is not False
            or not all(meaningful_name_part(values.get(field)) for field in ('first_name', 'last_name'))
            or not full_name_matches(body, values)
            or normalized_name(values['first_name'] + ' ' + values['last_name']).casefold() != normalized_name(person.name).casefold()):
        log.close('welcome_name_held')
        return 'onboarding_review'
    log.close('welcome_name_validated')
    confirmations.authorize_sender_fields(session, person, {'preferences'})
    person.preferences = {**person.preferences, 'welcome_name_evidence': {
        'incoming_id': incoming.id, 'incoming_body_hash': digest(body), 'welcome_id': welcome.id,
        'session_id': selected.id}}
    from app.core.onboarding import start as start_preferences
    start_preferences(session, clock, gate, person, gloo, copy_owner=person.preferences.get('onboarding_copy_owner'))
    return 'onboarding_interests'
