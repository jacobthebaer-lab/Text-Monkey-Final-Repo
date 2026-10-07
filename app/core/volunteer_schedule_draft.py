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
    if data['phase'] == 'withdrawn':
        return f'Got it, {name}! Your unapproved draft has been withdrawn. Any already approved bookings are unchanged.'
    if data['phase'] == 'submitted':
        return f'Thanks, {name}! Your proposed schedule is ready for final coordinator approval: {details}. These shifts are not booked yet.'
    if data['phase'] == 'choose':
        prefix = 'Those choices no longer fit your saved schedule rules. ' if data.get('selection_changed') else ''
        if data.get('selection_changed'):
            options = data.get('options', [])
            if not options:
                return f'Hi {name}! No current openings fit your saved rules. Would you like to review your availability or serving limit?'
            prefix += 'Current options: ' + '; '.join(f'{i}: ' + ' and '.join(describe(s, ZoneInfo(timezone)) for s in [option, *option.get('paired_shifts', [])]) for i, option in enumerate(options, 1)) + '. '
        return prefix + f'Which option(s) would you like, {name}? Reply with the number(s) or the role and date. I will build a draft for you before final coordinator approval.'
    return f'Proposed schedule for you, {name}: {details}. These shifts are not booked yet. Reply YES to submit this finished draft for final coordinator approval, or tell me which option(s) you prefer.'


def _notice(session, clock, gate, gloo, volunteer, row):
    from app.core.ordinary_reply import reply
    gate.gloo = gloo
    return reply(session, clock, gate, volunteer, schedule_draft=row.key)


def help_context(session, volunteer, now, incoming_id):
    """HELP continuation belongs only to a delivered opportunity/draft exchange."""
    last = session.scalar(scope(select(m.Message), session.info.get('mac_test_session')).where(
        m.Message.volunteer_id == volunteer.id, m.Message.phone == volunteer.phone,
        m.Message.direction == 'out', m.Message.id < incoming_id, m.Message.status.in_(('sent', 'submitted')),
        m.Message.created_at >= now-timedelta(days=2)).order_by(m.Message.id.desc()).limit(1))
    if last is None:
        return False
    notice = session.scalar(select(m.Notification).where(m.Notification.message_id == last.id,
        m.Notification.purpose.in_(('booking_status', 'signup_reply'))).limit(1))
    detail = notice.detail or {} if notice else {}
    if last.purpose == 'booking_status':
        meta = detail.get('conversation_meta', {})
        return (meta.get('session_scope') == booking_status.session_binding(session.info.get('mac_test_session'))
            and bool(meta.get('schedule', {}).get('opportunities', {}).get('eligible_open_shifts')))
    return (detail.get('schedule_draft') == _key(session, volunteer)
        and binding(session, volunteer, detail['schedule_draft'], now) is not None)


def handle(session, clock, gate, gloo, volunteer, body):
    now = clock.now()
    if body.strip().upper() in {'HELP', 'STOP', 'START', 'PROFILE', 'SETUP'}:
        return False
    draft_withdrawal = re.fullmatch(r'\s*(?:cancel|withdraw) (?:my|the|this) (?:draft|proposal)[!.\s]*', body, re.I)
    if not draft_withdrawal and re.search(r"\b(?:cancel|unavailable|stop)\b|\bi(?:'m| am| will be) (?:available|away|not available)\b|\bi (?:can't|cannot|won't)\b", body, re.I):
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
    text = body.strip().lower().replace('’', "'")
    withdrawal = (text.rstrip('!.') == 'no' or re.search(r"(?:^|[.!]\s*)(?:don't|do not) (?:book|schedule|sign me up|submit)\b", text)
        or re.fullmatch(r'(?:withdraw|cancel) (?:my|the|this) (?:draft|proposal)[!.]*', text))
    if withdrawal:
        for ident in data['approval_ids']:
            approval = session.scalar(select(m.Approval).where(m.Approval.id == ident).with_for_update().execution_options(populate_existing=True))
            if approval and approval.status == 'pending':
                approval.status, approval.decided_at, approval.decided_by = 'rejected', now, 'Volunteer withdrawal'
                confirmations.audit(session, approval, now, 'reject', 'Volunteer withdrawal')
        row.value = {**data, 'phase':'withdrawn', 'shifts':[], 'last_input_id':gate.reply_to_message_id,
            'expires_at':(now+timedelta(hours=2)).isoformat()}
        _notice(session, clock, gate, gloo, volunteer, row)
        return True
    if data['phase'] == 'submitted':
        approvals = [session.get(m.Approval, ident) for ident in data['approval_ids']]
        if any(a and a.status == 'approved' for a in approvals):
            from app.core.ordinary_reply import acknowledgment, reply
            if acknowledgment(body):
                gate.gloo = gloo
                reply(session, clock, gate, volunteer)
                return True
            return False
        if any(not a or a.status != 'pending' or not confirmations.valid(a, now) for a in approvals):
            _recover(session, clock, gate, gloo, volunteer, row)
            return True
        _notice(session, clock, gate, gloo, volunteer, row)
        return True  # Repeat replies never grant approval or duplicate reviews.
    if not _validate(session, volunteer, data, now):
        _recover(session, clock, gate, gloo, volunteer, row)
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
            reason = f'Final review of volunteer-selected draft, submission {incoming_id}'
            if companion:
                approval = paired_planning.stage_pair(session, now, volunteer, [shift, companion], month, tz, reason=reason)
                consumed.update((shift.id, companion.id))
            else:
                approval = confirmations.stage(session, now, {'action':'record_change', 'record':'Assignment',
                    'record_id':None, 'before':None,
                    'after':{'shift_id':shift.id, 'volunteer_id':volunteer.id, 'status':'approved', 'source':'planner'},
                    'reason':reason,
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
    return _choose(session, clock, gate, gloo, volunteer, row, body)


def _choose(session, clock, gate, gloo, volunteer, row, body):
    now, data, incoming_id = clock.now(), row.value, gate.reply_to_message_id
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
                    _recover(session, clock, gate, gloo, volunteer, row)
                    return True
                if facts not in shifts:
                    shifts.append(facts)
        proposed = {**data, 'phase':'preview', 'shifts':shifts, 'last_input_id':incoming_id}
        if not _validate(session, volunteer, proposed, now):
            _recover(session, clock, gate, gloo, volunteer, row)
            return True
        row.value = proposed
        preview = _notice(session, clock, gate, gloo, volunteer, row)
        # Delivery may still be held by quiet hours, Gloo or exact text review.
        row.value = {**row.value, 'preview_message_id':preview.message_id if preview else None}
        return True
    except GlooUnavailableError:
        # Retry interpretation from the genuine source, never through inbound.
        job_key = f'volunteer-choice:{incoming_id}'
        job = session.get(m.Notification, job_key)
        if job is None:
            job = m.Notification(key=job_key, volunteer_id=volunteer.id, purpose='volunteer_choice_work',
                body='', state='pending', created_at=now, due_at=now+timedelta(minutes=2),
                expires_at=now+timedelta(days=2), detail={'reply_id':incoming_id, 'draft_key':row.key,
                    'source_message_id':data['source_message_id'], 'session_scope':data['session_scope'],
                    'options_hash':paired_planning.fingerprint(data['options'])})
            session.add(job)
        else:
            job.due_at = now+timedelta(minutes=2)
        session.flush()
        return True


def _recover(session, clock, gate, gloo, volunteer, row):
    options = booking_status.snapshot(session, volunteer, clock.now(), include_opportunities=True)['opportunities']['eligible_open_shifts']
    row.value = {**row.value, 'phase':'choose', 'shifts':[], 'options':options, 'approval_ids':[],
        'expires_at':(clock.now()+timedelta(hours=2)).isoformat(), 'last_input_id':gate.reply_to_message_id,
        'selection_changed':True}
    _notice(session, clock, gate, gloo, volunteer, row)


def retry_due(ctx):
    from app.core.privacy import safe_message_history
    from app.llm.parser import keyword_sensitive
    count = 0
    jobs = ctx.session.scalars(select(m.Notification).where(m.Notification.purpose=='volunteer_choice_work',
        m.Notification.state=='pending', m.Notification.due_at<=ctx.clock.now()).with_for_update(skip_locked=True)).all()
    for job in jobs:
        person = ctx.session.get(m.Volunteer, job.volunteer_id)
        incoming = ctx.session.get(m.Message, job.detail['reply_id'])
        selected = getattr(ctx.provider, 'test_sessions', {}).get(person.phone) if person else None
        if selected is not None:
            ctx.session.info['mac_test_session'] = selected
        else:
            ctx.session.info.pop('mac_test_session', None)
        row = ctx.session.get(m.Policy, job.detail['draft_key'])
        latest = ctx.session.scalar(scope(select(m.Message), selected).where(m.Message.volunteer_id==job.volunteer_id,
            m.Message.direction=='in').order_by(m.Message.id.desc()).limit(1))
        if (not person or person.status!='active' or not person.sms_opt_in
                or not incoming or incoming.direction!='in' or incoming.volunteer_id!=person.id
                or incoming.phone!=person.phone or not latest or latest.id!=incoming.id
                or (hasattr(ctx.provider, 'allows') and (not selected or not selected.active(ctx.clock.now())))
                or not row or row.value['phase'] not in {'choose', 'preview'}
                or row.value['session_scope']!=job.detail['session_scope']
                or paired_planning.fingerprint(row.value['options'])!=job.detail['options_hash']
                or ctx.clock.now()>=job.expires_at or keyword_sensitive(incoming.body)
                or not safe_message_history(ctx.session,[incoming])):
            job.state='superseded'
            continue
        gate = ctx.gate
        gate.reply_to_message_id = incoming.id
        if not _validate(ctx.session, person, row.value, ctx.clock.now()):
            _recover(ctx.session, ctx.clock, gate, ctx.gloo, person, row)
            job.state='completed'
            count += 1
            continue
        _choose(ctx.session, ctx.clock, gate, ctx.gloo, person, row, incoming.body)
        if row.value.get('last_input_id')==incoming.id:
            job.state='completed'
        count += 1
    return count
