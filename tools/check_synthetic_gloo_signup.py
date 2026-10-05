"""Real Gloo signup check with fictional data and simulated delivery.

Run: python tools/check_synthetic_gloo_signup.py [--output-dir NEW_DIRECTORY]
Requires GLOO_API_KEY in the process environment; dotenv loading is disabled.
Reports use fresh directories and never replace prior evidence or follow symlinks.
"""
import argparse
from contextlib import contextmanager
from dataclasses import replace
from functools import partial
import json
import os
from pathlib import Path
import sys
from uuid import uuid4

root = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(root))


@contextmanager
def fresh_output_directory(path):
    """Reserve a new directory through non-symlink ancestors, before any API call."""
    path = Path(os.path.abspath(path))
    flags = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW
    descriptor = os.open(path.anchor, flags)
    try:
        for component in path.parts[1:-1]:
            try:
                child = os.open(component, flags, dir_fd=descriptor)
            except FileNotFoundError:
                os.mkdir(component, mode=0o700, dir_fd=descriptor)
                child = os.open(component, flags, dir_fd=descriptor)
            os.close(descriptor)
            descriptor = child
        # Existing directory/file/symlink is a conflict, even if empty.
        os.mkdir(path.name, mode=0o700, dir_fd=descriptor)
        child = os.open(path.name, flags, dir_fd=descriptor)
        os.close(descriptor)
        descriptor = child
        yield path, descriptor
    finally:
        os.close(descriptor)


def write_report(directory_descriptor, result):
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW
    descriptor = os.open('synthetic-gloo-signup.json', flags, 0o600,
                         dir_fd=directory_descriptor)
    with os.fdopen(descriptor, 'w', encoding='utf-8') as report:
        report.write(json.dumps(result, indent=2) + '\n')


def isolated_environment():
    """Set safety switches before app.config/app.main can initialize a store."""
    os.environ.update({
        'PYTHON_DOTENV_DISABLED':'1', 'DATABASE_URL':'sqlite://', 'SMS_PROVIDER':'mock',
        'LIVE_SMS':'false', 'DEMO_MODE':'false', 'AUTOMATION_ENABLED':'false',
        'MAC_BRIDGE_ENABLED':'false', 'MAC_BRIDGE_TOKEN':'', 'MAC_DEMO_PHONES':'',
        'MAC_TEST_SESSIONS':'', 'MAC_TEST_SIGNUP_REPLY_UNTIL':'',
        'BACKEND_BRIDGE_KEY':'', 'SUPABASE_URL':'', 'SUPABASE_PUBLISHABLE_KEY':'',
        'PROFILE_SYNC_ENABLED':'false', 'PROFILE_SYNC_DATABASE_URL':'',
        'PROFILE_SYNC_PHONES':'', 'PROFILE_SYNC_PROJECT_REF':'', 'PROFILE_SYNC_ROLE_MAP':'',
        'PCO_STAFFING_WRITE_ENABLED':'false', 'PCO_STAFFING_POLL_ENABLED':'false',
        'PCO_REVIEW_ENABLED':'false', 'PCO_CORRECTION_LINEAGE_ENABLED':'false',
        'PCO_REVIEW_BINDINGS_PATH':'', 'PCO_REVIEW_SIGNING_KEY_PATH':'',
        'PCO_CORRECTION_LINEAGE_KEY_PATH':'',
        'GOOGLE_VOICE_ENABLED':'false', 'COMPETITION_CONFIRMATION_REQUIRED':'false',
        'GLOO_SIGNUP_REPLIES':'true', 'ALLOW_TEXT_SIGNUP':'true',
    })


def run_signup(*, gloo=None, clock=None, continuation=None):
    # Injected fixture usage remains separate from an explicitly constructed
    # real Gloo client. The CLI always requires the configured real client.
    isolated_environment()
    from sqlalchemy import select
    from app.config import get_settings
    get_settings.cache_clear()
    from app.main import create_app
    from app.core.inbound import handle_inbound
    from app.core.signup_copy import (WELCOME, EXACT_COPY, exact_message, ensure_exact_role_menu,
                                      delivered_exact_invitation)
    from app.core.message_style import outbound_style_problem
    from app.agents.fill_agent import FillContext
    from app.llm.parser import parse_inbound
    from app.llm.gloo_client import GlooClient, GlooUnavailableError
    from app.sms.mock_provider import MockSMSProvider
    from app.db import models as m

    settings = replace(get_settings(), gloo_api_key='' if gloo is not None else get_settings().gloo_api_key)
    app = create_app(settings)
    if gloo is not None:
        app.state.gloo = gloo
    if clock is not None:
        app.state.clock = clock
    composition = 'real_gloo' if gloo is None or isinstance(gloo,GlooClient) else 'scripted_gloo'
    result = {'label':'SYNTHETIC DEMO: fictional signup, mock delivery', 'composition':composition,
              'passed':False, 'steps':[], 'real_messages_sent':0}
    phone = '+15555550187'
    expected_copy = [WELCOME, exact_message('interests','Jordan'), EXACT_COPY['availability'], None]
    inputs = [('JOIN','signup_invitation'), ('Jordan Demo','onboarding_interests'),
              ('Greeter','onboarding_availability'), ('Sundays 9-10am, twice a month','onboarding_complete')]
    try:
        if gloo is None and not settings.gloo_api_key:
            raise GlooUnavailableError('GLOO_API_KEY must be configured in the process environment')
        if not isinstance(app.state.provider, MockSMSProvider) or str(app.state.engine.url) != 'sqlite://':
            raise RuntimeError('Replay requires isolated memory storage and mock delivery')
        with app.state.session_factory() as session:
            session.add(m.Policy(key='signup_exact_copy:'+phone,value={'value':True}))
            ensure_exact_role_menu(session)  # The same five-role menu/clearance metadata; no grants.
            session.commit()
            for (body,expected_route),approved in zip(inputs,expected_copy):
                previous = len(app.state.provider.sent)
                previous_id = session.scalar(select(m.Message.id).order_by(m.Message.id.desc()).limit(1)) or 0
                usage_before = app.state.gloo.total_usage()['calls']
                step_error = None
                outcome = None
                try:
                    outcome = handle_inbound(session,app.state.clock,app.state.provider,phone,body,
                        partial(parse_inbound,app.state.gloo),
                        ctx=FillContext(session,app.state.clock,app.state.provider,app.state.gloo),allow_signup=True)
                except GlooUnavailableError as error:
                    # Preserve only this disposable replay's actual audit/hold state.
                    # An exception is not a successfully delivered invitation.
                    step_error = error
                session.commit()
                outgoing = session.scalars(select(m.Message).where(m.Message.id>previous_id,
                    m.Message.direction=='out').order_by(m.Message.id)).all()
                sent = app.state.provider.sent[previous:]
                row = {'fictional_input':body, 'route':outcome.routed_to if outcome else 'raised_gloo_unavailable','expected_route':expected_route,
                    'expected_mock_messages':int(approved is not None),
                    'simulated_replies':[message.body for message in sent],
                    'mock_messages':[{'id':message.id,'body':message.body,'purpose':message.purpose,
                        'status':message.status,'provider_sid':message.provider_sid} for message in outgoing],
                    'successful_gloo_responses':app.state.gloo.total_usage()['calls']-usage_before,
                    'real_delivery':False,
                    'open_escalations':[{'category':e.category,'status':e.status} for e in
                        session.scalars(select(m.Escalation).where(m.Escalation.status=='open'))]}
                if step_error is not None:
                    row['error_type'] = type(step_error).__name__
                result['steps'].append(row)
                print(json.dumps(row),flush=True)
                if step_error is not None:
                    raise step_error
                if outcome.routed_to!=expected_route:
                    raise RuntimeError('Signup did not advance to its expected stage')
                count = int(approved is not None)
                if len(sent)!=count or len(outgoing)!=count:
                    raise RuntimeError('Replay did not produce exactly the expected essential mock messages')
                if row['successful_gloo_responses']<1:
                    raise RuntimeError('Signup stage has no successful Gloo interpretation/composition')
                for delivered, recorded in zip(sent,outgoing):
                    if (delivered.to!=phone or recorded.phone!=phone or recorded.status!='sent'
                            or not (recorded.provider_sid or '').startswith('MOCK')
                            or recorded.provider_sid!=delivered.sid or recorded.body!=delivered.body
                            or recorded.body!=approved or outbound_style_problem(recorded.body)):
                        raise RuntimeError('Mock delivery and approved recorded copy do not match')
            volunteer = session.scalar(select(m.Volunteer).where(m.Volunteer.phone==phone))
            if (volunteer is None or volunteer.name!='Jordan Demo' or not volunteer.sms_opt_in
                    or volunteer.preferences.get('onboarding_stage')!='complete'
                    or volunteer.preferences.get('consent_source')!='sms_name_reply_to_exact_invitation'
                    or volunteer.preferences.get('interested_roles')!=['Greeter']
                    or volunteer.preferences.get('max_per_month')!=2
                    or volunteer.preferences.get('availability_frequency_known') is not True
                    or volunteer.preferences.get('availability_weekdays')!=[6]):
                raise RuntimeError('Signup did not save the sender-authorized identity/consent/preferences')
            name_reply = session.scalar(select(m.Message).where(m.Message.phone==phone,
                m.Message.direction=='in', m.Message.status=='received', m.Message.body=='Jordan Demo'))
            invitation = session.scalar(select(m.Message).where(m.Message.phone==phone,
                m.Message.direction=='out', m.Message.status=='sent', m.Message.body==WELCOME))
            if (name_reply is None or invitation is None or invitation.id>=name_reply.id
                    or not delivered_exact_invitation(session,app.state.clock,phone,
                        reply_message_id=name_reply.id,body=name_reply.body)):
                raise RuntimeError('Consent lacks the recorded disclosure and actual later name reply')
            result['consent_provenance'] = {'verified':True,
                'disclosure_message_id':invitation.id, 'reply_message_id':name_reply.id,
                'disclosure_status':invitation.status, 'reply_status':name_reply.status}
            windows = volunteer.preferences.get('recurring_windows',[])
            if len(windows)!=1 or not any(w['weekday']==6 and w['role_ids']==[1]
                       and w['time_mode']=='clock' and w['start_time']=='09:00'
                       and w['end_time']=='10:00' and not w['all_day'] and w['event_context'] is None
                       for w in windows):
                raise RuntimeError('Signup did not preserve the validated Greeter time window')
            if session.scalar(select(m.Assignment.id)) or session.scalar(select(m.Qualification.id)):
                raise RuntimeError('Signup must not create assignments or clearance grants')
            result['fictional_profile'] = {'name':volunteer.name,'sms_opt_in':volunteer.sms_opt_in,
                                         'preferences':volunteer.preferences}
            if continuation is not None:
                result['continuation'] = continuation(session, app)
            result['passed'] = True
    except Exception as error:
        result['error_type'] = type(error).__name__  # Never echo model errors/config values.
    finally:
        with app.state.session_factory() as audit_session:
            result['gloo_audit'] = [{'agent':a.agent,'outcome':a.outcome,'model':a.model,
                'input_tokens':a.input_tokens,'output_tokens':a.output_tokens}
                for a in audit_session.scalars(select(m.AgentRun))]
        usage = app.state.gloo.total_usage()
        result['real_gloo_usage'] = usage if composition=='real_gloo' else {'calls':0,'input_tokens':0,'output_tokens':0}
        result['scripted_gloo_usage'] = usage if composition=='scripted_gloo' else None
        app.state.engine.dispose()
    return result


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--output-dir', type=Path,
                        help='New report directory; existing paths and symlink ancestors are refused')
    args = parser.parse_args(argv)
    # Reuse the existing ignored *-logs/ rule for generated replay evidence.
    output = args.output_dir or root / 'evals' / 'reports' / ('signup-' + uuid4().hex + '-logs')
    # Reservation rejects collisions before importing app.main or spending model tokens.
    try:
        with fresh_output_directory(output) as (directory, descriptor):
            result = run_signup()
            write_report(descriptor, result)
    except OSError:
        parser.exit(2, 'Report storage unavailable; choose a new directory with no symlink ancestors.\n')
    print(json.dumps({'passed': result['passed'], 'gloo_usage': result['real_gloo_usage'],
                     'report': str(directory / 'synthetic-gloo-signup.json')}), flush=True)
    return 0 if result['passed'] else 1


if __name__ == '__main__':
    raise SystemExit(main())
