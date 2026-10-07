"""Code-owned reasons to contact volunteers, independent of model wording."""
import hashlib
import json
from datetime import timedelta
from sqlalchemy import select, inspect
from app.db import models as m

ADMIN_PURPOSES = {'coordinator_notify', 'escalation_notify', 'admin_reply'}
CONTROL_PURPOSES = {'stop_confirm', 'start_confirm'}
INTAKE_FIELDS = {'name', 'interests', 'availability', 'frequency'}


def _key(value):
    return 'conversation:' + hashlib.sha256(json.dumps(value, sort_keys=True, default=str).encode()).hexdigest()


def _processing_binding(session, volunteer, phone, supplied, now):
    from app.integrations.mac_models import MacInboundReceipt
    from app.integrations.mac_progress import profile_hash, job_key
    from app.core.conversation import inbound_scope
    selected = session.info.get('mac_test_session')
    if (not selected or not selected.outbound_prefix.startswith('MAC') or not selected.active(now)
            or not volunteer or volunteer.phone != phone or not volunteer.sms_opt_in or volunteer.status != 'active'
            or set(supplied) != {'processing_job_key','incoming_message_id','session_id'}
            or supplied['session_id'] != selected.id):
        return None
    job = session.get(m.Notification, supplied['processing_job_key'])
    incoming = session.scalar(select(m.Message).where(m.Message.id==supplied['incoming_message_id'],
        inbound_scope(selected),m.Message.phone==phone,m.Message.volunteer_id==volunteer.id,
        m.Message.direction=='in',m.Message.status=='received',m.Message.created_at>=selected.starts_at,
        m.Message.created_at<=now,selected.window(m.Message.created_at)))
    if (not job or not incoming or job.purpose!='mac_progress' or job.message_id!=incoming.id
            or job.volunteer_id!=volunteer.id or job.state not in {'ack_pending','waiting_ack','ready','extracting'}):
        return None
    detail=job.detail or {}
    mode=detail.get('workflow','availability')
    stage=(volunteer.preferences or {}).get('onboarding_stage')
    if mode=='schedule':
        from app.integrations.mac_progress import schedule_snapshot
        if stage!='complete' or schedule_snapshot(session,volunteer,now)!=detail.get('bookings'):
            return None
    elif mode!='availability' or stage!='availability':
        return None

    receipt=session.get(MacInboundReceipt,detail.get('guid'))
    if (not receipt or job.key!=job_key(detail['guid']) or detail.get('input_id')!=incoming.id
            or detail.get('phone')!=phone or detail.get('session_id')!=selected.id
            or detail.get('profile_hash')!=profile_hash(volunteer)
            or receipt.fingerprint!=detail.get('fingerprint')
            or receipt.result.get('progress_key')!=job.key
            or receipt.result.get('session_id')!=selected.id):
        return None
    fingerprints={hashlib.sha256((phone+'\0'+service+'\0'+selected.id+'\0'+incoming.body).encode()).hexdigest()
        for service in ('SMS','iMessage')}
    newer=session.scalar(select(m.Message.id).where(inbound_scope(selected),m.Message.phone==phone,
        m.Message.direction=='in',m.Message.id>incoming.id).limit(1))
    if receipt.fingerprint not in fingerprints or newer:
        return None
    return {'processing_job_key':job.key,'incoming_message_id':incoming.id,'session_id':selected.id,
        'fingerprint':receipt.fingerprint,'profile_hash':detail['profile_hash']}


def metadata(session, *, purpose, volunteer, phone, now, supplied=None, reply_id=None):
    """Called by application code only; never accept a model's send authority."""
    if purpose in CONTROL_PURPOSES:
        return {'control_key': supplied.get('control_key') if isinstance(supplied, dict) else None}, None
    if purpose == 'coordinator_notify' and isinstance(supplied, dict) and 'admin_check' in supplied:
        from app.core.admin_check_copy import binding
        source = binding(session, volunteer, session.info.get('mac_test_session'), supplied['admin_check'], now)
        return ({'admin_check': source}, None) if source else ({}, 'Admin check requires its current recipient session')
    if purpose in ADMIN_PURPOSES | {'manual'}:
        return {}, None
    if purpose == 'outreach':
        from app.core import algorithm_outreach as algorithm, offer_windows as offers
        from app.core.policies import PolicyStore
        if PolicyStore(session).get('algorithm_outreach_enabled') is not True:
            return {}, 'Algorithm outreach remains disabled pending transport authorization'
        outreach_id = supplied.get('outreach_id') if isinstance(supplied, dict) else None
        outreach = session.get(m.Outreach, outreach_id) if type(outreach_id) is int else None
        if not outreach or not volunteer or outreach.volunteer_id != volunteer.id or volunteer.phone != phone:
            return {}, 'Offer selection does not belong to this recipient'
        if issue := algorithm.ask_problem(session, volunteer.id, now, exclude_outreach_id=outreach.id):
            return {}, issue
        fill = session.get(m.FillRequest, outreach.fill_request_id)
        shift = session.get(m.Shift, fill.shift_id)
        if (not algorithm.valid_member(session, outreach) or fill.state not in offers.OPEN_FILLS
                or outreach.response not in offers.OPEN_RESPONSES or shift.event.status != 'scheduled'
                or now >= offers.cutoff(session, shift.starts_at)):
            return {}, 'Algorithm-selected offer is no longer current'
        return {'outreach_id': outreach.id, 'snapshot': offers.snapshot(shift),
                'recipient_name': volunteer.name, 'recipient_phone': volunteer.phone,
                'keys': [_key([phone, 'algorithm_offer', outreach.id])]}, None
    if purpose == 'signup_reply':
        if isinstance(supplied, dict) and supplied.get('cancellation_reply') is not None:
            from app.core.cancellation_reply import binding
            proof = binding(session, volunteer, supplied['cancellation_reply'], now)
            if not proof or volunteer.phone != phone:
                return {}, 'Cancellation reply requires its original sender and saved result'
            return {'cancellation_reply': supplied['cancellation_reply'], 'binding': proof,
                    'keys': [_key([phone, proof['session_scope'], 'cancellation_reply', proof['reply_id']])]}, None
        if isinstance(supplied, dict) and supplied.get('ordinary_reply') is not None:
            from app.core.ordinary_reply import binding
            proof = binding(session, volunteer, supplied['ordinary_reply'], now)
            if not proof or volunteer.phone != phone:
                return {}, 'Ordinary reply requires its original safe sender input and current session'
            return {'ordinary_reply': supplied['ordinary_reply'], 'binding': proof,
                    'keys': [_key([phone, proof['session_scope'], 'ordinary_reply', proof['reply_id']])]}, None
        if isinstance(supplied, dict) and supplied.get('availability_followup') is not None:
            from app.core.serving_requests import binding
            proof = binding(session, volunteer, supplied['availability_followup'], now)
            if not proof or volunteer.phone != phone:
                return {}, 'Availability acknowledgment requires its original sender input and saved facts'
            return {'availability_followup': supplied['availability_followup'], 'binding': proof,
                    'keys': [_key([phone, proof['session_scope'], 'availability_followup', proof['message_id']])]}, None
        if isinstance(supplied, dict) and supplied.get('welcome_introduction') is not None:
            from app.core.volunteer_introduction import binding
            key = supplied['welcome_introduction']
            proof = binding(session, volunteer, key, now)
            if not proof or volunteer.phone != phone:
                return {}, 'Welcome requires its original explicit recipient and session'
            meta = {'welcome_introduction': key, 'binding': proof,
                    'keys': [_key([phone, proof['session_id'], key])]}
            if proof.get('welcome_retry'):
                meta['welcome_retry'] = proof['welcome_retry']
            return meta, None
        if isinstance(supplied,dict) and supplied.get('processing_job_key') is not None:
            binding=_processing_binding(session,volunteer,phone,supplied,now)
            if not binding:
                return {}, 'Processing acknowledgment needs its unchanged current Mac input and job'
            return {'processing':supplied,'binding':binding,
                'keys':[_key([phone,binding['session_id'],'processing_ack',binding['incoming_message_id']])]}, None
        if isinstance(supplied, dict) and supplied.get('signup_followup') is not None:
            from app.core.conversational_signup import followup_binding
            proof = supplied['signup_followup']
            binding = followup_binding(session,volunteer,proof,now)
            if not binding:
                return {}, 'Conversational followup needs its current sender and validated draft'
            source_key = [phone,binding['session_id'],'signup_followup',binding['incoming_id']]
            if binding.get('recovery_key'):
                source_key.append(binding['recovery_key'])
            return {'signup_followup':proof,'binding':binding,
                'keys':[_key(source_key)]}, None
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
        correction = supplied.get('name_correction')
        if correction is not None:
            # A previously reviewed matching-name correction cannot be silently
            # rewritten into a different self-reported-name intake message.
            return {}, 'Legacy name correction requires a new exact review'
        recovery = supplied.get('name_recovery')
        if recovery is not None:
            from types import SimpleNamespace
            from app.integrations.google_voice_demo import name_reply_recovery
            fresh = name_reply_recovery(session, SimpleNamespace(now=lambda: now), phone,
                recovery.get('reply_id') if isinstance(recovery, dict) else None)
            if fields != ['name'] or not fresh or fresh != recovery:
                return {}, 'Name clarification requires its original pending sender reply'
            progress = {**progress, 'name_recovery': recovery}
        missing_times = any(missing_window_hours(window) for window in draft.get('recurring_windows', []))
        known = {
            'name': bool(volunteer and not prefs.get('consent_pending')),
            'interests': 'interested_roles' in prefs,
            'availability': prefs.get('onboarding_stage') == 'complete' or (draft.get('availability_known') is True and not (progress and missing_times)),
            'frequency': prefs.get('availability_frequency_known') is True or draft.get('frequency_known') is True,
        }
        selected = session.info.get("mac_test_session")
        if selected and selected.outbound_prefix.startswith("GV"):
            from app.integrations.google_voice_demo import RECIPIENT_KEY
            registration = session.get(m.Policy, RECIPIENT_KEY + phone)
            if registration and registration.value.get("consent_state") == "awaiting_name":
                known["name"] = False  # Imported names do not establish demo consent.
        if any(known[field] for field in fields):
            return {}, 'Signup prompt repeats a fact already supplied'
        selected = session.info.get('mac_test_session')
        scope = selected.id if selected else 'signup'
        meta = {'intake_fields': sorted(fields),
                'keys': [_key([phone, scope, 'intake', field] + ([progress] if progress else [])) for field in sorted(fields)]}
        retry=supplied.get('welcome_retry')
        if retry is not None:
            from app.core.volunteer_welcome import retry_binding
            if fields!=['interests'] or not volunteer or retry_binding(session,volunteer,retry)!=retry:
                return {}, 'Welcome retry requires its original terminal no-send proof'
            meta['welcome_retry']=retry
            meta['keys']=[_key([phone,scope,'welcome_retry',retry])]
        if progress:
            meta.update(intake_progress=True, progress=progress)
        if recovery is not None:
            meta['name_recovery'] = recovery
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
        from app.core.policies import PolicyStore
        result = {'assignment_id': assignment.id, 'notice': notice,
                'source': assignment_source(assignment, purpose),
                'recipient_name': volunteer.name, 'recipient_phone': volunteer.phone,
                'timezone': str(PolicyStore(session).church_tz()),
                'keys': [_key([phone, assignment.id, notice])]}
        if 'automatic_reminder' in supplied:
            if purpose != 'reminder':
                return {}, 'Automatic reminder authority cannot approve other texts'
            result['automatic_reminder'] = supplied['automatic_reminder']
        return result, None
    if purpose == 'booking_status':
        session.flush()
        session.expire_all()  # Requeries must not reuse pre-composition ORM facts.
        from app.core.conversation import scope
        inbound = session.scalar(scope(select(m.Message), session.info.get('mac_test_session')).where(m.Message.id == reply_id)) if reply_id else None
        from app.core.booking_status import requested, snapshot, session_binding, opportunities_requested
        if (not volunteer or volunteer.phone != phone or not volunteer.sms_opt_in or volunteer.status != 'active'
                or not inbound or inbound.volunteer_id != volunteer.id or inbound.direction != 'in' or inbound.phone != phone
                or not timedelta(0) <= now-inbound.created_at <= (timedelta(days=2) if opportunities_requested(inbound.body) else timedelta(minutes=10))
                or not requested(session, volunteer, inbound.body, now)):
            return {}, 'Booking status requires this sender\'s current explicit question'
        from app.core.privacy import safe_message_history
        from app.llm.parser import keyword_sensitive
        if keyword_sensitive(inbound.body) or not safe_message_history(session, [inbound]):
            return {}, 'Booking question requires internal care review'
        from app.core.policies import PolicyStore
        from app.llm.gloo_client import GlooUnavailableError
        try:
            schedule = snapshot(session, volunteer, now, include_opportunities=opportunities_requested(inbound.body))
        except GlooUnavailableError:
            return {}, 'Saved booking facts require review'
        return {'reply_id': inbound.id, 'question': inbound.body,
                'timezone': str(PolicyStore(session).church_tz()),
                'session_scope': session_binding(session.info.get('mac_test_session')),
                'schedule': schedule,
                'keys': [_key([phone, 'booking_status', inbound.id])]}, None
    return {}, 'Routine volunteer acknowledgments, progress and offer prompts are suppressed'


def problem(session, *, purpose, volunteer, phone, body, now, meta, approval=None, message=None):
    if purpose in CONTROL_PURPOSES:
        from app.core.consent_controls import acknowledgement_problem
        return acknowledgement_problem(session, purpose=purpose, volunteer=volunteer, phone=phone,
            body=body, key=(meta or {}).get('control_key'), message=message)
    if purpose in ADMIN_PURPOSES:
        if purpose == 'coordinator_notify' and (meta or {}).get('admin_check') is not None:
            from app.core.admin_check_copy import problem as check_problem
            return check_problem(session, volunteer, session.info.get('mac_test_session'), meta['admin_check'], body, now)
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
        from app.core.cloud_composition import reviewed_composition
        selected = session.info.get('mac_test_session')
        if not reviewed_composition(session, approval, selected):
            return 'Manual text requires its exact Gloo composition proof'
        return None
    if not meta or not meta.get('keys'):
        return 'Automatic volunteer text has no essential conversation source'
    if purpose == 'outreach':
        fresh, error = metadata(session, purpose=purpose, volunteer=volunteer, phone=phone, now=now,
                                supplied={'outreach_id': meta.get('outreach_id')})
        if error or fresh != meta:
            return error or 'Algorithm offer scope changed before delivery'
    elif purpose == 'signup_reply':
        supplied = ({'welcome_introduction': meta['welcome_introduction']} if meta.get('welcome_introduction') else
                    {'cancellation_reply': meta['cancellation_reply']} if meta.get('cancellation_reply') else
                    {'ordinary_reply': meta['ordinary_reply']} if meta.get('ordinary_reply') else
                    {'availability_followup': meta['availability_followup']} if meta.get('availability_followup') else
                    meta['processing'] if meta.get('processing') else
                    {'signup_followup':meta['signup_followup']} if meta.get('signup_followup') else
                    {'intake_fields':meta.get('intake_fields'),'intake_progress':meta.get('intake_progress'),
                     'name_correction':meta.get('name_correction'),'name_recovery':meta.get('name_recovery'),
                     'welcome_retry':meta.get('welcome_retry')})
        fresh, error = metadata(session, purpose=purpose, volunteer=volunteer, phone=phone, now=now,
                                supplied=supplied)
        if error or fresh != meta:
            return error or 'Signup intake scope changed'
        if meta.get('welcome_introduction'):
            if hashlib.sha256(body.encode()).hexdigest() != meta['binding']['body_hash']:
                return 'Welcome differs from its approved exact copy'
        if meta.get('availability_followup'):
            from app.core.serving_requests import copy_for
            if body != copy_for(meta['binding']):
                return 'Availability acknowledgment differs from its saved facts'
        if meta.get('cancellation_reply'):
            from app.core.cancellation_reply import copy_for
            from app.core.policies import PolicyStore
            if body != copy_for(meta['binding'], PolicyStore(session).church_tz()):
                return 'Cancellation reply differs from its saved result'
        if meta.get('ordinary_reply'):
            from app.core.ordinary_reply import copy_for
            if body != copy_for(meta['binding']):
                return 'Ordinary reply differs from its saved input or review state'
        if meta.get('processing'):
            job=session.get(m.Notification,meta['processing']['processing_job_key'])
            ack_id=job.detail.get('ack_message_id')
            if ack_id is not None and (message is None or ack_id!=message.id):
                return 'Processing acknowledgment already belongs to its original outgoing message'
    elif purpose in {'confirmation', 'reminder'}:
        from app.core.reminders import assignment_source
        from app.core import eligibility
        from app.core.policies import PolicyStore
        from app.core.schedule_messages import current_assignment
        assignment = current_assignment(session, meta.get('assignment_id'))
        if volunteer is not None:
            identity = inspect(volunteer).identity
            volunteer = session.get(m.Volunteer, identity[0], populate_existing=True) if identity else None
        if not assignment or not volunteer:
            return 'Schedule assignment or recipient is missing'
        session.expire(volunteer, ['qualifications'])
        if (assignment.volunteer_id != volunteer.id or assignment.status not in ('approved', 'confirmed')
                or assignment.shift.event.status != 'scheduled' or assignment.shift.starts_at <= now
                or assignment_source(assignment, purpose) != meta.get('source')):
            return 'Schedule assignment is no longer the recorded placement'
        tz = PolicyStore(session).church_tz()
        if meta.get('recipient_phone') != volunteer.phone or meta.get('recipient_name') != volunteer.name or meta.get('timezone') != str(tz):
            return 'Schedule recipient name or local timezone changed'
        if purpose == 'reminder' and assignment.shift.starts_at.astimezone(tz).date() != now.astimezone(tz).date()+timedelta(days=1):
            return 'Day-before reminder is not due'
        if not volunteer.sms_opt_in or not eligibility.check(session, volunteer, assignment.shift, str(tz), _exclude_assignment_id=assignment.id):
            return 'Schedule recipient is no longer eligible or consenting'
        if meta.get('automatic_reminder') is not None:
            from app.core.reminders import automatic_problem
            if error := automatic_problem(session, volunteer, body, now, meta, message):
                return error
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
            from app.integrations.google_voice_presend_review import original_reservation_allowed
            if original_reservation_allowed(session,approval,receipt):
                continue
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
    if row.purpose == 'reminder':
        job = session.scalar(select(m.Policy).where(m.Policy.key.startswith('job:reminder:'),
            m.Policy.value['message_id'].as_integer() == row.id,
            m.Policy.value['automatic_reminder'].as_boolean().is_(True)))
        if job and (not isinstance(meta.get('automatic_reminder'), dict)
                    or meta['automatic_reminder'].get('job_key') != job.key):
            return 'Automatic reminder lost its original composed job proof'
    if approval and receipt and meta != approval.payload.get('conversation', {}):
        return 'Conversation source differs from exact human review'
    volunteer = session.get(m.Volunteer, row.volunteer_id) if row.volunteer_id else session.scalar(
        select(m.Volunteer).where(m.Volunteer.phone == row.phone))
    return problem(session, purpose=row.purpose, volunteer=volunteer, phone=row.phone, body=row.body,
                   now=now, meta=meta, approval=approval, message=row)
