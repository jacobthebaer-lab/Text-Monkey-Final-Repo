"""Stage one fresh exact review from a proven, never-queued quiet hold."""
from copy import deepcopy

from fastapi import HTTPException
from sqlalchemy import select

from app.core import confirmations, outbound_conversation
from app.core.cloud_composition import reviewed_composition
from app.core.message_style import outbound_style_problem
from app.core.policies import PolicyStore, in_quiet_hours
from app.db import models as m
from app.integrations.google_voice_demo import demo_text_problem
from app.integrations.google_voice_policy import google_voice_demo_allowed
from app.integrations.google_voice_runtime import _clock, is_paused


def successor_review(session, state, actor, approval_id, expected):
    """No approval reset, expiry extension, composition, input replay or send."""
    now = _clock(state).now()
    original = session.scalar(select(m.Approval).where(m.Approval.id == approval_id)
        .with_for_update().execution_options(populate_existing=True))
    def held():
        raise HTTPException(409, "The original quiet hold cannot resume. Request a new proposal when needed.")
    if (not google_voice_demo_allowed(state.settings) or is_paused(session) or
            not state.settings.gloo_api_key or not original or original.kind != 'confirm_text' or
            original.status != 'expired' or original.via != 'web' or not original.decided_at or
            not confirmations.valid(original, now, expected) or original.payload.get('message_id') or
            original.payload.get('transport') != 'google_voice' or
            original.payload.get('purpose') not in {'signup_reply', 'admin_reply', 'coordinator_notify', 'escalation_notify'}):
        held()
    p = original.payload
    selected = state.provider.test_sessions.get(p['phone'])
    approved = session.get(m.Notification, f'review:{original.id}:approve')
    blocked = session.get(m.Notification, f'review:{original.id}:blocked')
    composition = session.get(m.Notification, f'google-voice-gloo:{original.id}')
    if (not selected or p.get('session_id') != selected.id or
            p.get('session_starts_at') != selected.starts_at.isoformat() or
            not selected.starts_at <= original.requested_at <= original.decided_at <= now or
            not composition or composition.detail.get('quiet_predecessor_id') is not None or
            not reviewed_composition(session, original, selected)):
        held()
    for proof, action in ((approved, 'approve'), (blocked, 'blocked')):
        if (not proof or proof.state != 'sent' or proof.purpose != 'human_review' or
                proof.created_at != original.decided_at or proof.detail.get('approval_id') != original.id or
                proof.detail.get('action') != action or proof.detail.get('actor') != original.decided_by or
                proof.detail.get('content_hash') != expected):
            held()
    if blocked.detail.get('detail') != 'inside quiet hours':
        held()
    # Initial invitations own registration links and cannot use this reply-only
    # recovery. The existing fresh-composition flow remains their supported path.
    if p['purpose'] == 'signup_reply' and not p.get('reply_to_message_id'):
        held()
    if session.scalar(select(m.Notification.key).where(m.Notification.key.startswith('confirmation:'),
            m.Notification.detail['approval_id'].as_integer() == original.id).limit(1)):
        held()
    if session.scalar(select(m.Message.id).where(m.Message.direction == 'out',
            m.Message.provider_sid.startswith('GV'), m.Message.status.in_(('dispatching', 'uncertain'))).limit(1)):
        held()
    # Validate mutable policy using a detached approved view, never changing the
    # original expired row or treating its former decision as new authorization.
    view = m.Approval(id=original.id, kind=original.kind, payload=deepcopy(p), status='approved',
        requested_at=original.requested_at, decided_at=original.decided_at,
        decided_by=original.decided_by, via=original.via)
    session.info['mac_test_session'] = selected
    volunteer = session.get(m.Volunteer, p.get('volunteer_id')) if p.get('volunteer_id') else None
    if (confirmations.delivery_problem(session, state.provider, view, now) or
            demo_text_problem(session, state.provider, p['phone'], p['body'], p['purpose'], now,
                reply_id=p.get('reply_to_message_id')) or
            outbound_conversation.problem(session, purpose=p['purpose'], volunteer=volunteer,
                phone=p['phone'], body=p['body'], now=now, meta=p.get('conversation', {}), approval=view)):
        held()
    if outbound_style_problem(p['body']):
        held()
    policies = PolicyStore(session)
    hours = policies.urgent_quiet_hours() if p.get('urgent') else policies.quiet_hours()
    if in_quiet_hours(now.astimezone(policies.church_tz()), *hours):
        from app.integrations.google_voice_quiet_test import deadline
        if not deadline(session, state.provider, p['phone'], p['purpose'], now, approval=view):
            held()
    key = f'google-quiet-review:{original.id}'
    link = session.get(m.Policy, key)
    if link:
        successor = session.get(m.Approval, link.value.get('successor_id'))
        receipt = session.get(m.Notification, f'google-voice-gloo:{successor.id}') if successor else None
        if (not successor or successor.status != 'pending' or successor.payload != p or
                not confirmations.valid(successor, now, expected) or
                link.value.get('original_hash') != expected or
                not receipt or receipt.detail.get('quiet_predecessor_id') != original.id or
                not reviewed_composition(session, successor, selected)):
            held()
        return {'approval_id': successor.id, 'content_hash': expected, 'already_staged': True,
                'original_approval_id': original.id, 'native_submission_attempted': False}
    successor = m.Approval(kind='confirm_text', payload=deepcopy(p), status='pending', requested_at=now)
    session.add(successor)
    session.flush()
    session.add(m.Notification(key=f'google-voice-gloo:{successor.id}', purpose='human_review', state='composed',
        body='', due_at=now, created_at=now, detail={**deepcopy(composition.detail),
            'quiet_predecessor_id': original.id, 'original_content_hash': expected}))
    value = {'original_id': original.id, 'successor_id': successor.id, 'original_hash': expected,
        'actor': actor, 'at': now.isoformat(), 'expires_at': p['expires_at']}
    session.add(m.Policy(key=key, value=value))
    session.add(m.Notification(key=key, purpose='human_review', state='pending', body='',
        due_at=now, created_at=now, detail=value))
    session.flush()
    return {'approval_id': successor.id, 'content_hash': expected, 'already_staged': False,
            'original_approval_id': original.id, 'native_submission_attempted': False}
