"""Reviewed partitioning and source-bound child bookings. No transport calls.

Partition approval creates genuine child Shifts, never Events or assignments.
Actual acceptance of a fresh child offer is held for a final atomic booking
review. Subsequent cancellation/replacement operates on the same child slot.
"""
from datetime import datetime, timedelta, timezone
from hashlib import sha256
import json

from sqlalchemy import event, inspect, select
from sqlalchemy.orm import Session, object_session

from app.config import get_settings
from app.core import offer_windows as offers
from app.db import models as m

PARTITION = 'confirm_split_partition'
BOOKING = 'confirm_split_booking'
ACTIVE = ('proposed', 'approved', 'confirmed')


def fingerprint(value):
    return sha256(json.dumps(value, sort_keys=True, separators=(',', ':'), ensure_ascii=False).encode()).hexdigest()


def instant(value):
    if type(value) is not str:
        raise ValueError('A dated interval with explicit timezone is required')
    parsed = datetime.fromisoformat(value)
    if parsed.tzinfo is None:
        raise ValueError('A dated interval with explicit timezone is required')
    return parsed.astimezone(timezone.utc)


def role_enabled(session, role_id):
    policy = session.get(m.Policy, f'split_role:{role_id}')
    return policy is not None and policy.value == {'value': True}


def source_scope(session, incoming, outgoing, now):
    selected = session.info.get('split_sessions', {}).get(incoming.phone) or session.info.get('mac_test_session')
    if incoming.purpose and incoming.purpose.startswith('test:'):
        if (not selected or not selected.active(now) or incoming.purpose != 'test:'+selected.id
                or incoming.created_at < selected.starts_at or outgoing.created_at < selected.starts_at
                or not (outgoing.provider_sid or '').startswith(selected.outbound_prefix)):
            raise ValueError('The actual input and delivered offer require the current active transport session')
        return {'session_id': selected.id, 'starts_at': selected.starts_at.isoformat(),
            'expires_at': selected.expires_at.isoformat() if selected.expires_at else None, 'outbound_prefix': selected.outbound_prefix}
    if selected:
        raise ValueError('Legacy input cannot authorize a connected split-coverage action')
    return None


def children(session, parent_id):
    return session.scalars(select(m.Shift).where(m.Shift.parent_shift_id == parent_id)
        .order_by(m.Shift.interval_starts_at, m.Shift.id)).all()


def imported(shift):
    # Imported slots remain held until an external partial-time contract exists.
    if (shift.event.gcal_event_id or '').startswith('pco:'):
        return True
    from sqlalchemy import inspect as db_inspect
    if db_inspect(object_session(shift).connection()).has_table('pco_event_links'):
        from app.integrations.planning_center import PCOEventLink
        return object_session(shift).scalar(select(PCOEventLink.key).where(
            PCOEventLink.event_id == shift.event_id)) is not None
    return False


def parent_source(session, shift):
    return {**offers.snapshot(shift), 'parent_id': shift.id, 'event_id': shift.event_id,
        'event_type_id': shift.event.event_type_id, 'slot_index': shift.slot_index,
        'event_status': shift.event.status, 'role_split_enabled': role_enabled(session, shift.role_id)}


def partition_problem(session, parent, now):
    if parent is None or parent.parent_shift_id is not None:
        return 'Choose an original whole slot'
    if not role_enabled(session, parent.role_id):
        return 'This role does not explicitly allow reviewed split coverage'
    if imported(parent):
        return 'Planning Center partial-time mapping is not verified; imported slots remain held'
    if parent.event.status != 'scheduled' or parent.starts_at <= now:
        return 'The parent slot is no longer upcoming'
    if session.scalar(select(m.Assignment.id).where(m.Assignment.shift_id == parent.id,
            m.Assignment.status.in_(ACTIVE))):
        return 'Cancel or resolve the existing full-slot assignment before partition review'
    if children(session, parent.id):
        return 'This slot already has a reviewed partition'
    return None


def partial_facts(session, parent_id, outreach_id, incoming_id, now):
    parent = session.get(m.Shift, parent_id)
    if error := partition_problem(session, parent, now):
        raise ValueError(error)
    outreach = session.get(m.Outreach, outreach_id)
    fill = session.get(m.FillRequest, outreach.fill_request_id) if outreach else None
    metadata = offers.metadata(session, outreach) if outreach else None
    incoming = session.get(m.Message, incoming_id)
    outgoing = session.get(m.Message, outreach.message_id) if outreach and outreach.message_id else None
    if (not outreach or not fill or fill.shift_id != parent.id or outreach.response not in ('partial', 'expired')
            or not incoming or incoming.direction != 'in' or incoming.status != 'received'
            or incoming.volunteer_id != outreach.volunteer_id or not outgoing
            or incoming.phone != outgoing.phone or outgoing.direction != 'out'
            or outgoing.purpose != 'outreach' or outgoing.status not in ('sent', 'submitted') or not outgoing.provider_sid
            or not metadata or metadata.detail.get('snapshot') != offers.snapshot(parent)
            or not metadata.expires_at or not outgoing.created_at <= incoming.created_at < metadata.expires_at
            or incoming.created_at > now):
        raise ValueError('An actual timely partial reply to this exact delivered slot offer is required')
    volunteer = session.get(m.Volunteer, outreach.volunteer_id)
    if (not volunteer or incoming.phone != volunteer.phone or not volunteer.sms_opt_in
            or session.get(m.Policy, 'sms_opt_out:'+volunteer.phone)):
        raise ValueError('The partial sender no longer consents')
    latest = session.scalar(select(m.Message.id).where(m.Message.phone == incoming.phone,
        m.Message.direction == 'in').order_by(m.Message.id.desc()).limit(1))
    if latest != incoming.id:
        raise ValueError('A newer sender reply requires a fresh interpretation')
    return {'parent': parent_source(session, parent), 'outreach_id': outreach.id,
        'incoming_id': incoming.id, 'outgoing_id': outgoing.id, 'phone': incoming.phone,
        'volunteer_id': volunteer.id, 'body': incoming.body,
        'session': source_scope(session, incoming, outgoing, now),
        'body_hash': sha256(incoming.body.encode()).hexdigest(),
        'outgoing_hash': sha256(outgoing.body.encode()).hexdigest(),
        'reply_at': incoming.created_at.isoformat(), 'offer_expires_at': metadata.expires_at.isoformat()}


def extract_partial(gloo, facts):
    """Pure network step; call only after releasing the source-read transaction."""
    response = gloo.create_response(model=getattr(gloo, 'settings', get_settings()).parser_model, input=json.dumps(facts),
        instructions='Interpret only the actual body of this partial reply against its dated offered slot. '
        'Return strict JSON {"start":"ISO8601 with timezone","end":"ISO8601 with timezone"}, '
        'or {"start":null,"end":null} if either exact boundary is ambiguous. Do not invent availability, '
        'dates, hours, consent or qualifications. This is an internal partition proposal, not a booking.',
        max_output_tokens=300)
    try:
        data = json.loads(response.output_text)
        if set(data) != {'start', 'end'}:
            raise ValueError
        start, end = instant(data['start']), instant(data['end'])
    except (TypeError, ValueError, AttributeError) as error:
        raise ValueError('Gloo did not provide an unambiguous dated partial interval') from error
    parent = facts['parent']
    left, right = instant(parent['start']), instant(parent['end'])
    if not left <= start < end <= right or (start, end) == (left, right):
        raise ValueError('A strict partial interval inside the actual full slot is required')
    return {'start': start.isoformat(), 'end': end.isoformat(), 'source_hash': fingerprint(facts),
        'gloo_output': response.output_text}


def digest(payload):
    return fingerprint({key: value for key, value in payload.items()
        if key not in {'content_hash', 'applied_child_ids', 'applied_assignment_ids'}})


def review_valid(review, owner, expected, now):
    try:
        return (review.kind in (PARTITION, BOOKING) and review.payload['owner'] == owner
            and review.payload['content_hash'] == expected == digest(review.payload)
            and now < instant(review.payload['expires_at']))
    except (KeyError, TypeError, ValueError):
        return False


def stage_partition(session, owner, facts, normalized, now):
    fresh = partial_facts(session, facts['parent']['parent_id'], facts['outreach_id'], facts['incoming_id'], now)
    if fresh != facts or normalized.get('source_hash') != fingerprint(facts):
        raise ValueError('The actual partial source changed during interpretation')
    intervals = partition_intervals(facts, normalized)
    left = instant(facts['parent']['start'])
    payload = {'action': 'partition_slot', 'owner': owner, 'source': facts,
        'normalized': normalized, 'intervals': intervals,
        'expires_at': min(now+timedelta(hours=2), left).isoformat()}
    payload['content_hash'] = digest(payload)
    prior = session.scalar(select(m.Approval).where(m.Approval.kind == PARTITION,
        m.Approval.status == 'pending', m.Approval.payload['content_hash'].as_string() == payload['content_hash']))
    if prior:
        return prior
    review = m.Approval(kind=PARTITION, status='pending', payload=payload, requested_at=now)
    session.add(review); session.flush()
    return review


def partition_intervals(facts, normalized):
    parent = facts['parent']
    left, right = instant(parent['start']), instant(parent['end'])
    start, end = instant(normalized['start']), instant(normalized['end'])
    if not left <= start < end <= right or (start, end) == (left, right):
        raise ValueError('Invalid source-bound partial interval')
    points = sorted({left, start, end, right})
    return [{'start': a.isoformat(), 'end': b.isoformat()} for a, b in zip(points, points[1:])]


def child_problem(session, child):
    if child.parent_shift_id is None:
        return None
    parent = session.get(m.Shift, child.parent_shift_id)
    review = session.get(m.Approval, child.coverage_review_id)
    if (not parent or parent.parent_shift_id is not None or not review or review.kind != PARTITION
            or review.status != 'approved' or review.payload.get('content_hash') != digest(review.payload)
            or not role_enabled(session, parent.role_id) or imported(parent)
            or child.event_id != parent.event_id or child.role_id != parent.role_id
            or parent_source(session, parent) != review.payload['source']['parent']
            or {'start': child.starts_at.isoformat(), 'end': child.ends_at.isoformat()} not in review.payload['intervals']
            or child.id not in review.payload.get('applied_child_ids', [])):
        return 'Reviewed child interval or parent scope changed; split coverage is held'
    return None


def pending_child(session, child):
    if child.parent_shift_id is None:
        return False
    review = session.get(m.Approval, child.coverage_review_id)
    return review is not None and not session.scalar(select(m.Notification.key).where(
        m.Notification.key == f'split_applied:{review.id}'))


def note_acceptance(session, child, outreach, incoming_id, now):
    incoming = session.get(m.Message, incoming_id) if incoming_id else None
    volunteer = session.get(m.Volunteer, outreach.volunteer_id)
    metadata = offers.metadata(session, outreach)
    outgoing = session.get(m.Message, outreach.message_id) if outreach.message_id else None
    if (not incoming or not volunteer or incoming.direction != 'in' or incoming.status != 'received'
            or incoming.volunteer_id != volunteer.id or incoming.phone != volunteer.phone
            or not metadata or not outgoing or metadata.detail.get('snapshot') != offers.snapshot(child)
            or outgoing.status not in ('sent', 'submitted') or outgoing.phone != volunteer.phone
            or not outgoing.created_at <= incoming.created_at <= now < metadata.expires_at):
        raise ValueError('A timely actual acceptance of this exact delivered child interval is required')
    detail = {'child_id': child.id, 'outreach_id': outreach.id, 'incoming_id': incoming.id,
        'session': source_scope(session, incoming, outgoing, now),
        'volunteer_id': volunteer.id, 'phone': incoming.phone, 'body_hash': sha256(incoming.body.encode()).hexdigest(),
        'outgoing_id': outgoing.id, 'outgoing_hash': sha256(outgoing.body.encode()).hexdigest(),
        'snapshot': offers.snapshot(child), 'expires_at': metadata.expires_at.isoformat()}
    key = f'split_accept:{outreach.id}'
    prior = session.get(m.Notification, key)
    if prior and prior.detail != detail:
        raise ValueError('This child acceptance source is already reserved')
    if prior is None:
        session.add(m.Notification(key=key, purpose='split_acceptance', body='', state='held', detail=detail,
            created_at=now, due_at=now, expires_at=metadata.expires_at))
    outreach.response, outreach.responded_at = 'yes', now
    fill = session.get(m.FillRequest, outreach.fill_request_id)
    fill.state, fill.next_action_at = 'waiting_split_review', None
    session.flush()
    return detail


def acceptance_problem(session, child, detail, now):
    from app.core import eligibility, scheduler
    from app.core.send_gate import has_open_sensitive_escalation
    if error := child_problem(session, child):
        return error
    incoming = session.get(m.Message, detail['incoming_id'])
    outgoing = session.get(m.Message, detail['outgoing_id'])
    outreach = session.get(m.Outreach, detail['outreach_id'])
    volunteer = session.get(m.Volunteer, detail['volunteer_id'])
    latest = session.scalar(select(m.Message.id).where(m.Message.phone == detail['phone'],
        m.Message.direction == 'in').order_by(m.Message.id.desc()).limit(1))
    proof = session.get(m.Notification, f"split_accept:{detail['outreach_id']}")
    fill = session.get(m.FillRequest, outreach.fill_request_id) if outreach else None
    if (not incoming or latest != incoming.id or incoming.phone != detail['phone']
            or incoming.direction != 'in' or incoming.volunteer_id != detail['volunteer_id']
            or sha256(incoming.body.encode()).hexdigest() != detail['body_hash']
            or incoming.status != 'received' or not outgoing or outgoing.status not in ('sent', 'submitted') or not outgoing.provider_sid
            or outgoing.direction != 'out' or outgoing.purpose != 'outreach'
            or outgoing.phone != detail['phone'] or sha256(outgoing.body.encode()).hexdigest() != detail['outgoing_hash']
            or not outreach or outreach.response != 'yes' or outreach.volunteer_id != detail['volunteer_id']
            or outreach.message_id != outgoing.id or not fill or fill.shift_id != child.id
            or detail['child_id'] != child.id or not proof or proof.detail != detail
            or not volunteer or volunteer.phone != detail['phone'] or not volunteer.sms_opt_in
            or session.get(m.Policy, 'sms_opt_out:'+detail['phone'])
            or now >= instant(detail['expires_at']) or offers.snapshot(child) != detail['snapshot']
            or has_open_sensitive_escalation(session, volunteer.id) or volunteer.is_coordinator or volunteer.is_pastor):
        return 'Child acceptance is stale, changed, expired or no longer authorized'
    try:
        if source_scope(session, incoming, outgoing, now) != detail.get('session'):
            return 'The selected native session changed'
    except ValueError as error:
        return str(error)
    from app.core.policies import PolicyStore
    tz = str(PolicyStore(session).church_tz())
    result = eligibility.check(session, volunteer, child, tz)
    if not result:
        return '; '.join(result.reasons)
    return scheduler.monthly_problem(session, volunteer, child, tz)


def stage_booking(session, owner, parent_id, now):
    parent = session.get(m.Shift, parent_id)
    slots = children(session, parent_id)
    if not parent or not 2 <= len(slots) <= 3 or any(not pending_child(session, s) for s in slots):
        raise ValueError('Choose a reviewed partition awaiting its first atomic booking')
    bindings = []
    for child in slots:
        matches = session.scalars(select(m.Notification).where(m.Notification.purpose == 'split_acceptance',
            m.Notification.detail['child_id'].as_integer() == child.id)).all()
        current = [r.detail for r in matches if acceptance_problem(session, child, r.detail, now) is None]
        if len(current) != 1:
            raise ValueError('Every child needs one current actual acceptance of its exact interval')
        bindings.append(current[0])
    if len({b['volunteer_id'] for b in bindings}) != len(bindings):
        raise ValueError('Duplicate helpers across this initial partition require separate review')
    payload = {'action': 'book_split_children', 'owner': owner, 'parent_id': parent.id,
        'partition_id': slots[0].coverage_review_id, 'children': bindings,
        'expires_at': min(now+timedelta(hours=2), *(instant(b['expires_at']) for b in bindings)).isoformat()}
    payload['content_hash'] = digest(payload)
    prior = session.scalar(select(m.Approval).where(m.Approval.kind == BOOKING, m.Approval.status == 'pending',
        m.Approval.payload['content_hash'].as_string() == payload['content_hash']))
    if prior:
        return prior
    review = m.Approval(kind=BOOKING, status='pending', requested_at=now, payload=payload)
    session.add(review); session.flush()
    return review


def start_outreach(session, parent_id, owner, expected, now):
    offers.begin_decision(session)
    parent = session.get(m.Shift,parent_id)
    if not parent:
        raise ValueError('The parent slot no longer exists')
    session.scalar(select(m.Event).where(m.Event.id==parent.event_id).with_for_update())
    slots = children(session,parent_id)
    review = session.get(m.Approval,slots[0].coverage_review_id) if slots else None
    if (not review or review.status != 'approved' or review.kind != PARTITION
            or review.payload.get('owner') != owner or review.payload.get('content_hash') != expected
            or expected != digest(review.payload) or parent.starts_at <= now):
        raise ValueError('Choose your exact approved upcoming partition')
    for child in slots:
        if error := child_problem(session,child):
            raise ValueError(error)
    for child in slots:
        for fill in session.scalars(select(m.FillRequest).where(m.FillRequest.shift_id==child.id,
                m.FillRequest.state=='open').with_for_update()):
            fill.state,fill.next_action_at='in_progress',now
    key=f'split_outreach:{review.id}'
    if session.get(m.Notification,key) is None:
        session.add(m.Notification(key=key,purpose='split_coverage',body='',state='sent',created_at=now,due_at=now,
            detail={'owner':owner,'partition_id':review.id,'content_hash':expected}))
    session.flush()
    return {'parent_id':parent_id,'queued_for_existing_worker':True,'sent':0}


def expire_acceptances(session, now):
    """Expired child YES sources cannot book; resume only the same opted-in slot."""
    rows=session.scalars(select(m.Notification).where(m.Notification.purpose=='split_acceptance',
        m.Notification.state=='held',m.Notification.expires_at<=now)
        .order_by(m.Notification.expires_at,m.Notification.key).limit(200)).all()
    for receipt in rows:
        child=session.get(m.Shift,receipt.detail['child_id'])
        outreach=session.get(m.Outreach,receipt.detail['outreach_id'])
        fill=session.get(m.FillRequest,outreach.fill_request_id) if outreach else None
        if not child or not pending_child(session,child):
            receipt.state='consumed' if child else 'expired'
            continue
        receipt.state='expired'
        if outreach and outreach.response=='yes':
            outreach.response='expired'
        if fill and fill.state=='waiting_split_review':
            fill.state,fill.next_action_at=('in_progress',now) if not child_problem(session,child) else ('escalated',None)
        for review in session.scalars(select(m.Approval).where(m.Approval.kind==BOOKING,m.Approval.status=='pending')):
            if any(b['incoming_id']==receipt.detail['incoming_id'] for b in review.payload.get('children',[])):
                review.status='expired'
    session.flush()


def coverage(session, parent):
    slots = children(session, parent.id)
    if not slots:
        return None
    result = {'parent_id': parent.id, 'children': [], 'fully_covered': False, 'gaps': [], 'held': None,
        'initial_pending': all(pending_child(session, slot) for slot in slots)}
    cursor = parent.starts_at
    for child in slots:
        if error := child_problem(session, child):
            result['held'] = error
        rows = session.scalars(select(m.Assignment).where(m.Assignment.shift_id == child.id,
            m.Assignment.status.in_(('approved', 'confirmed')))).all()
        assigned = len(rows) == 1
        if assigned:
            from app.core import eligibility
            from app.core.policies import PolicyStore
            person = session.get(m.Volunteer, rows[0].volunteer_id)
            from app.core.send_gate import has_open_sensitive_escalation
            check = eligibility.check(session, person, child, str(PolicyStore(session).church_tz()),
                _exclude_assignment_id=rows[0].id) if person else None
            opted_out = session.get(m.Policy,'sms_opt_out:'+person.phone) if person else None
            assigned = bool(person and person.sms_opt_in and check and not (opted_out and opted_out.value.get('value'))
                and not has_open_sensitive_escalation(session,person.id))
        if child.starts_at != cursor or child.ends_at > parent.ends_at:
            result['held'] = 'Child intervals do not form exact nonoverlapping parent coverage'
        result['children'].append({'shift_id': child.id, 'start': child.starts_at.isoformat(),
            'end': child.ends_at.isoformat(), 'assigned': assigned,
            'volunteer_id': rows[0].volunteer_id if assigned else None})
        if not assigned:
            result['gaps'].append({'shift_id': child.id, 'start': child.starts_at.isoformat(), 'end': child.ends_at.isoformat()})
        cursor = child.ends_at
    if cursor != parent.ends_at:
        result['held'] = 'Child intervals do not cover the parent end'
    result['fully_covered'] = not result['held'] and not result['gaps']
    return result


def decide(session, review_id, owner, expected, approve, now):
    offers.begin_decision(session)
    review = session.get(m.Approval, review_id)
    if not review or not review_valid(review, owner, expected, now):
        raise ValueError('The exact split review is stale, changed, expired or belongs to another owner')
    parent_id = review.payload['source']['parent']['parent_id'] if review.kind == PARTITION else review.payload['parent_id']
    parent = session.get(m.Shift, parent_id)
    if parent is None:
        raise ValueError('The original parent slot no longer exists')
    # Match normal offer lock order: event, slot, role, people, then review.
    session.scalar(select(m.Event).where(m.Event.id == parent.event_id).with_for_update()
        .execution_options(populate_existing=True))
    parent = session.scalar(select(m.Shift).where(m.Shift.id == parent_id).with_for_update()
        .execution_options(populate_existing=True))
    session.scalar(select(m.Role).where(m.Role.id == parent.role_id).with_for_update(read=True)
        .execution_options(populate_existing=True))
    session.scalar(select(m.Policy).where(m.Policy.key == f'split_role:{parent.role_id}').with_for_update()
        .execution_options(populate_existing=True))
    if review.kind == BOOKING:
        for child_id in sorted(b['child_id'] for b in review.payload['children']):
            session.scalar(select(m.Shift).where(m.Shift.id == child_id).with_for_update()
                .execution_options(populate_existing=True))
        for person_id in sorted({b['volunteer_id'] for b in review.payload['children']}):
            person = session.scalar(select(m.Volunteer).where(m.Volunteer.id == person_id).with_for_update()
                .execution_options(populate_existing=True))
            if person:
                session.expire(person,['qualifications'])
    review = session.scalar(select(m.Approval).where(m.Approval.id == review_id).with_for_update()
        .execution_options(populate_existing=True))
    if not review_valid(review,owner,expected,now):
        raise ValueError('The exact split review changed while reserving its source')
    if review.status == 'approved' and approve:
        return review
    if review.status != 'pending':
        raise ValueError('This split review was already decided')
    # Savepoint includes review status, child slots, bookings and audit. Failures
    # never leave half an applied partition or one helper silently booked.
    with session.begin_nested():
        if approve:
            payload = review.payload
            parent_id = payload['source']['parent']['parent_id'] if review.kind == PARTITION else payload['parent_id']
            parent = session.scalar(select(m.Shift).where(m.Shift.id == parent_id).with_for_update()
                .execution_options(populate_existing=True))
            if review.kind == PARTITION:
                source = payload['source']
                if partial_facts(session, parent_id, source['outreach_id'], source['incoming_id'], now) != source:
                    raise ValueError('Partition source changed; request a fresh exact review')
                if payload['intervals'] != partition_intervals(source,payload['normalized']):
                    raise ValueError('Reviewed child intervals overlap, duplicate or leave a parent gap')
            else:
                slots = children(session, parent_id)
                if (len(slots) != len(payload['children']) or len({b['volunteer_id'] for b in payload['children']}) != len(slots)
                        or {s.id for s in slots} != {b['child_id'] for b in payload['children']}):
                    raise ValueError('Reviewed child scope changed')
                for binding in payload['children']:
                    child = session.get(m.Shift, binding['child_id'])
                    if not pending_child(session, child) or session.scalar(select(m.Assignment.id).where(
                            m.Assignment.shift_id == child.id, m.Assignment.status.in_(ACTIVE))):
                        raise ValueError('A child is already occupied or applied')
                    if error := acceptance_problem(session, child, binding, now):
                        raise ValueError(error)
            old = session.info.get('record_authorized')
            session.info['record_authorized'] = session.info['split_apply_authorized'] = True
            try:
                review.status, review.decided_at, review.decided_by, review.via = 'approved', now, owner, 'web'
                if review.kind == PARTITION:
                    created = []
                    for index, interval in enumerate(payload['intervals']):
                        child = m.Shift(event_id=parent.event_id, role_id=parent.role_id, slot_index=-(index+1),
                            parent_shift_id=parent.id, interval_starts_at=instant(interval['start']),
                            interval_ends_at=instant(interval['end']), coverage_review_id=review.id)
                        session.add(child); session.flush(); created.append(child.id)
                        session.add(m.FillRequest(shift_id=child.id, urgency='normal', state='open', created_at=now))
                    review.payload = {**payload, 'applied_child_ids': created}
                    for fill in session.scalars(select(m.FillRequest).where(m.FillRequest.shift_id == parent.id,
                            m.FillRequest.state.in_(offers.OPEN_FILLS))):
                        fill.state, fill.closed_at, fill.next_action_at = 'split', now, None
                else:
                    ids = []
                    for binding in payload['children']:
                        assignment = m.Assignment(shift_id=binding['child_id'], volunteer_id=binding['volunteer_id'],
                            status='confirmed', source='fill', created_at=now, updated_at=now)
                        session.add(assignment); session.flush(); ids.append(assignment.id)
                        fill = session.get(m.FillRequest, session.get(m.Outreach, binding['outreach_id']).fill_request_id)
                        fill.state, fill.closed_at, fill.next_action_at = 'filled', now, None
                        session.get(m.Notification, f"split_accept:{binding['outreach_id']}").state = 'consumed'
                    review.payload = {**payload, 'applied_assignment_ids': ids}
                    session.add(m.Notification(key=f"split_applied:{payload['partition_id']}", purpose='split_coverage',
                        body='', state='sent', due_at=now, created_at=now, detail={'booking_review_id': review.id}))
            finally:
                session.info['record_authorized'] = old
                session.info.pop('split_apply_authorized', None)
        else:
            review.status, review.decided_at, review.decided_by, review.via = 'rejected', now, owner, 'web'
        session.add(m.Notification(key=f'split_review:{review.id}', purpose='human_review', body='', state='sent',
            created_at=now, due_at=now, detail={'review_id': review.id, 'owner': owner,
                'content_hash': expected, 'decision': review.status}))
        session.flush()
    return review


@event.listens_for(Session, 'before_flush')
def guard_partition_records(session, flush_context, instances):
    if session.info.get('split_apply_authorized'):
        return
    with session.no_autoflush:
        for obj in session.new | session.dirty | session.deleted:
            if isinstance(obj, m.Shift) and (obj.parent_shift_id is not None or
                    any(value is not None for value in inspect(obj).attrs.parent_shift_id.history.deleted)):
                fields = ('event_id', 'role_id', 'parent_shift_id', 'interval_starts_at', 'interval_ends_at', 'coverage_review_id')
                if obj in session.new or obj in session.deleted or any(inspect(obj).attrs[k].history.has_changes() for k in fields):
                    raise ValueError('Child intervals can only be created by exact partition review and cannot be changed independently')
            if isinstance(obj, m.Assignment) and obj.status in ACTIVE:
                shift = session.get(m.Shift, obj.shift_id)
                if shift and children(session, shift.id):
                    raise ValueError('A partitioned parent cannot receive a full-slot assignment')
                if shift and pending_child(session, shift):
                    raise ValueError('Initial child bookings require one atomic exact split review')
