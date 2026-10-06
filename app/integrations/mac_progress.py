"""Durable, source-bound Mac availability work with network calls outside writes.

One backend process owns the demo. The native worker submits the acknowledgment
before this service interprets preferences. Actual Gloo responses are cached by
exact request, so rollback/replay performs no substitute model interpretation.
"""
import hashlib
import json
import threading
from types import SimpleNamespace

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError

from app.db import models as m
from app.integrations.mac_models import MacInboundReceipt
from app.llm.gloo_client import GlooClient, GlooUnavailableError

ACK_TEXT = "Thanks, I'm working through your preferences now."
ACK_TIMEOUT_SECONDS = 8.0
MAX_NETWORK_STEPS = 8
WORK_STATES = {'ack_pending', 'waiting_ack', 'ready', 'extracting'}
_initialization_lock = threading.Lock()


def profile_hash(volunteer):
    values = {key: getattr(volunteer, key) for key in ('id', 'name', 'phone', 'sms_opt_in', 'status')}
    values['preferences'] = volunteer.preferences or {}
    return hashlib.sha256(json.dumps(values, sort_keys=True, separators=(',', ':'), default=str).encode()).hexdigest()


def job_key(guid):
    return 'mac-progress:' + hashlib.sha256(guid.encode()).hexdigest()


def complex_availability(session, state, data):
    """Route scoped preference work promptly, without interpreting its meaning."""
    from app.core.consent_controls import control_action
    from app.llm.parser import keyword_sensitive
    from app.core.inbound import _schedule_instruction
    from app.core.signup_recovery import PRIVACY, privacy_hold
    from app.core.policies import PolicyStore
    if (not state.settings.allow_text_signup or not state.settings.gloo_signup_replies
            or not PolicyStore(session).get('full_text_onboarding')
            or control_action(data.body) or keyword_sensitive(data.body) or PRIVACY.search(data.body)
            or _schedule_instruction(data.body) or '?' in data.body):
        return False
    volunteer = session.scalar(select(m.Volunteer).where(m.Volunteer.phone == data.phone))
    selected = state.provider.test_sessions.get(data.phone)
    policy = session.get(m.Policy, 'conversational_signup:' + data.phone)
    conversational = bool(selected and selected.id == data.session_id
        and selected.outbound_prefix.startswith('MAC') and selected.active(state.mac_delivery_clock.now())
        and policy and policy.value.get('value') is True and policy.value.get('session_id') == selected.id)
    return bool(volunteer and volunteer.sms_opt_in and volunteer.status == 'active'
        and volunteer.preferences.get('onboarding_stage') == 'availability'
        and not privacy_hold(session, data.phone, volunteer)
        # Even a short answer can require history-aware extraction and composition.
        # The opt-in conversation gets the same durable Gloo acknowledgment first.
        and (conversational or len(data.body) >= 160 or data.body.count('\n') >= 2 or data.body.count(';') >= 2))


def _source(session, state, job, *, require_ack=False):
    from app.core.send_gate import has_open_sensitive_escalation, BLOCKING_ESCALATION_STATUSES
    selected = state.provider.test_sessions.get(job.detail['phone'])
    now = state.mac_delivery_clock.now()
    if not selected or selected.id != job.detail['session_id'] or not selected.active(now):
        return None, None, None, 'Selected Mac session expired or changed'
    if not state.provider.allows(job.detail['phone']):
        return None, None, None, 'Recipient is no longer enabled'
    session.info['mac_test_session'] = selected
    session.info['conversation_origin'] = 'mac_messages'
    receipt = session.get(MacInboundReceipt, job.detail['guid'])
    incoming = session.get(m.Message, job.detail['input_id'])
    volunteer = session.get(m.Volunteer, job.volunteer_id)
    if (job.key != job_key(job.detail['guid']) or not receipt
            or receipt.result.get('progress_key') != job.key or receipt.result.get('session_id') != selected.id
            or receipt.fingerprint != job.detail['fingerprint'] or not incoming
            or incoming.id != job.message_id or incoming.direction != 'in' or incoming.status != 'received'
            or incoming.phone != job.detail['phone'] or incoming.kind != 'mac_test_in'
            or incoming.purpose != 'test:' + selected.id
            or not selected.starts_at <= incoming.created_at <= now):
        return None, None, None, 'Original Mac input binding is invalid'
    expected_fingerprint = hashlib.sha256((incoming.phone + '\0' + job.detail['service'] + '\0' + selected.id + '\0' + incoming.body).encode()).hexdigest()
    if expected_fingerprint != receipt.fingerprint:
        return None, None, None, 'Original Mac input content changed'
    stopped = session.get(m.Policy, 'sms_opt_out:' + incoming.phone)
    care = session.scalars(select(m.Escalation.related_ids).where(m.Escalation.category == 'sensitive',
        m.Escalation.status.in_(BLOCKING_ESCALATION_STATUSES)))
    if (not volunteer or volunteer.phone != incoming.phone or not volunteer.sms_opt_in or volunteer.status != 'active'
            or (stopped and stopped.value.get('value')) or has_open_sensitive_escalation(session, volunteer.id)
            or any(item.get('phone') == incoming.phone for item in care)):
        return None, None, None, 'Recipient stopped texts or requires human follow-up'
    if profile_hash(volunteer) != job.detail['profile_hash']:
        return None, None, None, 'Profile changed while preferences were being processed'
    newer = session.scalar(select(m.Message.id).where(m.Message.direction == 'in',
        m.Message.phone == incoming.phone, m.Message.purpose == incoming.purpose, m.Message.id > incoming.id).limit(1))
    if newer:
        return None, None, None, 'A newer sender message superseded this work'
    if require_ack:
        ack = session.get(m.Message, job.detail.get('ack_message_id'))
        if not ack or ack.status != 'submitted':
            return None, None, None, 'Acknowledgment has no native submission receipt'
    return volunteer, incoming, selected, None


class _NetworkBoundary(BaseException):
    def __init__(self, key, arguments):
        self.key, self.arguments = key, arguments


def _json_response(response):
    def plain(value):
        if hasattr(value, 'model_dump'):
            return value.model_dump(mode='json')
        if isinstance(value, dict):
            return {key: plain(item) for key, item in value.items()}
        if isinstance(value, (list, tuple)):
            return [plain(item) for item in value]
        if isinstance(value, SimpleNamespace):
            return plain(vars(value))
        return value
    result = plain(response) if isinstance(response, SimpleNamespace) or hasattr(response, 'model_dump') else {}
    result['output_text'] = getattr(response, 'output_text', '') or ''
    result['usage'] = plain(getattr(response, 'usage', None))
    return result


def _attributes(value):
    if isinstance(value, dict):
        return SimpleNamespace(**{key: _attributes(item) for key, item in value.items()})
    if isinstance(value, list):
        return [_attributes(item) for item in value]
    return value


class _ReplayGloo:
    def __init__(self, actual, responses):
        self.settings = actual.settings
        self.responses = responses

    def create_response(self, **arguments):
        key = hashlib.sha256(json.dumps(arguments, sort_keys=True, separators=(',', ':'), default=str).encode()).hexdigest()
        if key not in self.responses:
            raise _NetworkBoundary(key, arguments)
        return _attributes(self.responses[key])


def _fast_gloo(gloo):
    if isinstance(gloo, GlooClient):
        return GlooClient(gloo.settings, client=gloo._client.with_options(timeout=ACK_TIMEOUT_SECONDS, max_retries=0), max_attempts=1)
    return gloo  # injectable offline fixtures; live clients always use GlooClient


def _mark(session, job, state, reason):
    job.state = state
    job.detail = {**job.detail, 'reason': reason}
    ack_id = job.detail.get('ack_message_id')
    ack = session.get(m.Message, ack_id) if ack_id else None
    if ack and ack.status == 'queued':
        ack.status = 'superseded'
    receipt = session.get(MacInboundReceipt, job.detail['guid'])
    if receipt:
        receipt.result = {**receipt.result, 'progress_state': job.state}


def _hold(state, key, reason):
    with state.session_factory() as session:
        job = session.get(m.Notification, key)
        if job and job.state in WORK_STATES:
            _mark(session, job, 'held', reason)
            session.commit()


def _operation(state, key, phase):
    with _initialization_lock:
        if not hasattr(state, 'mac_progress_operations'):
            state.mac_progress_operations = {}
        lock = state.mac_progress_operations.setdefault(key, threading.Lock())
    if not lock.acquire(blocking=False):
        return
    try:
        return _execute_operation(state, key, phase)
    finally:
        lock.release()


def _execute_operation(state, key, phase):
    """Replay local validation, parking each uncached network request after rollback."""
    actual = _fast_gloo(state.gloo) if phase == 'ack' else state.gloo
    if actual is None:
        _hold(state, key, 'Gloo is unavailable; no substitute was sent')
        return
    for _ in range(MAX_NETWORK_STEPS + 1):
        boundary = None
        try:
            with state.session_factory() as session:
                job = session.get(m.Notification, key)
                allowed = {'ack_pending'} if phase == 'ack' else {'ready', 'extracting'}
                if not job or job.state not in allowed:
                    return
                volunteer, incoming, selected, error = _source(session, state, job, require_ack=phase != 'ack')
                if error:
                    _mark(session, job, 'superseded', error)
                    session.commit()
                    return
                from app.core.send_gate import SendGate
                gate = SendGate(session, state.clock, state.provider, reply_to_message_id=incoming.id)
                replay = _ReplayGloo(actual, job.detail.get('responses', {}))
                gate.gloo = replay
                if phase == 'ack':
                    from app.core.signup_responder import compose_signup_reply
                    body = compose_signup_reply(session, state.clock, replay, ACK_TEXT, (),
                        volunteer=volunteer, phone=incoming.phone, require_gloo=True, exact_copy=True)
                    outcome = gate.send(body=body, phone=incoming.phone, volunteer=volunteer, purpose='signup_reply', kind='ai',
                        conversation={'processing_job_key': key, 'incoming_message_id': incoming.id, 'session_id': selected.id})
                    if not outcome.sent and not outcome.approval_id:
                        raise GlooUnavailableError(outcome.reason or 'Acknowledgment is held by texting rules')
                    job.state = 'waiting_ack'
                    job.detail = {**job.detail, 'ack_message_id': outcome.message_id, 'approval_id': outcome.approval_id}
                else:
                    from app.core import onboarding, profile_sync
                    session.info.update(sender_phone=incoming.phone, sender_record_permissions={}, sender_assignment_permissions=set(),
                        sender_profile_instruction=True, record_authorized=False, confirmation_now=state.clock.now())
                    mirror = state.settings.profile_sync_enabled and incoming.phone in profile_sync.approved_phones(state.settings)
                    before = profile_sync.safe_snapshot(session, incoming.phone) if mirror else None
                    route = onboarding.handle(session, state.clock, gate, volunteer, incoming.body, replay)
                    session.flush()
                    if mirror:
                        profile_sync.capture(session, state.settings, phone=incoming.phone, guid=job.detail['guid'],
                            route=route, before=before, effective_at=state.mac_delivery_clock.now())
                    job.state = 'done' if route not in {'onboarding_review', 'escalated_sensitive'} else 'held'
                    job.detail = {**job.detail, 'route': route}
                receipt = session.get(MacInboundReceipt, job.detail['guid'])
                receipt.result = {**receipt.result, 'progress_state': job.state, 'ack_message_id': job.detail.get('ack_message_id'),
                                  'intent': job.detail.get('route', 'availability_processing')}
                session.commit()
                return
        except _NetworkBoundary as pending:
            boundary = pending  # Session context has rolled back every speculative write.
        except (GlooUnavailableError, ValueError) as error:
            _hold(state, key, str(error))
            return
        if boundary is None:
            return
        # No live database session/transaction remains during Gloo.
        try:
            with state.session_factory() as session:
                job = session.get(m.Notification, key)
                if not job or len(job.detail.get('responses', {})) >= MAX_NETWORK_STEPS:
                    raise GlooUnavailableError('Preference work exceeded its bounded model-call budget')
                _, _, _, source_error = _source(session, state, job, require_ack=phase != 'ack')
                if source_error:
                    _mark(session, job, 'superseded', source_error)
                    session.commit()
                    return
                if phase != 'ack':
                    job.state = 'extracting'
                    receipt = session.get(MacInboundReceipt, job.detail['guid'])
                    receipt.result = {**receipt.result, 'progress_state': 'extracting'}
                    session.commit()
            response = actual.create_response(**boundary.arguments)
        except GlooUnavailableError as error:
            _hold(state, key, str(error))
            return
        with state.session_factory() as session:
            job = session.get(m.Notification, key)
            if not job or job.state not in WORK_STATES:
                return
            _, _, _, error = _source(session, state, job, require_ack=phase != 'ack')
            if error:
                _mark(session, job, 'superseded', error)
            else:
                job.detail = {**job.detail, 'responses': {**job.detail.get('responses', {}), boundary.key: _json_response(response)}}
            session.commit()
    _hold(state, key, 'Preference work exceeded its bounded model-call budget')


def accept(state, data, selected, fingerprint):
    """Persist the received input before requesting its quick Gloo acknowledgment."""
    key = job_key(data.guid)
    with state.session_factory() as session:
        prior = session.get(MacInboundReceipt, data.guid)
        if prior:
            if prior.fingerprint != fingerprint:
                from fastapi import HTTPException
                raise HTTPException(409, 'Message ID was reused with different content')
            return {**prior.result, 'duplicate': True}
        volunteer = session.scalar(select(m.Volunteer).where(m.Volunteer.phone == data.phone).with_for_update())
        if not complex_availability(session, state, data):
            return None
        session.info['mac_test_session'] = selected
        incoming = m.Message(direction='in', volunteer_id=volunteer.id, phone=data.phone, body=data.body,
            kind='mac_test_in', purpose='test:' + selected.id, status='received', created_at=state.clock.now())
        session.add(incoming)
        receipt = MacInboundReceipt(guid=data.guid, fingerprint=fingerprint, result={})
        session.add(receipt)
        try:
            session.flush()
        except IntegrityError:
            session.rollback()
            prior = session.get(MacInboundReceipt, data.guid)
            if not prior or prior.fingerprint != fingerprint:
                from fastapi import HTTPException
                raise HTTPException(409, 'Message ID conflict')
            return {**prior.result, 'duplicate': True}
        job = m.Notification(key=key, volunteer_id=volunteer.id, message_id=incoming.id, purpose='mac_progress',
            state='ack_pending', due_at=state.clock.now(), created_at=state.clock.now(), detail={
                'guid': data.guid, 'fingerprint': fingerprint, 'service': data.service, 'phone': data.phone, 'session_id': selected.id,
                'input_id': incoming.id, 'profile_hash': profile_hash(volunteer), 'ack_message_id': None, 'responses': {}})
        session.add(job)
        receipt.result = {'intent': 'availability_processing', 'session_id': selected.id, 'progress_key': key, 'progress_state': 'ack_pending'}
        session.commit()
    _operation(state, key, 'ack')
    with state.session_factory() as session:
        receipt = session.get(MacInboundReceipt, data.guid)
        return {**receipt.result, 'duplicate': False}


def _drain(state, *, claimed=False):
    lock = state.mac_progress_lock
    if not claimed and not lock.acquire(blocking=False):
        return
    try:
        with state.session_factory() as session:
            keys = session.scalars(select(m.Notification.key).where(m.Notification.purpose == 'mac_progress',
                m.Notification.state.in_(WORK_STATES)).order_by(m.Notification.created_at)).all()
        for key in keys:
            with state.session_factory() as session:
                job = session.get(m.Notification, key)
                if not job:
                    continue
                _, _, _, source_error = _source(session, state, job)
                if source_error:
                    _mark(session, job, 'superseded', source_error)
                    session.commit()
                    continue
                phase = 'ack' if job.state == 'ack_pending' else 'work'
                if job.state == 'waiting_ack':
                    if not job.detail.get('ack_message_id') and job.detail.get('approval_id'):
                        approval = session.get(m.Approval, job.detail['approval_id'])
                        if approval and approval.status == 'approved' and approval.payload.get('message_id'):
                            job.detail = {**job.detail, 'ack_message_id': approval.payload['message_id']}
                        elif approval and approval.status == 'pending':
                            from app.core import confirmations
                            if confirmations.valid(approval, state.mac_delivery_clock.now()):
                                continue
                        else:
                            _mark(session, job, 'held', 'Acknowledgment review was rejected, expired or invalid')
                            session.commit()
                            continue
                    ack_id = job.detail.get('ack_message_id')
                    ack = session.get(m.Message, ack_id) if ack_id else None
                    if ack and ack.status in {'queued', 'dispatching'}:
                        continue
                    if not ack or ack.status != 'submitted':
                        _mark(session, job, 'held', 'Acknowledgment delivery was uncertain or blocked')
                        session.commit()
                        continue
                    job.state = 'ready'
                    session.commit()
            _operation(state, key, phase)
    finally:
        lock.release()


def kick(state):
    """Bridge polling resumes durable work after a backend restart; never sends native texts."""
    with _initialization_lock:
        if not hasattr(state, 'mac_progress_lock'):
            state.mac_progress_lock = threading.Lock()
    if not state.mac_progress_lock.acquire(blocking=False):
        return {'processing': True}
    thread = threading.Thread(target=_drain, args=(state,), kwargs={'claimed': True}, name='mac-availability-progress', daemon=True)
    state.mac_progress_thread = thread
    try:
        thread.start()
    except RuntimeError:
        state.mac_progress_lock.release()
        raise
    return {'processing': True}
