"""Durable additive roster enrollment. No composition, queues or native delivery."""
from copy import deepcopy
import re
from sqlalchemy import select
from app.db import models as m
from app.integrations.mac_ongoing import verify, _digest
from app.integrations.test_sessions import parse_sessions
from app.sms.mac_provider import MacMessagesProvider, PHONE

POLICY = 'mac_roster_enrollment'
SCOPE = 'mac_roster_accepted_scope'
PENDING = 'mac_roster_pending_enrollment'
INTENT = 'mac_roster_intent:'
RESERVED = re.compile(r'\+1[0-9]{3}55501[0-9]{2}\Z')


def policy(session):
    row = session.get(m.Policy, POLICY)
    value = row.value if row else {}
    return value if (isinstance(value,dict) and value.get('enabled') is True
        and isinstance(value.get('actor'),str) and 0 < len(value['actor'].strip()) <= 120) else None


def eligible(session, person):
    if not person or person.status != 'active' or person.sms_opt_in is not True:
        return False
    prefs = person.preferences or {}
    if (not PHONE.fullmatch(person.phone) or RESERVED.fullmatch(person.phone)
            or prefs.get('synthetic') or prefs.get('fictional_seed') or prefs.get('synthetic_dataset')
            or '[fictional]' in person.name.lower() or prefs.get('consent_pending')):
        return False
    stopped = session.get(m.Policy, 'sms_opt_out:'+person.phone)
    return not (stopped and stopped.value.get('value'))


def journal(state):
    return verify(getattr(state.provider,'roster_journal',None) or state.settings.mac_ongoing_authorization,
                  state.settings.mac_bridge_token)


def install(state, value):
    value = verify(value,state.settings.mac_bridge_token)
    sessions = parse_sessions(value['sessions'],set(value['route']['phones']),allow_ongoing=True)
    state.provider.test_sessions = sessions
    state.provider.phones = frozenset(sessions)
    state.provider.roster_journal = deepcopy(value)


def descends(value, anchor):
    while isinstance(value,dict):
        if _digest(value)==_digest(anchor): return True
        value=value.get('previous_authorization')
    return False


def restore(state):
    if not isinstance(state.provider,MacMessagesProvider): return
    with state.session_factory() as session:
        stored=session.get(m.Policy,SCOPE)
        if not stored: return
        value=verify(stored.value['journal'],state.settings.mac_bridge_token)
        anchor=journal(state)
        if not descends(value,anchor):
            staged=session.scalars(select(m.Policy).where(m.Policy.key.startswith(INTENT))).all()
            known=any(row.value.get('phase') in {'prepared','cancelled'}
                and _digest(row.value.get('journal'))==_digest(anchor)
                and _digest(anchor.get('previous_authorization'))==_digest(value) for row in staged)
            if not known:
                raise ValueError('Persisted roster scope does not descend from configured Mac authority')
        install(state,value)


def accepted(session,state):
    current=journal(state)
    stored=session.get(m.Policy,SCOPE)
    if stored and _digest(stored.value['journal'])!=_digest(current):
        value=verify(stored.value['journal'],state.settings.mac_bridge_token)
        if not descends(value,current): raise ValueError('Accepted scope conflicts with running Mac authority')
        install(state,value);current=value
    return current


def unsettled(session):
    return session.scalar(select(m.Message.id).where(m.Message.provider_sid.startswith('MAC'),
        m.Message.status.in_(['dispatching','uncertain'])).limit(1)) is not None


def freeze_claims(session):
    pending=session.get(m.Policy,PENDING)
    if not pending: return False
    intent=session.get(m.Policy,INTENT+pending.value['intent_id'])
    return bool(intent and intent.value.get('phase')=='prepared')


def put(session,key,value):
    row=session.get(m.Policy,key)
    if row: row.value=deepcopy(value)
    else: session.add(m.Policy(key=key,value=deepcopy(value)))


def intent(session,intent_id):
    row=session.get(m.Policy,INTENT+intent_id)
    return deepcopy(row.value) if row else None


def cancel(session,value,reason):
    value={**value,'phase':'cancelled','reason':reason}
    put(session,INTENT+value['intent_id'],value)
    pending=session.get(m.Policy,PENDING)
    if pending and pending.value.get('intent_id')==value['intent_id']: session.delete(pending)
    return value


def receipt(value):
    return {key:value[key] for key in ('intent_id','phase','reason','journal_id') if key in value}
