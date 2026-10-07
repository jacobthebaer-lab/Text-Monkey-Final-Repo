"""Source-bound service interval refusals, independent of planner cancellations."""
import hashlib
import json
from datetime import datetime, timezone
from sqlalchemy import select
from app.db import models as m

PREFIX = 'cancellation-refusal:'


def digest(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, separators=(',', ':')).encode()).hexdigest()


def instant(value):
    result = datetime.fromisoformat(value) if isinstance(value, str) else value
    if not isinstance(result, datetime) or result.tzinfo is None:
        raise ValueError('A timezone-aware cancellation interval is required')
    return result.astimezone(timezone.utc)


def _input(ctx, volunteer, incoming_id):
    from app.core.cancellation_scope import review_source
    source = review_source(ctx.session, volunteer.id, incoming_id, ctx.provider,
                          ctx.session.info.get('mac_test_session'), ctx.clock.now())
    if source is None:
        raise ValueError('Actual cancellation sender and input are required')
    receipt = None
    selected = ctx.session.info.get('mac_test_session')
    if selected:
        from app.integrations.mac_models import MacInboundReceipt
        fingerprints = {hashlib.sha256((volunteer.phone + '\0' + service + '\0' + selected.id + '\0' + body).encode()).hexdigest()
                        for service in ('iMessage', 'SMS') for body in (source.body, selected.prefix + source.body)}
        receipts = list(ctx.session.scalars(select(MacInboundReceipt).where(MacInboundReceipt.fingerprint.in_(fingerprints))))
        receipts = [r for r in receipts if not r.result or r.result.get('session_id') == selected.id]
        if not receipts:
            raise ValueError('Recorded Mac receipt must match the cancellation input')
        receipt = sorted(receipts, key=lambda r: r.guid)[0]
    return source, receipt


def _save(ctx, volunteer, assignment, source, receipt, start, end, original_status, original_updated_at=None):
    start, end = instant(start), instant(end)
    if start >= end or source.created_at > ctx.clock.now() or original_status not in ('proposed', 'approved', 'confirmed'):
        raise ValueError('Current sender booking and original interval are required')
    facts = {'version': 1, 'volunteer_id': volunteer.id, 'assignment_id': assignment.id,
             'shift_id': assignment.shift_id, 'event_id': assignment.shift.event_id,
             'starts_at': start.isoformat(), 'ends_at': end.isoformat(),
             'source_message_id': source.id, 'source_body_hash': hashlib.sha256(source.body.encode()).hexdigest(),
             'source_created_at': instant(source.created_at).isoformat(), 'source_kind': source.kind,
             'source_purpose': source.purpose,
             'source_session_id': getattr(ctx.session.info.get('mac_test_session'), 'id', None), 'phone_hash': hashlib.sha256(source.phone.encode()).hexdigest(),
             'source_guid': receipt.guid if receipt else None, 'source_fingerprint': receipt.fingerprint if receipt else None,
             'original_status': original_status,
             'original_updated_at': instant(original_updated_at or assignment.updated_at).isoformat()}
    key = PREFIX + str(volunteer.id) + ':' + str(assignment.id) + ':' + str(source.id)
    prior = ctx.session.get(m.Policy, key)
    if prior:
        if prior.value.get('facts') != facts or prior.value.get('facts_hash') != digest(facts):
            raise ValueError('Cancellation refusal changed; review its original source')
        return prior  # A replay cannot reactivate a specifically reviewed reversal.
    row = m.Policy(key=key, value={'facts': facts, 'facts_hash': digest(facts), 'state': 'active',
                                  'recorded_at': ctx.clock.now().isoformat()})
    ctx.session.add(row)
    ctx.session.flush()
    return row


def record(ctx, volunteer, assignment):
    """Called before normal cancellation, only for an actual sender instruction."""
    if (ctx.session.info.get('sender_phone') != volunteer.phone
            or ctx.session.info.get('sender_schedule_action') != 'cancel'
            or ctx.reply_to_message_id is None):
        return None  # Admin replanning and legacy calls do not imply unavailability.
    source, receipt = _input(ctx, volunteer, ctx.reply_to_message_id)
    return _save(ctx, volunteer, assignment, source, receipt,
                 assignment.shift.starts_at, assignment.shift.ends_at, assignment.status)


def backfill(ctx, volunteer, assignment_id, incoming_id):
    """Restore a verified resolved sender cancellation, without replay or delivery."""
    assignment = ctx.session.get(m.Assignment, assignment_id)
    source, receipt = _input(ctx, volunteer, incoming_id)
    hold = ctx.session.get(m.Notification, 'cancellation-scope:' + str(volunteer.id))
    fills = list(ctx.session.scalars(select(m.FillRequest).where(m.FillRequest.cancelled_assignment_id == assignment_id)))
    if (not assignment or assignment.volunteer_id != volunteer.id or assignment.status != 'cancelled'
            or len(fills) != 1 or fills[0].shift_id != assignment.shift_id
            or not hold or hold.volunteer_id != volunteer.id or hold.state != 'resolved'
            or hold.purpose != 'cancellation_scope' or hold.detail.get('phone') != volunteer.phone
            or hold.detail.get('source_message_id') != incoming_id
            or hold.detail.get('source_body_hash') != hashlib.sha256(source.body.encode()).hexdigest()
            or hold.detail.get('session_id') != getattr(ctx.session.info.get('mac_test_session'), 'id', None)
            or hold.detail.get('assignment_id') != assignment_id or hold.detail.get('resolved_message_id') != incoming_id
            or source.created_at > fills[0].created_at or fills[0].created_at > ctx.clock.now()):
        raise ValueError('Resolved cancellation and its exact fill request are required')
    original = [r for r in hold.detail.get('bookings', []) if isinstance(r, list) and len(r) == 8
                and r[0] == assignment_id and r[1] == assignment.shift_id and r[4] == assignment.shift.role_id and r[7] == 'scheduled']
    if len(original) != 1:
        raise ValueError('Immutable original booking interval is required')
    return _save(ctx, volunteer, assignment, source, receipt, original[0][5], original[0][6], original[0][2], original[0][3])


def _source_problem(session, volunteer, value):
    facts = value.get('facts', {})
    if value.get('facts_hash') != digest(facts) or facts.get('version') != 1 or facts.get('volunteer_id') != volunteer.id:
        return True
    source = session.get(m.Message, facts.get('source_message_id'))
    if (not source or source.direction != 'in' or source.status != 'received' or source.volunteer_id != volunteer.id
            or source.phone != volunteer.phone or facts.get('phone_hash') != hashlib.sha256(source.phone.encode()).hexdigest()
            or facts.get('source_body_hash') != hashlib.sha256(source.body.encode()).hexdigest()
            or facts.get('source_created_at') != instant(source.created_at).isoformat()
            or instant(source.created_at) > instant(value['recorded_at'])
            or facts.get('source_kind') != source.kind or facts.get('source_purpose') != source.purpose):
        return True
    if facts.get('source_guid'):
        from app.integrations.mac_models import MacInboundReceipt
        receipt = session.get(MacInboundReceipt, facts['source_guid'])
        if (not receipt or receipt.fingerprint != facts.get('source_fingerprint')
                or receipt.result and receipt.result.get('session_id') != facts.get('source_session_id')):
            return True
    return False


def problem(session, volunteer, shift):
    """All roles overlapping the refused interval are held, not the whole day."""
    rows = session.scalars(select(m.Policy).where(m.Policy.key.startswith(PREFIX + str(volunteer.id) + ':')))
    for row in rows:
        value = row.value if isinstance(row.value, dict) else {}
        if value.get('state') == 'revoked' and _valid_reversal(value):
            continue
        facts = value.get('facts') if isinstance(value.get('facts'), dict) else {}
        try:
            start, end = instant(facts['starts_at']), instant(facts['ends_at'])
        except (KeyError, TypeError, ValueError):
            try:
                assignment_id = int(row.key.split(':')[2])
            except ValueError:
                continue
            original = session.get(m.Assignment, assignment_id)
            if original and original.volunteer_id == volunteer.id and original.shift.event_id == shift.event_id:
                return 'Prior service cancellation needs source review'
            continue
        if start < instant(shift.ends_at) and end > instant(shift.starts_at):
            try:
                invalid = _source_problem(session, volunteer, value)
            except (TypeError, ValueError, AttributeError):
                invalid = True
            if value.get('state') != 'active' or invalid:
                return 'Prior service cancellation needs source review'
            return 'Sender cancelled this service interval'
    return None


def _valid_reversal(value):
    review = value.get('reversal')
    if not isinstance(review, dict) or not all(isinstance(review.get(k), str) and review[k].strip() for k in ('actor', 'reason')):
        return False
    original = {k: v for k, v in value.items() if k != 'reversal'}
    original['state'] = 'active'
    try:
        return (review.get('reviewed_hash') == digest(original)
                and instant(review['at']) >= instant(value['recorded_at']))
    except (KeyError, TypeError, ValueError):
        return False


def revoke(session, key, *, expected, actor, reason, now):
    """Explicit narrow operator record decision; never inferred by planning/YES."""
    if (session.info.get('record_authorized') is not True or not isinstance(actor, str) or not actor.strip()
            or not isinstance(reason, str) or not reason.strip()):
        raise ValueError('Explicit scoped record review is required')
    row = session.scalar(select(m.Policy).where(m.Policy.key == key).with_for_update().execution_options(populate_existing=True))
    if (not row or not key.startswith(PREFIX) or digest(row.value) != expected or row.value.get('state') != 'active'
            or instant(now) < instant(row.value['recorded_at'])):
        raise ValueError('Cancellation reversal changed; request a new exact review')
    row.value = {**row.value, 'state': 'revoked', 'reversal': {'actor': actor, 'reason': reason,
                 'at': instant(now).isoformat(), 'reviewed_hash': expected}}
    session.flush()
    return row
