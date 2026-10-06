"""Internal repeated-decline evidence, never outreach or inferred opt-out intent."""
import hashlib
from datetime import datetime, timedelta, timezone

from sqlalchemy import select
from app.db import models as m
from app.core.consent_controls import control_action

POLICY_KEY = 'capacity_repeated_declines'
TYPE = 'repeated_declines'
DEFAULTS = {'window_days': 56, 'minimum_events': 3, 'minimum_span_days': 14,
            'history_days': 365, 'minimum_history_events': 3, 'minimum_history_span_days': 28,
            'maximum_records': 5000}


def configuration(session, supplied=None):
    row = session.get(m.Policy, POLICY_KEY)
    if row and (not isinstance(row.value, dict) or set(row.value) != {'value'}):
        raise ValueError('Repeated-decline policy requires its explicit value wrapper')
    raw = supplied if supplied is not None else (row.value.get('value', {}) if row else {})
    if not isinstance(raw, dict) or set(raw) - set(DEFAULTS):
        raise ValueError('Repeated-decline policy contains unsupported fields')
    result = {**DEFAULTS, **raw}
    if any(type(value) is not int or not 1 <= value <= 10000 for value in result.values()):
        raise ValueError('Repeated-decline policy requires bounded positive integers')
    if (result['minimum_events'] < 2 or result['minimum_history_events'] < 2
            or result['minimum_span_days'] > result['window_days']
            or result['minimum_history_span_days'] > result['history_days']):
        raise ValueError('Repeated declines require distinct events across a valid time window')
    return result


def _utc(value):
    if not isinstance(value, datetime) or value.tzinfo is None:
        raise ValueError('Evidence requires an aware timestamp')
    return value.astimezone(timezone.utc)


def _hash(value):
    return hashlib.sha256(value.encode()).hexdigest()


def _evidence(session, outreach, incoming_id, now):
    """Validate the actual routed response, not a bare historical response code."""
    if not outreach or outreach.response != 'no' or not outreach.responded_at:
        return None
    person = session.get(m.Volunteer, outreach.volunteer_id)
    incoming = session.get(m.Message, incoming_id)
    outgoing = session.get(m.Message, outreach.message_id) if outreach.message_id else None
    fill = session.get(m.FillRequest, outreach.fill_request_id)
    shift = session.get(m.Shift, fill.shift_id) if fill else None
    event = shift.interval_event if shift else None
    offer = session.get(m.Notification, f'offer:{outreach.id}')
    if offer is not None and not isinstance(offer.detail, dict):
        return None
    if (not person or not incoming or not outgoing or not event or not offer
            or incoming.direction != 'in' or incoming.status != 'received'
            or incoming.volunteer_id != person.id or incoming.phone != person.phone
            or control_action(incoming.body) is not None
            or outgoing.direction != 'out' or outgoing.purpose != 'outreach'
            or outgoing.volunteer_id != person.id or outgoing.phone != person.phone
            or outgoing.status not in {'sent', 'submitted', 'delivered'} or not outgoing.provider_sid
            or offer.purpose != 'offer_window' or offer.state != 'offer_active'
            or offer.message_id != outgoing.id or offer.volunteer_id != person.id
            or offer.event_id != event.id or offer.body != outgoing.body
            or offer.detail.get('fill_request_id') != fill.id or offer.detail.get('shift_id') != shift.id):
        return None
    try:
        dispatched = _utc(datetime.fromisoformat(offer.detail['dispatched_at']))
        replied = _utc(outreach.responded_at)
        received = _utc(incoming.created_at)
        deadline = _utc(offer.expires_at)
        if not (_utc(outgoing.created_at) <= dispatched <= received <= replied <= _utc(now)
                and replied < deadline and replied < _utc(event.starts_at)):
            return None
    except (ValueError, TypeError, KeyError):
        return None
    selected = session.info.get('mac_test_session')
    if selected and (incoming.purpose != 'test:' + selected.id
            or not outgoing.provider_sid.startswith(selected.outbound_prefix)):
        return None
    if outgoing.provider_sid.startswith('MAC'):
        prefix = outgoing.provider_sid.split(':', 1)[0]
        if incoming.kind != 'mac_test_in' or incoming.purpose != 'test:' + prefix[3:]:
            return None
    return {'outreach_id': outreach.id, 'volunteer_id': person.id, 'fill_request_id': fill.id,
        'shift_id': shift.id, 'event_id': event.id, 'event_starts_at': _utc(event.starts_at).isoformat(),
        'offer_message_id': outgoing.id, 'incoming_message_id': incoming.id,
        'offer_body_hash': _hash(outgoing.body), 'reply_body_hash': _hash(incoming.body),
        'incoming_scope': incoming.purpose, 'dispatched_at': dispatched.isoformat(),
        'received_at': received.isoformat(), 'responded_at': replied.isoformat(),
        'delivery_evidence': 'provider_delivered' if outgoing.status == 'delivered' else 'submitted_offer_with_actual_scoped_decline'}


def record_decline(session, outreach, incoming_id, now):
    """Hook only after a real scoped decline is accepted, in its transaction."""
    detail = _evidence(session, outreach, incoming_id, now)
    if detail is None:
        return None
    key = f'repeated-decline:{outreach.id}'
    prior = session.get(m.Notification, key)
    if prior:
        return prior if prior.detail == detail else None  # Never replace the original response proof.
    row = m.Notification(key=key, purpose='decline_evidence', state='recorded',
        volunteer_id=outreach.volunteer_id, event_id=detail['event_id'], message_id=incoming_id,
        body='', due_at=now, created_at=now, detail=detail)
    session.add(row)
    session.flush()
    return row


def refresh(session, now, *, config=None):
    """Bounded current evidence and deduplicated flags. No model or provider calls."""
    try:
        settings = configuration(session, config)
        now = _utc(now)
    except (ValueError, TypeError):
        return {'flags': [], 'held': 'invalid_repeated_decline_policy'}
    start = now - timedelta(days=settings['window_days'])
    proofs = list(session.scalars(select(m.Notification).where(m.Notification.purpose == 'decline_evidence',
        m.Notification.created_at >= start, m.Notification.created_at <= now)
        .order_by(m.Notification.created_at, m.Notification.key).limit(settings['maximum_records'] + 1)))
    history = list(session.scalars(select(m.Assignment).join(m.Shift).join(m.Event).where(
        m.Assignment.status == 'completed', m.Event.status == 'completed',
        m.Shift.starts_at >= now - timedelta(days=settings['history_days']),
        m.Shift.ends_at <= now).order_by(m.Assignment.id).limit(settings['maximum_records'] + 1)))
    if len(proofs) > settings['maximum_records'] or len(history) > settings['maximum_records']:
        return {'flags': [], 'held': 'repeated_decline_evidence_limit'}  # Do not resolve from a truncated scan.
    by_person = {}
    for proof in proofs:
        detail = proof.detail or {}
        outreach = session.get(m.Outreach, detail.get('outreach_id'))
        fresh = _evidence(session, outreach, detail.get('incoming_message_id'), now)
        if (fresh is None or fresh != detail or proof.key != f'repeated-decline:{outreach.id}'
                or proof.state != 'recorded' or proof.message_id != detail['incoming_message_id']
                or proof.volunteer_id != detail['volunteer_id'] or proof.event_id != detail['event_id']):
            continue
        replied = datetime.fromisoformat(detail['responded_at'])
        if replied >= start:
            bucket = by_person.setdefault(detail['volunteer_id'], {})
            # Multiple roles, retries or fills on one event are one observation.
            if detail['event_id'] not in bucket or replied > datetime.fromisoformat(bucket[detail['event_id']]['responded_at']):
                bucket[detail['event_id']] = detail
    existing = {row.evidence.get('key'): row for row in session.scalars(select(m.Flag).where(m.Flag.type == TYPE))}
    active, flags = set(), []
    for identifier, events in by_person.items():
        person = session.get(m.Volunteer, identifier)
        stopped = session.get(m.Policy, 'sms_opt_out:' + person.phone) if person else None
        if (not person or not person.sms_opt_in or person.status != 'active'
                or person.is_coordinator or person.is_pastor or stopped and stopped.value.get('value')):
            continue
        evidence = sorted(events.values(), key=lambda item: item['responded_at'])
        first, last = (datetime.fromisoformat(evidence[i]['responded_at']) for i in (0, -1))
        prior = {assignment.shift.event_id: assignment.shift.starts_at for assignment in history
            if assignment.volunteer_id == identifier and assignment.shift.ends_at < first}
        dates = sorted(prior.values())
        if (len(evidence) < settings['minimum_events'] or last - first < timedelta(days=settings['minimum_span_days'])
                or len(dates) < settings['minimum_history_events']
                or dates[-1] - dates[0] < timedelta(days=settings['minimum_history_span_days'])):
            continue
        key = f'{TYPE}:{identifier}'; active.add(key)
        flag = existing.get(key)
        snapshot = {'key': key, 'volunteer_id': identifier, 'policy': settings, 'current': True,
            'as_of': now.isoformat(), 'declines': evidence,
            'completed_history_event_ids': sorted(prior),
            'completed_history_dates': [day.isoformat() for day in dates]}
        if flag is None:
            flag = m.Flag(type=TYPE, kind='concern', status='open', created_at=now, evidence=snapshot,
                summary='', suggested_action='A coordinator may review preferences personally. No automated text or pressure.')
            session.add(flag)
        if flag.status == 'resolved':
            flag.status = 'open'
        flag.evidence = snapshot
        flag.summary = f'{person.name} declined {len(evidence)} distinct event offers across {(last-first).days} days.'
        if flag.status != 'dismissed':
            flags.append(flag)
    for key, flag in existing.items():
        if key not in active and flag.status != 'dismissed':
            flag.status = 'resolved'
            flag.evidence = {**flag.evidence, 'current': False, 'as_of': now.isoformat(),
                'resolution': 'Current verified evidence or recipient eligibility no longer meets the configured threshold'}
    session.flush()
    return {'flags': flags, 'held': None}
