#!/usr/bin/env python3
"""Offline fictional rehearsal. Scripted Gloo, mock delivery, no ranking or live calls."""
import argparse
import json
import os
from pathlib import Path
import sys
from types import SimpleNamespace

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from tools.check_synthetic_gloo_signup import isolated_environment, fresh_output_directory
isolated_environment()  # Before every backend import, including module-level app.
# This tool is offline only. A funded inherited key must never create a real client.
os.environ['GLOO_API_KEY'] = ''

from datetime import datetime, timedelta
from zoneinfo import ZoneInfo
from functools import partial
from uuid import uuid4

TZ = ZoneInfo('America/Denver')
START = datetime(2026, 10, 9, 10, tzinfo=TZ)
REPLACEMENT_PHONE = '+15555550188'


class ScriptedGloo:
    """Synthetic contract fixture, never a claim of real Gloo interpretation."""
    def __init__(self, outage=None):
        self.calls = []
        self.outage = outage

    def create_response(self, **kwargs):
        from app.llm.gloo_client import GlooUnavailableError
        try:
            facts = json.loads(kwargs['input'])
        except ValueError:
            facts = {'parser_body':kwargs['input']}
        kind = ('identity' if isinstance(facts, list) else
                'recovery' if facts.get('recovery') else
                'copy' if 'approved_message' in facts else
                'parser' if 'parser_body' in facts else facts['stage'])
        if self.outage == kind:
            raise GlooUnavailableError('Offline rehearsal outage')
        if kind == 'identity':
            body = facts[-1]['body']
            identities = {'Jordan Demo':('Jordan','Demo'), 'Avery':('Avery',None), 'Demo':(None,'Demo')}
            first,last = identities.get(body,(None,None))
            output = json.dumps(dict(signup=bool(first or last), first_name=first,
                last_name=last, identity_reply=bool(first or last)))
        elif kind == 'recovery':
            recovery = facts['recovery']
            output = json.dumps(dict(stage=recovery['stage'], missing=recovery['missing'],
                acknowledgment='', question=facts['approved_message']))
        elif kind == 'copy':
            output = facts['approved_message']
        elif kind == 'parser':
            assert 'Cancel my Greeter' in facts['parser_body']
            output = json.dumps(dict(intent='cancel',shift_hint='Greeter Sunday',confidence=1,
                                    sensitive=False,severity='normal',dates=[]))
        elif kind == 'interests':
            output = json.dumps(dict(understood=True, any_role=False, role_ids=[1]))
        elif kind == 'availability':
            output = json.dumps(dict(understood=True, availability_known=True,
                frequency_known=True, weekdays=[6], all_day=False, preferred_services=[],
                max_per_month=2, available_dates=[], unavailable_dates=[],
                recurring_windows=[dict(weekday=6,role_ids=[1],role_label='Greeter',any_role=False,time_mode='clock',
                    start_time='09:00',end_time='10:00',all_day=False,event_context=None)]))
        else:
            raise AssertionError('Unexpected interpretation contract')
        self.calls.append({'kind':kind,'facts':facts,
            'input_bytes':len(kwargs['input'].encode()),
            'instruction_bytes':len((kwargs.get('instructions') or '').encode())})
        return SimpleNamespace(output_text=output, usage=SimpleNamespace(input_tokens=0,output_tokens=0))

    def total_usage(self):
        return dict(calls=len(self.calls),input_tokens=0,output_tokens=0)


def continuation(session, app):
    from sqlalchemy import select
    from fastapi.testclient import TestClient
    from app.db import models as m
    from app.agents.fill_agent import FillContext
    from app.core import confirmations, eligibility, reminders, notifications
    from app.core.inbound import handle_inbound
    from app.llm.parser import parse_inbound
    from app.web.texty import admin
    from app.web.routes import db
    clock, model, provider = app.state.clock, app.state.gloo, app.state.provider
    ctx = FillContext(session,clock,provider,model)
    timeline = []
    model.rehearsal_timeline = timeline

    def checkpoint(step, **extra):
        session.commit()
        timeline.append({'step':step,'fake_time':clock.now().isoformat(), **extra})

    def incoming(phone, body):
        before = len(provider.sent)
        result = handle_inbound(session,clock,provider,phone,body,partial(parse_inbound,model),ctx=ctx,allow_signup=True)
        session.commit()
        return result, provider.sent[before:]

    # A second fictional person gives only a first name. Code owns the precise
    # missing-last-name question; later input must retain the saved first name.
    session.add(m.Policy(key='signup_exact_copy:'+REPLACEMENT_PHONE,value={'value':True}))
    session.commit()
    for body, route in [('JOIN','signup_invitation'),('Avery','signup_name_needed'),
                        ('Demo','onboarding_interests'),('Greeter','onboarding_availability'),
                        ('Sundays 9-10am, twice a month','onboarding_complete')]:
        result, sent = incoming(REPLACEMENT_PHONE,body)
        assert result.routed_to == route, (body,result.routed_to)
        if body == 'Avery':
            assert [s.body for s in sent] == ["What's your last name?"]
        if route == 'onboarding_complete':
            assert not sent
        checkpoint('Replacement missing-fact signup', input=body, route=result.routed_to,
                   mock_replies=[s.body for s in sent])
    jordan = session.scalar(select(m.Volunteer).where(m.Volunteer.name=='Jordan Demo'))
    avery = session.scalar(select(m.Volunteer).where(m.Volunteer.name=='Avery Demo'))
    assert avery.sms_opt_in and avery.preferences['onboarding_stage']=='complete'
    assert not avery.is_coordinator and not avery.is_pastor and not list(avery.qualifications)

    # Explicit event/slot fixture. No planner, candidate search or recipient score.
    event = m.Event(title='FICTIONAL: Sunday Welcome',starts_at=START+timedelta(days=2,hours=-1),
                    ends_at=START+timedelta(days=2),status='scheduled')
    session.add(event); session.flush()
    shift = m.Shift(event_id=event.id,role_id=1,slot_index=0)
    session.add(shift)
    restricted_event=m.Event(title='FICTIONAL: Clearance check only',
        starts_at=event.starts_at+timedelta(days=1),ends_at=event.ends_at+timedelta(days=1),status='scheduled')
    session.add(restricted_event);session.flush()
    restricted=m.Shift(event_id=restricted_event.id,role_id=5,slot_index=0)
    session.add(restricted);session.commit()
    session.info.update(competition_confirmation_required=True, confirmation_now=clock.now())
    # These are fictional fixture-authenticated requests, not a real admin login.
    app.dependency_overrides[admin] = lambda: {'id':'11111111-1111-4111-8111-111111111111',
                                             'email':'fixture-admin@example.test'}
    app.dependency_overrides[db] = lambda: session

    def review(approval):
        assert approval.status=='pending' and confirmations.valid(approval,clock.now())
        expected = approval.payload['content_hash']
        with TestClient(app) as client:
            wrong = client.post(f'/api/proposals/{approval.id}/approve',json={'content_hash':'wrong'})
            assert wrong.status_code==409 and approval.status=='pending'
            response = client.post(f'/api/proposals/{approval.id}/approve',json={'content_hash':expected})
            assert response.status_code==200, response.text
            data = response.json()
            assert approval.status=='approved'
            assert client.post(f'/api/proposals/{approval.id}/approve',json={'content_hash':expected}).status_code==409
        checkpoint('Exact synthetic admin review',approval_id=approval.id, kind=approval.kind,
                   content_hash=expected, outcome=data['delivery'], mock_sms_count=data.get('mock_sms_count'))
        return data

    def fixture_assignment(person):
        assert eligibility.check(session,person,shift)
        row = m.Assignment(shift_id=shift.id,volunteer_id=person.id,status='approved',source='planner',
                           created_at=clock.now(),updated_at=clock.now())
        session.add(row); session.flush()
        # Code must hold the explicit selected fixture until exact record review.
        assert row not in session and not session.scalar(select(m.Assignment).where(
            m.Assignment.volunteer_id==person.id,m.Assignment.status=='approved'))
        approval = session.scalars(select(m.Approval).where(m.Approval.kind=='confirm_record',
            m.Approval.status=='pending')).one()
        review(approval)
        return session.get(m.Assignment,approval.payload['applied_record_id'])

    original = fixture_assignment(jordan)
    # No real principal can be inferred from our explicit fixture auth override.
    app.dependency_overrides.pop(admin)
    with TestClient(app) as client:
        assert client.post(f'/api/proposals/{original.id}/approve',json={}).status_code in {401,503}
    app.dependency_overrides[admin] = lambda: {'email':'fixture-admin@example.test'}
    reminders.process(ctx)
    scheduled = session.scalars(select(m.Approval).where(m.Approval.kind=='confirm_text',
        m.Approval.status=='pending')).one()
    assert event.title in scheduled.payload['body'] and 'Jordan' in scheduled.payload['body']
    assert review(scheduled)['delivery']=='simulated'
    calls = model.total_usage()['calls']; reminders.process(ctx); assert model.total_usage()['calls']==calls
    checkpoint('Factual scheduled notice',body=scheduled.payload['body'],deduplicated=True)

    clock.set_time(START+timedelta(days=1,hours=-2))  # Saturday08:00
    session.info['confirmation_now']=clock.now()
    reminders.process(ctx)
    reminder = session.scalars(select(m.Approval).where(m.Approval.kind=='confirm_text',
        m.Approval.status=='pending')).one()
    expected = "Hey Jordan, Text Monkey here. You're signed up to greet tomorrow at 9am. If we don't hear from you, we'll assume you're good to go. If you can't make it, just let me know."
    assert reminder.payload['body']==expected
    assert review(reminder)['delivery']=='simulated'
    checkpoint('Exact day-before reminder',body=expected)

    # Genuine quiet-hour cancellation route reaches its existing waiting_quiet
    # boundary before candidate search. Nothing is monkeypatched or re-enabled.
    clock.set_time(START+timedelta(days=1,hours=12))  # Saturday22:00
    session.info['confirmation_now']=clock.now()
    outcome,_ = incoming(jordan.phone,'Cancel my Greeter shift on Sunday October 11')
    assert original.status=='cancelled', outcome
    fill = session.scalars(select(m.FillRequest)).one()
    assert fill.state=='waiting_quiet' and not session.scalar(select(m.Outreach.id))
    checkpoint('Cancellation, selection deferred',route=outcome.routed_to,fill_state=fill.state,
               recipient_selection='awaiting Clyde, not exercised')

    # The console admin and existing qualifications are explicitly fictional
    # setup fixtures; signup never grants admin or child-care clearance.
    session.info['record_authorized']=True
    coordinator = m.Volunteer(name='Fictional Coordinator',phone='+15555550189',status='active',
        sms_opt_in=True,is_coordinator=True,is_pastor=False,preferences={},created_at=clock.now())
    session.add(coordinator);session.flush()
    session.info['record_authorized']=False
    clock.set_time(START+timedelta(days=2,hours=-4))  # Sunday06:00, exactly3h
    session.info['confirmation_now']=clock.now()
    notifications.queue_pre_event_updates(ctx); notifications.flush_due(ctx)
    status = session.scalars(select(m.Notification).where(m.Notification.key.startswith('pre-event:'))).one()
    assert status.state=='awaiting_approval' and status.message_id is None
    # Exact review can be prepared at06:00; delivery still must respect quiet hours.
    first_status=session.get(m.Approval,status.detail['approval_id'])
    assert first_status.status=='pending' and '0/1' in first_status.payload['body']
    checkpoint('Three-hour status held for exact review',status=status.state,approval_id=first_status.id)
    clock.set_time(START+timedelta(days=2,hours=-3))  # Sunday07:00
    session.info['confirmation_now']=clock.now()
    # Greeter requires no clearance; the explicit replacement passes actual hard
    # eligibility. A Child Care fixture must fail without its verified grants.
    denied = eligibility.check(session,avery,restricted)
    assert not denied and any('missing qualification:' in r for r in denied.reasons)
    replacement = fixture_assignment(avery)
    # Record the reviewed explicit fill closure, without calling any fill timer.
    fill.state='filled';fill.closed_at=clock.now();fill.next_action_at=None
    reminders.process(ctx)
    pending = session.scalars(select(m.Approval).where(m.Approval.kind=='confirm_text',m.Approval.status=='pending',
        m.Approval.payload['purpose'].as_string()=='confirmation')).all()
    for proposal in pending:
        assert 'Avery' in proposal.payload['body'] and review(proposal)['delivery']=='simulated'
    # Staffing changed while the exact three-hour review was held. The original
    # hash still identifies old copy and must never authorize that stale claim.
    session.commit()
    outgoing_before=session.scalar(select(m.Message.id).where(m.Message.direction=='out').order_by(m.Message.id.desc()).limit(1))
    with TestClient(app) as client:
        stale=client.post(f'/api/proposals/{first_status.id}/approve',
                         json={'content_hash':first_status.payload['content_hash']})
        assert stale.status_code==200, stale.text
        assert stale.json()['delivery']!='simulated'
    assert first_status.status=='expired'
    assert session.scalar(select(m.Message.id).where(m.Message.direction=='out').order_by(m.Message.id.desc()).limit(1))==outgoing_before
    checkpoint('Changed staffing refuses original status review',approval_id=first_status.id,
               status=first_status.status,new_mock_messages=0)
    notifications.flush_due(ctx)
    session.commit()
    # Current staffing requires a distinct Gloo composition and exact review.
    for proposal in session.scalars(select(m.Approval).where(m.Approval.kind=='confirm_text',m.Approval.status=='pending')):
        assert proposal.payload['purpose']=='coordinator_notify' and proposal.id!=first_status.id
        assert 'All set:' in proposal.payload['body']
        assert review(proposal)['delivery']=='simulated'
    messages = session.scalars(select(m.Message).where(m.Message.direction=='out')).all()
    updates = [msg for msg in messages if msg.purpose=='coordinator_notify']
    assert len(updates)==1 and 'All set:' in updates[0].body and 'All 1 required spots' in updates[0].body
    assert 'No action needed' in updates[0].body
    before = len(messages); notifications.queue_pre_event_updates(ctx);notifications.flush_due(ctx);session.commit()
    assert len(session.scalars(select(m.Message).where(m.Message.direction=='out')).all())==before
    assert not session.scalar(select(m.Outreach.id))
    assert not session.scalar(select(m.Qualification.id))
    from app.core.message_style import outbound_style_problem
    assert all(not outbound_style_problem(msg.body) and msg.status=='sent' and msg.provider_sid.startswith('MOCK') for msg in messages)
    checkpoint('Explicit eligible replacement and fresh admin status',assignment_id=replacement.id,
        selection='explicit fixture, not algorithm',admin_body=updates[0].body,deduplicated=True,
        child_care_denied=denied.reasons)
    app.dependency_overrides.clear()
    return {'passed':True,'timeline':timeline,'outgoing_records':[{'id':msg.id,'body':msg.body,
            'purpose':msg.purpose,'status':msg.status,'provider_sid':msg.provider_sid,
            'native_delivery_verified':False} for msg in messages],
            'ranking_called':False,'native_messages_sent':0,'auth':'synthetic fixture principal only',
            'pending':'Clyde scoring/selection integration; real Gloo/native delivery/PCO writes unverified'}


def run(outage=None, *, gloo=None):
    if not __debug__:
        return {'passed':False,'error_type':'RuntimeChecksDisabled','real_messages_sent':0,
                'real_gloo_usage':{'calls':0,'input_tokens':0,'output_tokens':0}}
    from app.clock import FakeClock
    from tools.check_synthetic_gloo_signup import run_signup
    # A caller may explicitly inject an authorized Gloo client later; the CLI
    # defaults to offline. No client is built or funded key read here.
    model=gloo or ScriptedGloo(outage)
    result=run_signup(gloo=model,clock=FakeClock(START),continuation=continuation)
    if 'continuation' not in result:
        result['continuation']={'passed':False,'timeline':getattr(model,'rehearsal_timeline',[]),
            'failure_type':result.get('error_type'),'native_messages_sent':0}
    return result


def bounded_real_gloo(settings):
    """Python-only opt-in for a separately authorized fictional model pass.

    One HTTP attempt per call, at most24 calls,150000 total input UTF-8 bytes
    and1024 output tokens per call. No provider/store setup or credential lookup.
    """
    from app.llm.gloo_client import GlooClient, GlooUnavailableError
    from app.core.message_style import NO_EM_DASH_INSTRUCTIONS
    if (settings.sms_provider!='mock' or settings.database_url!='sqlite://'
            or settings.live_sms or settings.automation_enabled or settings.mac_bridge_enabled
            or settings.profile_sync_enabled or settings.pco_review_enabled
            or settings.pco_staffing_write_enabled or settings.pco_staffing_poll_enabled):
        raise ValueError('A real-model rehearsal requires explicitly isolated mock settings')
    class BoundedGloo(GlooClient):
        attempts=0
        input_bytes=0
        def create_response(self, **kwargs):
            if not isinstance(kwargs.get('input'),str) or kwargs.get('tools'):
                raise GlooUnavailableError('Rehearsal only accepts its string factual inputs')
            size=len(kwargs['input'].encode())+len((kwargs.get('instructions') or '').encode())
            size+=len(('\n\n'+NO_EM_DASH_INSTRUCTIONS).encode())
            if self.attempts>=24 or self.input_bytes+size>150000:
                raise GlooUnavailableError('Fictional rehearsal model budget reached, nothing substituted')
            self.attempts+=1;self.input_bytes+=size
            return super().create_response(**{**kwargs,'max_output_tokens':1024})
    return BoundedGloo(settings,max_attempts=1)


def main(argv=None):
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--output-dir',type=Path,help='New directory; existing paths and symlinks refused')
    args=parser.parse_args(argv)
    output=args.output_dir or Path(__file__).resolve().parents[1]/'evals/reports'/('workflow-'+uuid4().hex+'-logs')
    try:
        with fresh_output_directory(output) as (directory,fd):
            result=run()
            descriptor=os.open('fictional-workflow.json',os.O_WRONLY|os.O_CREAT|os.O_EXCL|os.O_NOFOLLOW,0o600,dir_fd=fd)
            with os.fdopen(descriptor,'w') as report:
                json.dump(result,report,indent=2);report.write('\n')
    except OSError:
        parser.exit(2,'Evidence output unavailable; use a new directory without symlink ancestors.\n')
    print(json.dumps({'passed':result['passed'],'report':str(directory/'fictional-workflow.json'),
        'composition':'scripted_gloo','real_messages_sent':0}))
    return 0 if result['passed'] else 1


if __name__=='__main__':
    raise SystemExit(main())
