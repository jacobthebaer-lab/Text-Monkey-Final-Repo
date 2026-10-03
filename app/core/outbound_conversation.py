"""Code-owned reasons to contact volunteers, independent of model wording."""
import hashlib
import json
from datetime import timedelta
from sqlalchemy import select
from app.db import models as m

ADMIN_PURPOSES = {'coordinator_notify', 'escalation_notify', 'admin_reply'}
CONTROL_PURPOSES = {'stop_confirm', 'start_confirm'}
INTAKE_FIELDS = {'name', 'interests', 'availability', 'frequency'}


def _key(value):
    return 'conversation:' + hashlib.sha256(json.dumps(value, sort_keys=True, default=str).encode()).hexdigest()


def metadata(session, *, purpose, volunteer, phone, now, supplied=None, reply_id=None):
    """Called by application code only; never accept a model's send authority."""
    if purpose in CONTROL_PURPOSES:
        return {'control_key': supplied.get('control_key') if isinstance(supplied, dict) else None}, None
    if purpose in ADMIN_PURPOSES | {'manual'}:
        return {}, None
    if purpose == 'signup_reply':
        fields = supplied.get('intake_fields') if isinstance(supplied, dict) else None
        if (not isinstance(fields, list) or not fields or any(not isinstance(field, str) or field not in INTAKE_FIELDS for field in fields)
                or len(fields) != len(set(fields))):
            return {}, 'Only essential missing signup facts may prompt a volunteer'
        prefs = volunteer.preferences or {} if volunteer else {}
        draft = prefs.get('onboarding_availability_draft') or {}
        from app.core.onboarding import missing_window_hours
        progress = {}
        if supplied.get('intake_progress') is True:
            if fields == ['name']:
                from types import SimpleNamespace
                from app.core.signup import identity_parts
                parts = identity_parts(session, SimpleNamespace(now=lambda: now), phone)
                progress = {'name_parts': sorted(parts)} if parts else {}
            elif set(fields) <= {'availability', 'frequency'} and draft:
                # These are code-validated saved facts, never a model's send authority.
                if draft.get('availability_known') is True or draft.get('frequency_known') is True:
                    progress = {'availability_known': draft.get('availability_known') is True,
                        'frequency_known': draft.get('frequency_known') is True and
                            not (prefs.get('signup_minimal_texts') is True and draft.get('availability_known') is True),
                        'windows': [{key: window.get(key) for key in ('weekday','role_ids','event_context')} |
                                    {'hours_known': not missing_window_hours(window)}
                                    for window in draft.get('recurring_windows', [])]}
        missing_times = any(missing_window_hours(window) for window in draft.get('recurring_windows', []))
        known = {
            'name': bool(volunteer and not prefs.get('consent_pending')),
            'interests': 'interested_roles' in prefs,
            'availability': prefs.get('onboarding_stage') == 'complete' or (draft.get('availability_known') is True and not (progress and missing_times)),
            'frequency': prefs.get('availability_frequency_known') is True or draft.get('frequency_known') is True,
        }
        if any(known[field] for field in fields):
            return {}, 'Signup prompt repeats a fact already supplied'
        selected = session.info.get('mac_test_session')
        scope = selected.id if selected else 'signup'
        meta = {'intake_fields': sorted(fields),
                'keys': [_key([phone, scope, 'intake', field] + ([progress] if progress else [])) for field in sorted(fields)]}
        if progress:
            meta.update(intake_progress=True, progress=progress)
        return meta, None
    if purpose in {'confirmation', 'reminder'}:
        if not isinstance(supplied, dict) or type(supplied.get('assignment_id')) is not int:
            return {}, 'Schedule notification requires a recorded assignment'
        notice = 'scheduled' if purpose == 'confirmation' else 'day_before'
        if supplied.get('notice') != notice:
            return {}, 'Schedule notification purpose and source do not match'
        assignment = session.get(m.Assignment, supplied['assignment_id'])
        if assignment is None or volunteer is None or assignment.volunteer_id != volunteer.id:
            return {}, 'Schedule notification assignment does not belong to this recipient'
        from app.core.reminders import assignment_source
        return {'assignment_id': assignment.id, 'notice': notice,
                'source': assignment_source(assignment, purpose),
                'keys': [_key([phone, assignment.id, notice])]}, None
    if purpose == 'booking_status':
        inbound = session.get(m.Message, reply_id) if reply_id else None
        from app.core.booking_status import requested
        if (not volunteer or not inbound or inbound.direction != 'in' or inbound.phone != phone
                or not timedelta(0) <= now-inbound.created_at <= timedelta(minutes=10)
                or not requested(session, volunteer, inbound.body, now)):
            return {}, 'Booking status requires this sender\'s current explicit question'
        assignments = session.scalars(select(m.Assignment).join(m.Shift).join(m.Event).where(
            m.Assignment.volunteer_id == volunteer.id, m.Event.status == 'scheduled', m.Event.ends_at > now,
            m.Assignment.status.in_(('proposed', 'approved', 'confirmed'))).order_by(m.Assignment.id)).all()
        return {'reply_id': inbound.id,
                'schedule': [[a.id, a.status, a.shift_id, a.shift.event.starts_at.isoformat(), a.shift.role.name] for a in assignments],
                'keys': [_key([phone, 'booking_status', inbound.id])]}, None
    return {}, 'Routine volunteer acknowledgments, progress and offer prompts are suppressed'


def problem(session, *, purpose, volunteer, phone, body, now, meta, approval=None, message=None):
    if purpose in CONTROL_PURPOSES:
        from app.core.consent_controls import acknowledgement_problem
        return acknowledgement_problem(session, purpose=purpose, volunteer=volunteer, phone=phone,
            body=body, key=(meta or {}).get('control_key'), message=message)
    if purpose in ADMIN_PURPOSES:
        if volunteer and (volunteer.is_coordinator or volunteer.is_pastor or
                          (volunteer.preferences or {}).get('admin_text_owner')):
            return None
        return 'Administrative status is internal to configured administrators'
    if purpose == 'manual':
        if approval is None and message is None:
            return None  # gate must stage exact human review before sending
        from app.core import confirmations
        if not approval or not confirmations.valid(approval, now) or approval.status != 'approved':
            return 'Manual text requires valid exact human review'
        if approval.payload.get('purpose') != 'manual' or approval.payload.get('body') != body or approval.payload.get('phone') != phone:
            return 'Manual recipient or body differs from exact human review'
        return None
    if not meta or not meta.get('keys'):
        return 'Automatic volunteer text has no essential conversation source'
    if purpose == 'signup_reply':
        fresh, error = metadata(session, purpose=purpose, volunteer=volunteer, phone=phone, now=now,
                                supplied={'intake_fields': meta.get('intake_fields'), 'intake_progress': meta.get('intake_progress')})
        if error or fresh != meta:
            return error or 'Signup intake scope changed'
    elif purpose in {'confirmation', 'reminder'}:
        from app.core.reminders import assignment_source
        from app.core import eligibility
        from app.core.policies import PolicyStore
        assignment = session.get(m.Assignment, meta.get('assignment_id'))
        if not assignment or not volunteer:
            return 'Schedule assignment or recipient is missing'
        session.refresh(assignment)
        session.refresh(assignment.shift)
        session.refresh(assignment.shift.event)
        session.refresh(assignment.shift.role)
        session.refresh(volunteer)
        session.expire(volunteer, ['qualifications'])
        if (assignment.volunteer_id != volunteer.id or assignment.status not in ('approved', 'confirmed')
                or assignment.shift.event.status != 'scheduled' or assignment.shift.event.starts_at <= now
                or assignment_source(assignment, purpose) != meta.get('source')):
            return 'Schedule assignment is no longer the recorded placement'
        tz = PolicyStore(session).church_tz()
        if purpose == 'reminder' and assignment.shift.event.starts_at.astimezone(tz).date() != now.astimezone(tz).date()+timedelta(days=1):
            return 'Day-before reminder is not due'
        if not volunteer.sms_opt_in or not eligibility.check(session, volunteer, assignment.shift, str(tz), _exclude_assignment_id=assignment.id):
            return 'Schedule recipient is no longer eligible or consenting'
    elif purpose == 'booking_status':
        fresh, error = metadata(session, purpose=purpose, volunteer=volunteer, phone=phone, now=now,
                                reply_id=meta.get('reply_id'))
        if error or fresh != meta:
            return error or 'Requested schedule facts changed before delivery'
    else:
        return 'Routine volunteer conversation is suppressed'
    for key in meta['keys']:
        receipt = session.get(m.Notification, key)
        if receipt is not None and (message is None or receipt.message_id != message.id):
            return 'This signup question or assignment notification was already requested'
    return None


def record_suppression(session, phone, purpose, body, now, reason):
    key = _key(['suppressed', phone, purpose, body, now.date().isoformat(), reason])
    if session.get(m.Notification, key) is None:
        session.add(m.Notification(key=key, purpose='conversation_suppression', body='', state='blocked_policy',
            due_at=now, created_at=now, detail={'purpose': purpose, 'reason': reason}))
        session.flush()


def queued_problem(session, row, now, approval=None):
    receipt = session.get(m.Notification, f'conversation-message:{row.id}')
    meta = receipt.detail if receipt else (approval.payload.get('conversation', {}) if approval else {})
    if approval and receipt and meta != approval.payload.get('conversation', {}):
        return 'Conversation source differs from exact human review'
    volunteer = session.get(m.Volunteer, row.volunteer_id) if row.volunteer_id else session.scalar(
        select(m.Volunteer).where(m.Volunteer.phone == row.phone))
    return problem(session, purpose=row.purpose, volunteer=volunteer, phone=row.phone, body=row.body,
                   now=now, meta=meta, approval=approval, message=row)
