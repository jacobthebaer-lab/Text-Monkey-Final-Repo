"""Explicit admin staffing subscriptions and immutable per-message source receipts.

Subscriptions limit event digests, never grant leadership, roster access or consent.
The mutable digest is separate from each durable reviewed/native delivery receipt.
"""
from datetime import datetime
import hashlib
import unicodedata
from uuid import uuid4
from sqlalchemy import select
from app.db import models as m

PREFERENCE = 'admin_staffing_scope'
PREFIX = 'staffing-source:'


def ministry(value):
    if not isinstance(value, str):
        raise ValueError('Choose a recorded ministry.')
    value = unicodedata.normalize('NFC', value.strip())
    if not value or len(value) > 80 or any(ord(c) < 32 for c in value):
        raise ValueError('Choose a recorded ministry.')
    return value


def available_ministries(session):
    values = set()
    for value in session.scalars(select(m.Role.ministry)):
        try:
            values.add(ministry(value))
        except ValueError:
            pass  # Invalid historical labels cannot become subscription choices.
    return sorted(values)


def validate_scope(value, available):
    if (not isinstance(value, dict) or set(value) != {'mode', 'ministries'}
            or not isinstance(value['mode'], str) or value['mode'] not in {'all', 'selected'}
            or not isinstance(value['ministries'], list)
            or len(value['ministries']) > 50):
        raise ValueError('Choose all ministries or a list of recorded ministries.')
    selected = sorted({ministry(item) for item in value['ministries']})
    if (value['mode'] == 'all' and selected) or (value['mode'] == 'selected' and not selected):
        raise ValueError('Choose at least one ministry for selected updates, or all ministries.')
    if not set(selected) <= set(available):
        raise ValueError('A selected ministry is no longer recorded. Reload the ministry choices.')
    return {'mode': value['mode'], 'ministries': selected}


def scope(session, recipient):
    preferences = recipient.preferences or {}
    if PREFERENCE not in preferences:
        return {'mode': 'all', 'ministries': []}
    return validate_scope(preferences[PREFERENCE], available_ministries(session))


def role_facts(session, event):
    shifts = session.execute(select(m.Shift.id, m.Shift.role_id, m.Role.ministry).join(m.Role)
        .where(m.Shift.event_id == event.id).order_by(m.Shift.id)).all()
    recipes = session.execute(select(m.RoleRecipe.id, m.RoleRecipe.role_id, m.RoleRecipe.count, m.Role.ministry)
        .join(m.Role).where(m.RoleRecipe.event_type_id == event.event_type_id)
        .order_by(m.RoleRecipe.id)).all() if event.event_type_id is not None else []
    return {'shifts': [list(row) for row in shifts], 'recipes': [list(row) for row in recipes]}


def matches(session, event, recipient):
    if not recipient or not recipient.is_coordinator or recipient.status != 'active' or not recipient.sms_opt_in:
        return False
    if (recipient.preferences or {}).get('admin_event_only') is True:
        return False
    stopped = session.get(m.Policy, 'sms_opt_out:' + recipient.phone, populate_existing=True)
    if stopped and stopped.value.get('value'):
        return False
    try:
        subscription = scope(session, recipient)
    except ValueError:
        return False
    if subscription['mode'] == 'all':
        return True
    ministries = set()
    facts = role_facts(session, event)
    for row in facts['shifts'] + [row for row in facts['recipes'] if row[2] > 0]:
        try:
            ministries.add(ministry(row[-1]))
        except ValueError:
            pass
    return bool(ministries.intersection(subscription['ministries']))


def capture(session, notification, now):
    event = session.get(m.Event, notification.event_id, populate_existing=True)
    recipient = session.get(m.Volunteer, notification.volunteer_id, populate_existing=True)
    if (not event or event.status != 'scheduled' or (notification.expires_at and now >= notification.expires_at)
            or not matches(session, event, recipient)):
        return None
    from app.core.notifications import pre_event_source
    return {'subscription': scope(session, recipient),
            'admin_text_owner': (recipient.preferences or {}).get('admin_text_owner'),
            'admin_text_consent_key': (recipient.preferences or {}).get('admin_text_consent_key'),
            'event_type_id': event.event_type_id,
            'roles': role_facts(session, event), 'event_facts': pre_event_source(session, notification)}


def prepare(session, notification, facts, body, now):
    body_hash = hashlib.sha256(body.encode()).hexdigest()
    prior = notification.detail.get('staffing_source')
    if isinstance(prior, dict) and prior.get('facts') == facts and prior.get('body_hash') == body_hash:
        row = session.get(m.Policy, prior.get('receipt_key'), populate_existing=True)
        if row and row.value.get('state') == 'awaiting_approval':
            approval = session.get(m.Approval, row.value.get('approval_id'), populate_existing=True)
            from app.core.confirmations import valid
            if (approval and approval.status == 'pending' and valid(approval, now)
                    and approval.payload.get('staffing_source') == prior
                    and problem(session, prior, now, approval=approval) is None):
                notification.state = 'awaiting_approval'
                return prior, True
    if isinstance(prior, dict):
        saved = receipt(session, prior)
        approval_id = saved.value.get('approval_id') if saved else None
        approval = session.get(m.Approval, approval_id) if approval_id else None
        if saved and approval and approval.status == 'pending':
            from app.core.confirmations import audit
            approval.status = 'expired'
            saved.value = {**saved.value, 'state': 'blocked', 'reason': 'New staffing facts or copy require fresh review'}
            audit(session, approval, now, 'blocked', 'system', saved.value['reason'])
    # stage_text deduplicates generic copy before the new source is attached.
    # Retire a matching legacy proposal rather than silently changing its hash.
    for legacy in session.scalars(select(m.Approval).where(m.Approval.kind == 'confirm_text',
            m.Approval.status == 'pending', m.Approval.payload['volunteer_id'].as_integer() == notification.volunteer_id,
            m.Approval.payload['purpose'].as_string() == 'coordinator_notify', m.Approval.payload['body'].as_string() == body)):
        if legacy.payload.get('staffing_source') is None:
            from app.core.confirmations import audit
            legacy.status = 'expired'
            audit(session, legacy, now, 'blocked', 'system', 'New staffing digest requires its own source review')
    key = PREFIX + uuid4().hex
    binding = {'receipt_key': key, 'facts': facts, 'body_hash': body_hash}
    session.add(m.Policy(key=key, value={'binding': binding, 'notification_key': notification.key,
        'event_id': notification.event_id, 'volunteer_id': notification.volunteer_id,
        'expires_at': notification.expires_at.isoformat() if notification.expires_at else None,
        'state': 'prepared', 'approval_id': None, 'message_id': None}))
    notification.detail = {**notification.detail, 'staffing_source': binding}
    session.flush()
    return binding, False


def receipt(session, binding):
    key = binding.get('receipt_key') if isinstance(binding, dict) else None
    if not isinstance(key, str) or not key.startswith(PREFIX) or len(key) != len(PREFIX) + 32:
        return None
    return session.scalar(select(m.Policy).where(m.Policy.key == key).with_for_update()
        .execution_options(populate_existing=True))


def problem(session, binding, now, *, approval=None, message=None):
    saved = receipt(session, binding)
    if not saved or saved.value.get('binding') != binding:
        return 'Staffing update source binding is missing or changed'
    proof = saved.value
    row = session.get(m.Notification, proof.get('notification_key'), populate_existing=True)
    if (not row or not row.key.startswith('staffing:') or row.purpose != 'coordinator_notify'
            or row.event_id != proof.get('event_id') or row.volunteer_id != proof.get('volunteer_id')):
        return 'Staffing update has no linked event or recipient'
    if binding.get('facts') != capture(session, row, now):
        return 'Staffing subscription, ministry, event or admin changed; fresh review required'
    try:
        expires = datetime.fromisoformat(proof['expires_at'])
        if now >= expires:
            return 'Staffing update source expired'
    except (KeyError, TypeError, ValueError):
        return 'Staffing update source expiry is missing'
    person = session.get(m.Volunteer, row.volunteer_id, populate_existing=True)
    if approval is not None:
        p = approval.payload
        if (proof.get('approval_id') != approval.id or p.get('staffing_source') != binding
                or p.get('volunteer_id') != row.volunteer_id or p.get('phone') != person.phone
                or p.get('purpose') != 'coordinator_notify'):
            return 'Staffing exact review is not bound to this recipient'
        if message is None and (proof.get('state') != 'awaiting_approval' or proof.get('message_id') is not None):
            return 'Staffing review source is already consumed or held'
        body = p.get('body')
    else:
        body = message.body if message else None
    if message is not None:
        if (proof.get('state') != 'queued' or proof.get('message_id') != message.id
                or message.volunteer_id != row.volunteer_id or message.phone != person.phone
                or message.purpose != 'coordinator_notify'):
            return 'Staffing message is not linked to its source'
        body = message.body
    if not isinstance(body, str) or hashlib.sha256(body.encode()).hexdigest() != binding.get('body_hash'):
        return 'Staffing reviewed body changed'
    return None


def hold(session, binding, reason):
    saved = receipt(session, binding)
    if not saved:
        return
    saved.value = {**saved.value, 'state': 'blocked', 'reason': reason}
    row = session.get(m.Notification, saved.value.get('notification_key'))
    if row and row.detail.get('staffing_source') == binding:
        row.state = 'blocked_policy'
        row.detail = {**row.detail, 'reason': reason}


def approval_problem(session, approval, now, message=None):
    binding = approval.payload.get('staffing_source')
    if binding is None:
        return legacy_problem(session, approval.payload.get('volunteer_id'), approval.payload.get('purpose'), approval.payload.get('body'))
    return problem(session, binding, now, approval=approval, message=message)


def link_message(session, binding, message_id, now=None):
    saved = receipt(session, binding)
    if saved:
        saved.value = {**saved.value, 'message_id': message_id, 'state': 'queued'}
        # A stable message marker prevents a lost source receipt from being
        # treated as an older unbound digest after the mutable digest is reused.
        key = 'staffing-message:' + str(message_id)
        marker = session.get(m.Policy, key)
        if marker is None:
            session.add(m.Policy(key=key, value={'binding': binding}))
        row = session.get(m.Notification, saved.value.get('notification_key'))
        if row and row.detail.get('staffing_source') == binding:
            row.message_id, row.state = message_id, 'sent'
            if now is not None:
                row.detail = {**row.detail, 'last_sent_at': now.isoformat(),
                    'last_snapshot': row.detail.get('pending_snapshot')}


def native_problem(session, message, now, approval=None):
    marker = session.get(m.Policy, 'staffing-message:' + str(message.id), populate_existing=True)
    if not marker and message.purpose != 'coordinator_notify' and not (approval and approval.payload.get('staffing_source')):
        return None
    saved = None if marker else session.scalar(select(m.Policy).where(m.Policy.key.startswith(PREFIX),
        m.Policy.value['message_id'].as_integer() == message.id).with_for_update()
        .execution_options(populate_existing=True))
    binding = (approval.payload.get('staffing_source') if approval else
        marker.value.get('binding') if marker else saved.value.get('binding') if saved else None)
    if marker and (not isinstance(binding, dict) or marker.value.get('binding') != binding):
        return 'Staffing message source marker is missing or changed'
    if binding is None:
        return legacy_problem(session, message.volunteer_id, message.purpose, message.body)
    error = problem(session, binding, now, approval=approval, message=message)
    if error:
        hold(session, binding, error)
    return error


def legacy_problem(session, recipient_id, purpose, body):
    # Old exact hashes remain unchanged. A restricted recipient cannot inherit
    # an unbound historical whole-church staffing digest.
    if purpose != 'coordinator_notify' or not isinstance(body, str) or 'Pre-event update:' in body or not any(
            marker in body for marker in ('Fully staffed:', 'Still needs cover:')):
        return None
    recipient = session.get(m.Volunteer, recipient_id, populate_existing=True) if recipient_id else None
    if (not recipient or not recipient.is_coordinator or recipient.status != 'active' or not recipient.sms_opt_in
            or (recipient.preferences or {}).get('admin_event_only') is True):
        return 'Staffing admin recipient changed'
    stopped = session.get(m.Policy, 'sms_opt_out:' + recipient.phone, populate_existing=True)
    if stopped and stopped.value.get('value'):
        return 'Staffing admin no longer consents'
    try:
        if scope(session, recipient)['mode'] == 'all':
            return None
    except ValueError:
        pass
    return 'Legacy staffing update needs fresh subscription source review'
