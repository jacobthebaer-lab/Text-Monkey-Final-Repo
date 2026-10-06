"""Opt-in Mac conversations; models propose facts, code owns source and writes."""
import hashlib
import json
import re
from copy import deepcopy
from sqlalchemy import select
from app.db import models as m
from app.core.conversation import inbound_scope


def digest(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, default=str).encode()).hexdigest()


def enabled(session, phone, now):
    selected = session.info.get('mac_test_session')
    policy = session.get(m.Policy, 'conversational_signup:' + phone)
    return bool(selected and selected.outbound_prefix.startswith('MAC') and selected.active(now)
        and policy and policy.value.get('value') is True
        and policy.value.get('session_id') == selected.id)


def church_context(session, volunteer):
    # No first-account guess: require the bound owner or one completed workspace.
    from sqlalchemy import text
    owner = (volunteer.preferences or {}).get('onboarding_copy_owner')
    query = 'SELECT id, revision, details FROM admin_workspaces WHERE completed = 1'
    if owner:
        query += ' AND owner_id = :owner'
    try:
        rows = session.execute(text(query), {'owner': owner}).mappings().all()
    except Exception:
        return {}  # Portable synthetic stores need not have the web workspace table.
    if len(rows) != 1:
        return {}
    details = rows[0]['details']
    details = json.loads(details) if isinstance(details, str) else details
    times = details.get('service_times') if isinstance(details, dict) else None
    if not isinstance(times, str) or not times.strip() or len(times) > 500:
        return {}
    return {'workspace_id': rows[0]['id'], 'revision': rows[0]['revision'],
            'service_times': times, 'timezone': details.get('timezone')}


def source(session, volunteer, incoming_id, now):
    from app.core.signup_recovery import privacy_hold
    from app.core.send_gate import has_open_sensitive_escalation
    selected = session.info.get('mac_test_session')
    if not enabled(session, volunteer.phone, now) or not volunteer.sms_opt_in or volunteer.status != 'active':
        return None
    optout = session.get(m.Policy, 'sms_opt_out:' + volunteer.phone)
    if optout and optout.value.get('value'):
        return None
    if privacy_hold(session,volunteer.phone,volunteer) or has_open_sensitive_escalation(session,volunteer.id):
        return None
    incoming = session.scalar(select(m.Message).where(m.Message.id == incoming_id,
        inbound_scope(selected), m.Message.volunteer_id == volunteer.id,
        m.Message.phone == volunteer.phone, m.Message.direction == 'in', m.Message.status == 'received',
        m.Message.created_at >= selected.starts_at, m.Message.created_at <= now,
        m.Message.created_at < selected.expires_at))
    latest = session.scalar(select(m.Message.id).where(inbound_scope(selected),
        m.Message.phone == volunteer.phone, m.Message.direction == 'in', m.Message.status == 'received'
    ).order_by(m.Message.id.desc()).limit(1))
    return incoming if incoming and latest == incoming.id else None


def partial_availability(data, previous, today, roles, event_types, *, actual_body=''):
    """Retain valid components; invalid windows are pending evidence, never eligibility."""
    from app.core.onboarding import validated_availability
    from app.core.recurring_availability import normalize_recurring_windows
    clean = deepcopy(data)
    windows = clean.get('recurring_windows', previous.get('recurring_windows', []))
    if not isinstance(windows, list) or len(windows) > 64:
        raise ValueError('Invalid recurring availability windows')
    valid, pending = [], []
    for window in windows:
        try:
            valid.extend(normalize_recurring_windows([window], roles, event_types))
        except (ValueError, TypeError):
            if not isinstance(window, dict):
                raise ValueError('Invalid pending window')
            # Raw untrusted output is retained for the next interpretation,
            # outside the validated scheduling preferences.
            pending.append({'kind': 'unresolved_window', 'proposal': window})
    clean['recurring_windows'] = valid
    # Ordinals are not clock hours. Windows own timing; legacy hour enums must
    # not supply invented service times for role-specific replies.
    if windows:
        clean['preferred_services'] = []
    constraints = data.get('pending_constraints', previous.get('pending_constraints', []))
    if not isinstance(constraints, list) or len(constraints) > 16:
        raise ValueError('Invalid pending constraints')
    for item in constraints:
        if item in previous.get('pending_constraints', []) and item.get('kind') in {'unresolved_window','validation'}:
            pending.append(deepcopy(item))
            continue
        if (not isinstance(item, dict) or set(item) != {'kind', 'description', 'role_ids'}
                or item['kind'] not in {'same_day', 'service_time', 'event_mapping'}
                or not isinstance(item['description'], str) or not 0 < len(item['description']) <= 240
                or not isinstance(item['role_ids'], list) or not item['role_ids']
                or not all(type(i) is int and i in {r.id for r in roles} for i in item['role_ids'])):
            raise ValueError('Invalid pending preference constraint')
        pending.append(deepcopy(item))
    if re.search(r'\bsame (?:days|dates|weeks)\b', actual_body, re.I) and not any(
            item.get('kind')=='same_day' for item in pending):
        pending.append({'kind':'same_day','description':'Sender requested linked serving days; dependency needs coordinator mapping.',
            'role_ids':sorted({i for w in valid for i in w['role_ids']})})
    for window in valid:
        if window.get('time_mode')=='event' and not window['event_context']['event_type_ids']:
            pending.append({'kind':'event_mapping','description':window['event_context']['label'],
                'role_ids':window['role_ids']})
    draft = validated_availability(clean, previous, today, roles=roles, event_types=event_types)
    if len(pending)>80 or len(json.dumps(pending,ensure_ascii=False).encode())>65536:
        raise ValueError('Pending preference evidence exceeds the audit bound')
    draft['pending_constraints'] = pending
    return draft


def bind_turn(session, clock, volunteer, gate, stage, draft, step_id):
    incoming = source(session, volunteer, gate.reply_to_message_id, clock.now())
    if incoming is None:
        raise ValueError('Conversation needs its current actual sender input')
    detail = {'incoming_id': incoming.id, 'phone': volunteer.phone,
        'session_id': session.info['mac_test_session'].id, 'stage': stage,
        'body_hash': hashlib.sha256(incoming.body.encode()).hexdigest(),
        'draft_hash': digest(draft), 'step_id': step_id,
        'step_hash': digest(session.get(m.AgentStep,step_id).result)}
    key = 'onboarding-turn:' + str(incoming.id)
    row = session.get(m.Notification, key)
    if row is None:
        session.add(m.Notification(key=key, purpose='onboarding_turn', state='pending',
            volunteer_id=volunteer.id, message_id=incoming.id, body='',
            created_at=clock.now(), due_at=clock.now(), detail=detail))
    elif row.detail != detail:
        # A retry may generate a different extraction, but may not replace the
        # original turn's evidence or acquire a second outbound reservation.
        raise ValueError('Conversation turn already has different evidence')
    session.flush()
    return {'signup_followup': {'incoming_id': incoming.id, 'turn_key': key}}


def followup_binding(session, volunteer, proof, now):
    if not isinstance(proof, dict) or set(proof) != {'incoming_id', 'turn_key'}:
        return None
    incoming = source(session, volunteer, proof['incoming_id'], now) if volunteer else None
    row = session.get(m.Notification, proof['turn_key'])
    if not incoming or not row or row.key != 'onboarding-turn:' + str(incoming.id):
        return None
    detail = row.detail or {}
    complete = detail.get('stage') == 'complete'
    interests = detail.get('stage') == 'interests'
    draft = volunteer.preferences if complete or interests else (volunteer.preferences or {}).get('onboarding_availability_draft')
    step = session.get(m.AgentStep,detail.get('step_id'))
    if (row.purpose != 'onboarding_turn' or row.message_id != incoming.id
            or row.volunteer_id != volunteer.id or detail.get('phone') != volunteer.phone
            or detail.get('session_id') != session.info['mac_test_session'].id
            or detail.get('body_hash') != hashlib.sha256(incoming.body.encode()).hexdigest()
            or detail.get('stage') not in {'interests','availability','complete'}
            or (volunteer.preferences or {}).get('onboarding_stage') != ('complete' if complete else 'interests' if interests else 'availability')
            or detail.get('draft_hash') != digest(draft)):
        return None
    if (not step or step.type!='decision' or step.run.agent!='onboarding'
            or step.result.get('stage')!=('interests' if interests else 'availability') or digest(step.result)!=detail.get('step_hash')
            or step.run.started_at<incoming.created_at or step.created_at>now):
        return None
    return dict(detail)


def recover_recorded(session, clock, gate, volunteer, gloo, *, receipt_guid, step_id, extraction_hash):
    """Python-only operator recovery of an unchanged native receipt, never input replay.

    Caller supplies the independently reviewed original audit hash. This does
    not create an incoming Message or update the native cursor/receipt.
    """
    from app.integrations.mac_models import MacInboundReceipt
    from app.llm.gloo_client import GlooUnavailableError
    from app.core.onboarding import handle
    incoming = source(session, volunteer, gate.reply_to_message_id, clock.now())
    receipt = session.get(MacInboundReceipt, receipt_guid)
    step = session.get(m.AgentStep, step_id)
    if (incoming is None or receipt is None or step is None or step.type != 'decision'
            or step.run.agent != 'onboarding' or step.run.trigger != 'Profile availability'
            or step.run.outcome != 'needs_clarification' or step.run.started_at < incoming.created_at
            or not step.run.ended_at or step.run.ended_at > clock.now()
            or not isinstance(step.result, dict) or step.result.get('stage') != 'availability'
            or digest(step.result.get('extraction')) != extraction_hash
            or receipt.result.get('session_id') != session.info['mac_test_session'].id
            or receipt.result.get('intent') != 'onboarding_suppressed'
            or (volunteer.preferences or {}).get('onboarding_stage') != 'availability'):
        raise GlooUnavailableError('Recorded availability recovery evidence changed')
    expected = hashlib.sha256((incoming.phone + '\0SMS\0' + session.info['mac_test_session'].id
        + '\0' + incoming.body).encode()).hexdigest()
    imessage = hashlib.sha256((incoming.phone + '\0iMessage\0' + session.info['mac_test_session'].id
        + '\0' + incoming.body).encode()).hexdigest()
    if receipt.fingerprint not in {expected, imessage}:
        raise GlooUnavailableError('Native receipt does not identify the original input')
    if session.get(m.Notification, 'onboarding-turn:' + str(incoming.id)) is not None:
        return 'onboarding_suppressed'
    # Reinterpret the original actual text through Gloo with corrected context;
    # keep original audit immutable and record the repair's donor separately.
    prior = session.info.get('onboarding_repair')
    session.info['onboarding_repair'] = {'step_id': step.id, 'extraction_hash': extraction_hash,
        'original_extraction': step.result['extraction']}
    try:
        return handle(session, clock, gate, volunteer, incoming.body, gloo)
    finally:
        if prior is None:
            session.info.pop('onboarding_repair', None)
        else:
            session.info['onboarding_repair'] = prior


def resume_followup(session, clock, gate, volunteer, gloo, *, turn_key, step_hash):
    """Explicit operator continuation of one unsent turn, composition only.

    No parser replay, altered donor, new source or delivery retry. Any durable
    outgoing reservation, including uncertain or blocked, prevents this path.
    """
    from app.core import outbound_conversation
    from app.core.signup_recovery import redirect
    from app.llm.gloo_client import GlooUnavailableError
    row = session.get(m.Notification,turn_key)
    if not row or row.detail.get('step_hash') != step_hash:
        raise GlooUnavailableError('Original conversation audit changed')
    proof={'turn_key':turn_key,'incoming_id':row.detail.get('incoming_id')}
    if gate.reply_to_message_id != proof['incoming_id']:
        raise GlooUnavailableError('Continuation needs the same original input')
    binding=followup_binding(session,volunteer,proof,clock.now())
    if not binding:
        raise GlooUnavailableError('Original conversation scope or preferences changed')
    meta,error=outbound_conversation.metadata(session,purpose='signup_reply',volunteer=volunteer,
        phone=volunteer.phone,now=clock.now(),supplied={'signup_followup':proof})
    if error:
        raise GlooUnavailableError(error)
    if any(session.get(m.Notification,key) is not None for key in meta['keys']):
        return 'onboarding_suppressed'
    complete=binding['stage']=='complete'
    stage='availability' if complete else binding['stage']
    saved=volunteer.preferences if complete or stage=='interests' else volunteer.preferences['onboarding_availability_draft']
    incoming=session.get(m.Message,proof['incoming_id'])
    return redirect(session,clock,gate,gloo,phone=volunteer.phone,body=incoming.body,stage=stage,
        missing=[] if complete else [stage],question='' if complete else 'Please clarify only the unresolved preferences.',
        saved=saved,volunteer=volunteer,conversational=True,complete=complete)
