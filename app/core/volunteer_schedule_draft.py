"""Sender-selected, unbooked schedules, followed by exact coordinator review."""
from datetime import datetime, timedelta
from pathlib import Path
import json
import re
from sqlalchemy import select
from app.db import models as m
from app.core import booking_status, scheduler, confirmations, paired_planning
from app.core.conversation import scope
from app.core.policies import PolicyStore
from app.core.schedule_messages import shift_facts, describe
from app.llm.gloo_client import GlooUnavailableError
from app.llm.parser import _extract_json


def _key(session, volunteer):
    selected = booking_status.session_binding(session.info.get('mac_test_session'))
    return f'volunteer-draft:{volunteer.id}:' + paired_planning.fingerprint(selected)


def _validate(session, volunteer, data, now):
    if (volunteer.status != 'active' or not volunteer.sms_opt_in
            or data.get('session_scope') != booking_status.session_binding(session.info.get('mac_test_session'))
            or now >= datetime.fromisoformat(data['expires_at'])):
        return False
    choices = [{'shift_id':s['shift_id'], 'volunteer_id':volunteer.id} for s in data['shifts']]
    tz = str(PolicyStore(session).church_tz())
    for saved in data['shifts']:
        shift = session.get(m.Shift, saved['shift_id'])
        if (not shift or shift.starts_at <= now or shift.event.status != 'scheduled'
                or shift_facts(shift) != saved or scheduler.preview_problem(session, volunteer, shift, choices, tz)):
            return False
    return True


def binding(session, volunteer, key, now):
    row = session.get(m.Policy, key)
    if key != _key(session, volunteer) or row is None:
        return None
    data = row.value
    if not _validate(session, volunteer, data, now):
        return None
    if data['phase'] == 'submitted':
        approvals = [session.get(m.Approval, i) for i in data['approval_ids']]
        if not approvals or any(not a or a.status != 'pending' or not confirmations.valid(a, now) for a in approvals):
            return None
    return {k:v for k,v in data.items() if k != 'preview_message_id'}


def copy_for(data, name, timezone):
    from zoneinfo import ZoneInfo
    details = '; '.join(describe(s, ZoneInfo(timezone)) for s in data['shifts'])
    if data['phase'] == 'submitted':
        return f'Thanks, {name}! Your proposed schedule is ready for final coordinator approval: {details}. These shifts are not booked yet.'
    if data['phase'] == 'choose':
        prefix = 'Those choices no longer fit your saved schedule rules. ' if data.get('selection_changed') else ''
        return prefix + f'Which option(s) would you like, {name}? Reply with the number(s) or the role and date. I will build a draft for you before final coordinator approval.'
    return f'Proposed schedule for you, {name}: {details}. These shifts are not booked yet. Reply YES to submit this finished draft for final coordinator approval, or tell me which option(s) you prefer.'


def _notice(session, clock, gate, gloo, volunteer, row):
    from app.core.ordinary_reply import reply
    gate.gloo = gloo
    return reply(session, clock, gate, volunteer, schedule_draft=row.key)


def handle(session, clock, gate, gloo, volunteer, body):
    now = clock.now()
    if re.search(r"\b(?:cancel|unavailable|stop)\b|\bi(?:'m| am| will be) (?:available|away|not available)\b|\bi (?:can't|cannot|won't)\b", body, re.I):
        return False
    if ((volunteer.preferences or {}).get('onboarding_stage') not in {None, 'complete'}
            or (volunteer.preferences or {}).get('consent_pending') or volunteer.is_coordinator or volunteer.is_pastor):
        return False
    last = session.scalar(scope(select(m.Message), session.info.get('mac_test_session')).where(
        m.Message.volunteer_id == volunteer.id, m.Message.phone == volunteer.phone,
        m.Message.direction == 'out', m.Message.status.in_(('sent', 'submitted')),
        m.Message.created_at >= now-timedelta(days=2)).order_by(m.Message.id.desc()).limit(1))
    if last is None:
        return False
    key = _key(session, volunteer)
    row = session.get(m.Policy, key)
    notice = session.scalar(select(m.Notification).where(m.Notification.message_id == last.id,
        m.Notification.purpose.in_(('booking_status', 'signup_reply'))).order_by(m.Notification.created_at.desc()).limit(1))
    if last.purpose == 'booking_status':
        if row and row.value.get('phase') == 'preview':
            pending = session.get(m.Notification, f"ordinary-reply:{row.value.get('last_input_id')}")
            if pending and pending.state in {'pending', 'awaiting_approval'}:
                return True  # A held preview never grants permission to submit.
        meta = (notice.detail or {}).get('conversation_meta', {}) if notice else {}
        options = meta.get('schedule', {}).get('opportunities', {}).get('eligible_open_shifts', [])
        if not options or meta.get('session_scope') != booking_status.session_binding(session.info.get('mac_test_session')):
            return False
        data = {'phase':'choose', 'session_scope':meta['session_scope'], 'options':options,
            'shifts':[], 'expires_at':(now+timedelta(hours=2)).isoformat(), 'approval_ids':[], 'source_message_id':last.id}
        if row is None:
            row = m.Policy(key=key, value=data); session.add(row)
        else:
            row.value = data
        session.flush()
    elif not (row and notice and (notice.detail or {}).get('schedule_draft') == key):
        return False
    data = row.value
    if data['phase'] == 'submitted':
        _notice(session, clock, gate, gloo, volunteer, row)
        return True  # Repeat replies never grant approval or duplicate reviews.
    if not _validate(session, volunteer, data, now):
        row.value = {**data, 'phase':'choose', 'shifts':[], 'selection_changed':True,
            'last_input_id':gate.reply_to_message_id}
        _notice(session, clock, gate, gloo, volunteer, row)
        return True
    incoming_id = gate.reply_to_message_id
    if data.get('last_input_id') == incoming_id:
        return True
    if data['phase'] == 'preview' and re.fullmatch(r'\s*(?:yes|y|submit|approve draft)[!.\s]*', body, re.I):
        # A reply cannot submit an undispatched or superseded draft preview.
        if not notice or notice.detail.get('reply_id') != data.get('last_input_id'):
            return False
        zone = PolicyStore(session).church_tz(); tz = str(zone)
        consumed, approvals = set(), []
        for saved in data['shifts']:
            shift = session.get(m.Shift, saved['shift_id'])
            if shift.id in consumed:
                continue
            partner = paired_planning.partner_role(session, volunteer, shift.role_id)
            companion = next((session.get(m.Shift, s['shift_id']) for s in data['shifts']
                if s['role_id'] == partner and session.get(m.Shift, s['shift_id']).starts_at.astimezone(zone).date() == shift.starts_at.astimezone(zone).date()), None)
            month = shift.starts_at.astimezone(zone).strftime('%Y-%m')
            if companion:
                approval = paired_planning.stage_pair(session, now, volunteer, [shift, companion], month, tz)
                consumed.update((shift.id, companion.id))
            else:
                approval = confirmations.stage(session, now, {'action':'record_change', 'record':'Assignment',
                    'record_id':None, 'before':None,
                    'after':{'shift_id':shift.id, 'volunteer_id':volunteer.id, 'status':'approved', 'source':'planner'},
                    'reason':'Final review of the volunteer-selected schedule draft',
                    'workflow_plan_source':scheduler.planning_source(shift, volunteer, month), 'workflow_plan_timezone':tz,
                    **paired_planning.review_binding(session, volunteer, now)}, record=True)
                consumed.add(shift.id)
            approvals.append(approval.id)
        row.value = {**data, 'phase':'submitted', 'approval_ids':approvals, 'last_input_id':incoming_id}
        _notice(session, clock, gate, gloo, volunteer, row)
        return True
    if re.fullmatch(r'\s*(?:yes|y|ok|okay)[!.\s]*', body, re.I):
        row.value = {**data, 'phase':'choose', 'shifts':[], 'last_input_id':incoming_id}
        _notice(session, clock, gate, gloo, volunteer, row)
        return True
    try:
        if gloo is None:
            raise GlooUnavailableError('Gloo is required to interpret draft choices')
        settings = getattr(gloo, 'settings', None)
        response = gloo.create_response(model=settings.parser_model if settings else 'gloo-openai-gpt-5-mini',
            instructions=(Path(__file__).resolve().parents[2]/'prompts/volunteer_choices.md').read_text(),
            input=json.dumps({'reply':body, 'options':data['options']}))
        answer = _extract_json(getattr(response, 'output_text', '') or '')
        numbers = answer.get('choice_numbers') if isinstance(answer, dict) and answer.get('understood') is True else []
        if (not isinstance(numbers, list) or not numbers or any(type(i) is not int or not 1 <= i <= len(data['options']) for i in numbers)):
            row.value = {**data, 'phase':'choose', 'shifts':[], 'last_input_id':incoming_id}
            _notice(session, clock, gate, gloo, volunteer, row)
            return True
        shifts = []
        for number in sorted(set(numbers)):
            option = data['options'][number-1]
            for saved in [option, *option.get('paired_shifts', [])]:
                shift = session.get(m.Shift, saved['shift_id'])
                facts = shift_facts(shift) if shift else None
                if facts is None or any(facts.get(k) != v for k,v in saved.items() if k != 'paired_shifts'):
                    raise GlooUnavailableError('Opening changed before draft selection')
                if facts not in shifts:
                    shifts.append(facts)
        proposed = {**data, 'phase':'preview', 'shifts':shifts, 'last_input_id':incoming_id}
        if not _validate(session, volunteer, proposed, now):
            row.value = {**data, 'phase':'choose', 'shifts':[], 'last_input_id':incoming_id, 'selection_changed':True}
            _notice(session, clock, gate, gloo, volunteer, row)
            return True
        row.value = proposed
        preview = _notice(session, clock, gate, gloo, volunteer, row)
        # Delivery may still be held by quiet hours, Gloo or exact text review.
        row.value = {**row.value, 'preview_message_id':preview.message_id if preview else None}
        return True
    except GlooUnavailableError:
        # A model outage or changed opening cannot cause a booking or fallback.
        return True
