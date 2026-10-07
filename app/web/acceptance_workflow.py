"""Authenticated, privately scoped event acceptance controls."""
from datetime import datetime, timedelta, timezone
from pathlib import Path

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.concurrency import run_in_threadpool
from fastapi.responses import FileResponse

from app.agents.fill_agent import FillContext
from app.clock import RealClock
from app.core import confirmations, reminders, scheduler
from app.core.policies import PolicyStore, in_quiet_hours
from app.db import models as m
from app.integrations import acceptance_workflow as flow
from app.integrations.google_voice_client import ConnectorUnavailable
from app.llm.gloo_client import GlooUnavailableError
from app.integrations.google_voice_runtime import _clock, _tick_lock
from app.web.google_voice import superadmin, small_json

router = APIRouter()
PREFIX = '/api/acceptance-event'


@router.get('/acceptance-workflow.js', include_in_schema=False)
def asset():
    return FileResponse(Path(__file__).resolve().parents[2] / 'web/texty/public/acceptance-workflow.js')


@router.get(PREFIX)
def status(request: Request, user=Depends(superadmin)):
    return flow.snapshot(request.app.state, user['email'])


def schema(data, keys):
    if set(data) != set(keys):
        raise HTTPException(400, 'Submit only the fields shown in this event test.')


def moment(value):
    try:
        if not isinstance(value, str):
            raise ValueError()
        dt = datetime.fromisoformat(value.replace('Z', '+00:00'))
        if dt.tzinfo is None:
            raise ValueError()
        return dt.astimezone(timezone.utc)
    except (TypeError, ValueError):
        raise HTTPException(400, 'Choose a date and time with an explicit UTC offset.') from None


def record(session, now, name, after, reason, **extra):
    return confirmations.stage(session, now, {'action': 'record_change', 'record': name,
        'record_id': None, 'before': None, 'after': after, 'reason': reason, **extra}, record=True)


def step(state, actor, action, data):
    scope = flow.private_scope(state, actor)
    if not _tick_lock.acquire(blocking=False):
        raise HTTPException(409, 'Another cloud texting step is in progress.')
    try:
        if action == 'dispatch':
            schema(data, ('event_id', 'assignment_id', 'message_id', 'body_hash'))
            flow.dispatch_exact(state, scope, data)
        else:
            with state.session_factory() as session:
                if not confirmations.enabled(session):
                    raise HTTPException(409, 'Exact review must be enabled.')
                value = flow.saved(session, scope)
                now = _clock(state).now()
                ctx = FillContext(session, _clock(state), state.provider, state.gloo)
                if action == 'event':
                    schema(data, ('title', 'starts_at', 'ends_at', 'zone', 'role_id'))
                    if value.get('event_review'):
                        raise HTTPException(409, 'This private test already has an event. Continue its saved review.')
                    tz = str(PolicyStore(session).church_tz())
                    role = session.get(m.Role, data['role_id']) if type(data['role_id']) is int else None
                    start, end = moment(data['starts_at']), moment(data['ends_at'])
                    if (not isinstance(data['title'], str) or not data['title'].startswith('Demo: ') or
                            not 7 <= len(data['title']) <= 200 or data['zone'] != tz or not role or
                            not now < start < end or end - start > timedelta(hours=12)):
                        raise HTTPException(400, 'Use a Demo: title, church timezone, existing role, and future event of at most 12 hours.')
                    reason = f"Create one event in {tz}, with one {role.name} slot (role {role.id})."
                    a = record(session, now, 'Event', {'gcal_event_id': None, 'title': data['title'],
                        'event_type_id': None, 'starts_at': start.isoformat(), 'ends_at': end.isoformat(), 'status': 'scheduled'}, reason)
                    value.update(event_review=a.id, role_id=role.id, role_source=confirmations.values(role), zone=tz)
                elif action == 'approve':
                    schema(data, ('review_id', 'content_hash'))
                    ids = [value.get(k) for k in ('event_review', 'shift_review', 'assignment_review')]
                    receipt = session.get(m.Policy, f"job:reminder:{value['assignment_id']}") if value.get('assignment_id') else None
                    if receipt:
                        ids.append(receipt.value.get('approval_id'))
                    a = session.get(m.Approval, data['review_id']) if type(data['review_id']) is int and data['review_id'] in ids else None
                    if a is None:
                        raise HTTPException(404, 'Review is outside this private test.')
                    if a.id == value.get('event_review'):
                        role = session.get(m.Role, value['role_id'], populate_existing=True)
                        if not role or confirmations.values(role) != value['role_source'] or value['zone'] != str(PolicyStore(session).church_tz()):
                            raise HTTPException(409, 'The selected role or timezone changed. Review setup again.')
                        if moment(a.payload['after']['starts_at']) <= now:
                            raise HTTPException(409, 'The event must still be in the future.')
                    if a.id in (value.get('shift_review'), value.get('assignment_review')):
                        event_review = session.get(m.Approval, value['event_review'])
                        event = session.get(m.Event, value['event_id'], populate_existing=True)
                        role = session.get(m.Role, value['role_id'], populate_existing=True)
                        if (not event or confirmations.values(event) != event_review.payload['after'] or
                                not role or confirmations.values(role) != value['role_source'] or
                                value['zone'] != str(PolicyStore(session).church_tz())):
                            raise HTTPException(409, 'The reviewed event, role or timezone changed.')
                    confirmations.decide(session, ctx.gate, a, approve=True, actor=actor, expected=data['content_hash'], now=now, ctx=ctx)
                    if a.status != 'approved':
                        raise HTTPException(409, 'Review did not approve this action.')
                    if a.id == value.get('event_review'):
                        value['event_id'] = a.payload['applied_record_id']
                        shift = record(session, now, 'Shift', {'event_id': value['event_id'], 'role_id': value['role_id'], 'slot_index': 0},
                                       'Create the one reviewed role slot for this event.')
                        value['shift_review'] = shift.id
                    elif a.id == value.get('shift_review'):
                        value['shift_id'] = a.payload['applied_record_id']
                    elif a.id == value.get('assignment_review'):
                        value['assignment_id'] = a.payload['applied_record_id']
                elif action == 'assignment':
                    schema(data, ('event_id',))
                    if type(data['event_id']) is not int or value.get('event_id') != data['event_id'] or not value.get('shift_id'):
                        raise HTTPException(409, 'Approve this test event and role slot first.')
                    if value.get('assignment_review'):
                        raise HTTPException(409, 'Continue the saved assignment review.')
                    shift = session.get(m.Shift, value['shift_id'])
                    person = flow.target(session, scope)
                    tz = value['zone']
                    if shift.event_id != data['event_id'] or shift.starts_at <= now:
                        raise HTTPException(409, 'The test event is no longer current.')
                    if problem := scheduler.preview_problem(session, person, shift, [], tz):
                        raise HTTPException(409, problem)
                    a = record(session, now, 'Assignment', {'shift_id': shift.id, 'volunteer_id': person.id,
                        'status': 'approved', 'source': 'planner'}, 'Publish the explicit participant assignment after eligibility and capacity checks.',
                        workflow_plan_source=scheduler.planning_source(shift, person, shift.starts_at.astimezone(PolicyStore(session).church_tz()).strftime('%Y-%m')),
                        workflow_plan_timezone=tz)
                    value['assignment_review'] = a.id
                elif action == 'prepare':
                    schema(data, ('event_id', 'assignment_id'))
                    row = flow.assignment(session, scope, value, **data)
                    source = reminders.assignment_source(row, 'reminder')
                    if problem := reminders.source_problem(session, row.volunteer, source, now):
                        raise HTTPException(409, problem)
                    policies = PolicyStore(session)
                    if in_quiet_hours(now.astimezone(policies.church_tz()), *policies.quiet_hours()):
                        raise HTTPException(409, 'Wait until quiet hours end before preparing this reminder.')
                    reminders.once(ctx, f'reminder:{row.id}', row.volunteer, reminders.day_before_copy(row, policies.church_tz()),
                                   'reminder', source=source, exact_copy=True)
                elif action == 'timer':
                    if data == {'enabled': False}:
                        value['timer'] = {**value.get('timer', {}), 'enabled': False, 'state': 'off'}
                    else:
                        schema(data, ('enabled', 'event_id', 'assignment_id', 'message_id', 'body_hash', 'due_at'))
                        if data['enabled'] is not True or not scope['timer_available'] or not isinstance(_clock(state), RealClock):
                            raise HTTPException(409, 'The private real-time reminder job is disabled.')
                        fields = {k: data[k] for k in ('event_id', 'assignment_id', 'message_id', 'body_hash')}
                        message, approval = flow.reminder_message(session, state, scope, value, **fields)
                        due = moment(data['due_at'])
                        expiry = min(moment(approval.payload['expires_at']),
                            message.created_at + timedelta(seconds=state.settings.google_voice_max_queue_age_seconds - 30))
                        if not now <= due < expiry:
                            raise HTTPException(409, 'Choose a real due time before the exact review or queued reminder expires.')
                        value['timer'] = {**fields, 'enabled': True, 'state': 'armed', 'due_at': due.isoformat(),
                                          'actor': actor, 'armed_at': now.isoformat()}
                else:
                    raise HTTPException(404, 'Unknown event test action.')
                flow.save(session, scope, value)
                session.commit()
        if action == 'timer':
            flow.stop_service(state)
            flow.start_service(state)
        return flow.snapshot(state, actor)
    except (ConnectorUnavailable, GlooUnavailableError):
        raise HTTPException(503, 'Cloud texting or Gloo is unavailable. Nothing else was submitted.') from None
    except ValueError:
        raise HTTPException(409, 'The saved action changed or is no longer eligible. Refresh the exact review.') from None
    finally:
        _tick_lock.release()


@router.post(PREFIX + '/{action}')
async def action(request: Request, action: str, user=Depends(superadmin)):
    data = await small_json(request)
    return await run_in_threadpool(step, request.app.state, user['email'], action, data)
