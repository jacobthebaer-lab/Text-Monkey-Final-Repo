"""Explicit event recipients and factual cancellation/coverage history.

Only existing consented roster records can be saved as event admins. Routing
does not recruit contacts or change separate ministry staffing subscriptions.
"""
import hashlib
import json
from datetime import timezone
from sqlalchemy import inspect, select
from app.db import models as m

EVENT_OWNER = 'admin_event_recipient_owner'
EVENT_ONLY = 'admin_event_only'
PREFIX = 'event-admins:'


def eligible(session, person):
    if person:
        person = session.get(m.Volunteer, person.id, populate_existing=True)
    if not person or person.status != 'active' or not person.sms_opt_in:
        return False
    prefs = person.preferences or {}
    if not person.is_coordinator and (prefs.get(EVENT_ONLY) is not True or not prefs.get(EVENT_OWNER)):
        return False
    owner_id = saved_owner(person)
    if owner_id:
        from app.core.admin_text_enrollment import ADMIN_PURPOSES, evidence_hash
        consent_key = prefs.get('admin_text_consent_key')
        receipt = session.get(m.Notification, consent_key, populate_existing=True) if isinstance(consent_key, str) and consent_key.startswith('admin-text-consent:') else None
        detail = receipt.detail if receipt else {}
        if (not receipt or receipt.state != 'enrolled' or receipt.purpose != 'human_review'
                or receipt.volunteer_id != person.id or detail.get('recipient_id') != person.id
                or detail.get('owner_id') != owner_id or detail.get('phone') != person.phone
                or detail.get('name') != person.name or detail.get('mode') not in {'self_service', 'operator_attested'}
                or detail.get('mode') != prefs.get('admin_text_consent_mode')
                or (not person.is_coordinator and detail.get('mode') != 'operator_attested')
                or detail.get('consent_at') != prefs.get('admin_text_consent_at')
                or detail.get('consent_at') != receipt.created_at.isoformat()
                or detail.get('purposes') != sorted(ADMIN_PURPOSES)):
            return False
        if detail['mode'] == 'operator_attested':
            review_key = detail.get('review_id')
            review = session.get(m.Notification, review_key, populate_existing=True) if isinstance(review_key, str) and review_key.startswith('admin-recipient-review:') else None
            if (not review or review.state != 'approved' or review.detail.get('target_id') != person.id
                    or review.purpose != 'human_review' or review.detail.get('phone') != person.phone
                    or review.detail.get('owner_id') != owner_id
                    or detail.get('review_hash') != evidence_hash(review.detail)):
                return False
    if prefs.get('admin_text_owner') and prefs.get(EVENT_OWNER) and prefs['admin_text_owner'] != prefs[EVENT_OWNER]:
        return False
    stopped = session.get(m.Policy, 'sms_opt_out:' + person.phone, populate_existing=True)
    return not (stopped and stopped.value.get('value'))


def saved_owner(person):
    prefs = person.preferences or {}
    return prefs.get('admin_text_owner') or prefs.get(EVENT_OWNER)


def saved_admins(session, owner_id):
    return list(session.scalars(select(m.Volunteer).where(
        (m.Volunteer.preferences['admin_text_owner'].as_string() == owner_id) |
        (m.Volunteer.preferences[EVENT_OWNER].as_string() == owner_id)
    ).order_by(m.Volunteer.id).execution_options(populate_existing=True)))


def default_admins(session):
    # Once a primary has been enrolled, STOP/pause must hold it rather than
    # falling back to an unrelated coordinator. Portable legacy fixtures retain
    # their existing recipients until a primary is explicitly enrolled.
    primary = list(session.scalars(select(m.Volunteer).where(
        m.Volunteer.preferences['admin_text_owner'].as_string().is_not(None)
    ).order_by(m.Volunteer.id).execution_options(populate_existing=True)))
    if primary:
        from app.admin_setup.models import Workspace
        owners = {(p.preferences or {}).get('admin_text_owner') for p in primary}
        if len(owners) != 1:
            return []  # More than one account's primary needs an explicit event choice.
        owner_id = next(iter(owners))
        workspace = session.scalar(select(Workspace).where(Workspace.owner_id == owner_id)
            .execution_options(populate_existing=True))
        if not workspace or not workspace.completed:
            return []
        configured = next((p for p in primary if p.phone == workspace.details.get('coordinator_phone')), None)
        return [configured] if eligible(session, configured) else []
    from app.admin_setup.models import Workspace
    if (inspect(session.connection()).has_table(Workspace.__tablename__)
            and session.scalar(select(Workspace.id).where(Workspace.completed).limit(1))):
        return []  # Durable setup survives a deleted primary or lost owner marker.
    if session.scalar(select(m.Notification.key).where(
            m.Notification.key.startswith('admin-text-consent:')).limit(1)):
        return []
    return [p for p in session.scalars(select(m.Volunteer).where(m.Volunteer.is_coordinator)
        .order_by(m.Volunteer.id).execution_options(populate_existing=True))
        if eligible(session, p) and (p.preferences or {}).get(EVENT_ONLY) is not True]


def route(session, event):
    policy = session.get(m.Policy, PREFIX + str(event.id), populate_existing=True)
    if not policy:
        return {'mode': 'inherit', 'recipient_ids': [], 'revision': 0, 'owner_id': None}
    value = policy.value
    ids = value.get('recipient_ids') if isinstance(value, dict) else None
    if (not isinstance(value, dict) or set(value) != {'mode', 'recipient_ids', 'revision', 'owner_id'}
            or value.get('mode') not in ('inherit', 'selected') or not isinstance(ids, list)
            or len(ids) > 20 or any(type(i) is not int or i < 1 for i in ids)
            or len(set(ids)) != len(ids) or ids != sorted(ids)
            or (value['mode'] == 'inherit' and ids) or type(value.get('revision')) is not int
            or value['revision'] < 1 or not isinstance(value.get('owner_id'), str)):
        return {'mode': 'invalid', 'recipient_ids': [], 'revision': None, 'owner_id': None}
    return dict(value)


def recipients(session, event):
    value = route(session, event)
    if value['mode'] == 'inherit':
        return default_admins(session)
    if value['mode'] != 'selected':
        return []
    people = [session.get(m.Volunteer, i, populate_existing=True) for i in value['recipient_ids']]
    # A removed/ineligible selected recipient is held individually. No replacement
    # recipient is inferred and no contact is enrolled from a stored phone.
    return [p for p in people if eligible(session, p) and saved_owner(p) == value['owner_id']]


def routing_source(session, event):
    value = route(session, event)
    return {**value, 'resolved_ids': [p.id for p in recipients(session, event)]}


def text_problem(session, person, purpose, body, now):
    """Event-only recipients cannot inherit other coordinator text privileges."""
    if not person or (person.preferences or {}).get(EVENT_ONLY) is not True or purpose not in {'coordinator_notify', 'escalation_notify'}:
        return None
    from app.core.notifications import pre_event_delivery_problem
    for row in session.scalars(select(m.Notification).where(
            m.Notification.volunteer_id == person.id, m.Notification.key.startswith('pre-event:'),
            m.Notification.purpose == purpose).execution_options(populate_existing=True)):
        binding = (row.detail or {}).get('pre_event_source')
        if (isinstance(binding, dict) and binding.get('body_hash') == hashlib.sha256(body.encode()).hexdigest()
                and not pre_event_delivery_problem(session, row, now, binding=binding, body=body)):
            return None
    return 'Event recipient requires a current event-specific source and consent'


def changes(session, event):
    rows = session.execute(select(m.Assignment, m.Volunteer.name, m.Role.name, m.Shift.parent_shift_id,
        m.Shift.interval_starts_at, m.Shift.interval_ends_at).join(m.Shift, m.Assignment.shift_id == m.Shift.id)
        .join(m.Role, m.Shift.role_id == m.Role.id).join(m.Volunteer, m.Assignment.volunteer_id == m.Volunteer.id)
        .where(m.Shift.event_id == event.id).order_by(m.Assignment.id)
        .execution_options(populate_existing=True)).all()
    history = [{'assignment_id': a.id, 'shift_id': a.shift_id, 'volunteer_id': a.volunteer_id,
        'name': name, 'role': role, 'status': a.status, 'source': a.source,
        'created_at': a.created_at.astimezone(timezone.utc).isoformat(), 'updated_at': a.updated_at.astimezone(timezone.utc).isoformat(),
        'parent_shift_id': parent, 'starts_at': starts.isoformat() if starts else None,
        'ends_at': ends.isoformat() if ends else None} for a, name, role, parent, starts, ends in rows]
    cancelled = [row for row in history if row['status'] == 'cancelled']
    active = [row for row in history if row['status'] in ('approved', 'confirmed')]
    fills = [{'id': f.id, 'shift_id': f.shift_id, 'cancelled_assignment_id': f.cancelled_assignment_id,
        'state': f.state, 'created_at': f.created_at.astimezone(timezone.utc).isoformat(),
        'closed_at': f.closed_at.astimezone(timezone.utc).isoformat() if f.closed_at else None}
        for f in session.scalars(select(m.FillRequest).join(m.Shift).where(m.Shift.event_id == event.id)
            .order_by(m.FillRequest.id).execution_options(populate_existing=True))]
    filled = []
    for current in active:
        # Report coverage of a vacated slot, not a guessed one-to-one replacement.
        # Split children cover their explicitly recorded parent slot.
        cancellations = [old for old in cancelled if old['shift_id'] in
            (current['shift_id'], current['parent_shift_id']) and old['assignment_id'] < current['assignment_id']
            and old['updated_at'] <= current['created_at']]
        recorded_fill = current['source'] == 'fill' and any(f['shift_id'] == current['shift_id']
            and f['state'] == 'filled' and f['closed_at']
            and f['created_at'] <= current['created_at'] <= f['closed_at'] for f in fills)
        if cancellations or recorded_fill:
            filled.append(current)
    return {'history': history, 'fill_requests': fills, 'cancelled': cancelled, 'filled': filled,
            'initial_roster': [row for row in active if row not in filled]}


def changes_copy(facts):
    from app.core.church_labels import church_label
    def names(rows):
        labels = list(dict.fromkeys(f"{church_label(row['name'])} ({church_label(row['role'])}{', partial cover' if row['parent_shift_id'] else ''})" for row in rows))
        return ', '.join(labels) if labels else 'none'
    return f"Canceled: {names(facts['cancelled'])}. Filled spots: {names(facts['filled'])}."


def event_hash(session, event):
    facts = {'event': {key: getattr(event, key) for key in
        ('id', 'title', 'starts_at', 'ends_at', 'status', 'event_type_id')}, 'route': route(session, event)}
    return hashlib.sha256(json.dumps(facts, sort_keys=True, default=str).encode()).hexdigest()


def invalidate_unsent(session, event, now):
    """Keep old reviewed bodies/hashes; only known unsent notices can recapture."""
    from app.core import confirmations, notifications
    from app.integrations.mac_models import MacDeliveryClaim
    rows = list(session.scalars(select(m.Notification).where(m.Notification.event_id == event.id,
        m.Notification.key.startswith('pre-event:'), m.Notification.state.in_(('pending', 'awaiting_approval', 'sent')))
        .with_for_update().execution_options(populate_existing=True)))
    for row in rows:
        approval = session.get(m.Approval, row.detail.get('approval_id')) if row.detail.get('approval_id') else None
        message = session.get(m.Message, row.message_id) if row.message_id else None
        uncertain = message and (message.status != 'queued' or session.get(MacDeliveryClaim, message.id) is not None)
        if uncertain:
            continue  # Claim/preflight recheck the new route; never auto-replay a native attempt.
        reason = 'Event admin recipients changed; fresh source review required'
        if approval and approval.status in ('pending', 'approved'):
            notifications.invalidate_pre_event_review(session, approval, now, reason, message)
            confirmations.audit(session, approval, now, 'blocked', 'system', reason)
        else:
            row.state = 'expired' if notifications.pre_event_delivery_problem(session, row, now) else 'pending'
            row.message_id = None
            row.due_at = now
            row.detail = {k: v for k, v in row.detail.items() if k not in
                ('pre_event_source', 'approval_id', 'conversation_meta')}
            row.detail = {**row.detail, 'reason': reason}
        if message:
            message.status = 'superseded'
