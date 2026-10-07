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
    if value.get('facts_hash') != digest(facts) or facts.get('volunteer_id') != volunteer.id:
        return True
    if facts.get('source_kind') == 'planning_center_transition':
        return _native_source_problem(session, volunteer, value)
    if facts.get('version') != 1:
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


def record_native_decline(session, assignment, link, scope, after, now):
    """Save an observed native C/U-to-D transition, without an inbound message.

    The immutable evidence and interval are committed with cancellation. Later
    planner edits and repeated polls cannot erase or reactivate this refusal.
    """
    from app.integrations.planning_center import PCOVolunteerPerson
    before = link.remote_snapshot
    volunteer = assignment.volunteer
    mapping = session.scalar(select(PCOVolunteerPerson).where(
        PCOVolunteerPerson.organization_id == link.organization_id,
        PCOVolunteerPerson.volunteer_id == volunteer.id))
    start, end = instant(assignment.shift.starts_at), instant(assignment.shift.ends_at)
    identity = (scope.organization_id, scope.service_type_id, scope.plan_id, scope.team_id)
    if (not isinstance(before, dict) or not isinstance(after, dict)
            or assignment.status not in ('proposed', 'approved', 'confirmed')
            or link.remote_status not in ('C', 'U') or before.get('status') != link.remote_status
            or after.get('status') != 'D' or not mapping or mapping.person_id != link.person_id
            or identity != (link.organization_id, link.service_type_id, link.plan_id, link.team_id)
            or assignment.id != link.assignment_id or assignment.shift.event_id != scope.event_id
            or assignment.shift.role_id != scope.role_id or start >= end
            or instant(link.verified_at) > instant(now)
            or any(row.get('id') != link.plan_person_id or str(row.get('person')) != link.person_id
                   or str(row.get('team')) != link.team_id or row.get('position') != scope.position_name
                   for row in (before, after))):
        raise ValueError('Verified native decline transition and exact interval are required')
    proof = {'organization_id': link.organization_id, 'service_type_id': link.service_type_id,
             'plan_id': link.plan_id, 'team_id': link.team_id, 'position_id': scope.position_id,
             'position_name': scope.position_name, 'person_id': link.person_id,
             'plan_person_id': link.plan_person_id, 'volunteer_id': volunteer.id,
             'assignment_id': assignment.id, 'shift_id': assignment.shift_id,
             'event_id': assignment.shift.event_id, 'starts_at': start.isoformat(), 'ends_at': end.isoformat(),
             'before': dict(before), 'after': dict(after), 'before_verified_at': instant(link.verified_at).isoformat(),
             'original_status': assignment.status, 'original_updated_at': instant(assignment.updated_at).isoformat()}
    proof_hash = digest(proof)
    source_key = 'pco-decline-transition:' + proof_hash
    source = session.get(m.Policy, source_key)
    if source:
        if source.value.get('proof') != proof or source.value.get('proof_hash') != proof_hash:
            raise ValueError('Native decline evidence changed; review original source')
    else:
        session.add(m.Policy(key=source_key, value={'proof': proof, 'proof_hash': proof_hash,
            'observed_at': instant(now).isoformat()}))
    facts = {'version': 2, 'source_kind': 'planning_center_transition', 'volunteer_id': volunteer.id,
             'assignment_id': assignment.id, 'shift_id': assignment.shift_id, 'event_id': assignment.shift.event_id,
             'starts_at': start.isoformat(), 'ends_at': end.isoformat(),
             'phone_hash': hashlib.sha256(volunteer.phone.encode()).hexdigest(),
             'source_transition_key': source_key, 'source_transition_hash': proof_hash}
    key = PREFIX + str(volunteer.id) + ':' + str(assignment.id) + ':native-' + proof_hash
    prior = session.get(m.Policy, key)
    if prior:
        if prior.value.get('facts') != facts or prior.value.get('facts_hash') != digest(facts):
            raise ValueError('Native decline refusal changed; review original source')
        return prior  # A replay cannot undo an explicitly reviewed reversal.
    row = m.Policy(key=key, value={'facts': facts, 'facts_hash': digest(facts), 'state': 'active',
        'recorded_at': instant(now).isoformat()})
    session.add(row); session.flush()
    return row


def _native_source_problem(session, volunteer, value):
    facts = value['facts']
    if (facts.get('version') != 2 or facts.get('phone_hash') != hashlib.sha256(volunteer.phone.encode()).hexdigest()
            or not isinstance(facts.get('source_transition_key'), str)):
        return True
    source = session.get(m.Policy, facts['source_transition_key'])
    if not source or not isinstance(source.value, dict):
        return True
    proof = source.value.get('proof')
    if not isinstance(proof, dict):
        return True
    expected = digest(proof)
    if (source.key != 'pco-decline-transition:' + expected or source.value.get('proof_hash') != expected
            or facts.get('source_transition_hash') != expected
            or any(proof.get(k) != facts.get(k) for k in
                   ('volunteer_id', 'assignment_id', 'shift_id', 'event_id', 'starts_at', 'ends_at'))
            or proof.get('original_status') not in ('proposed', 'approved', 'confirmed')
            or instant(proof['before_verified_at']) > instant(source.value['observed_at'])
            or instant(source.value['observed_at']) > instant(value['recorded_at'])):
        return True
    before, after = proof.get('before'), proof.get('after')
    if (not isinstance(before, dict) or not isinstance(after, dict)
            or before.get('status') not in ('C', 'U') or after.get('status') != 'D'):
        return True
    return any(not isinstance(proof.get(k), str) or not proof[k].isdigit() for k in
               ('organization_id', 'service_type_id', 'plan_id', 'team_id', 'position_id', 'person_id', 'plan_person_id')) or any(
        row.get('id') != proof['plan_person_id'] or str(row.get('person')) != proof['person_id']
        or str(row.get('team')) != proof['team_id'] or row.get('position') != proof['position_name']
        for row in (before, after))


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
            except (KeyError, TypeError, ValueError, AttributeError):
                invalid = True
            if value.get('state') != 'active' or invalid:
                return 'Prior service cancellation needs source review'
            return ('Declined this service interval in Planning Center'
                    if facts.get('source_kind') == 'planning_center_transition'
                    else 'Sender cancelled this service interval')
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
