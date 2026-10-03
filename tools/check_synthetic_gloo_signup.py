"""Real Gloo signup check with fictional data and simulated delivery.

Run: python tools/check_synthetic_gloo_signup.py [--output-dir NEW_DIRECTORY]
Requires private GLOO_API_KEY. Use the runbook's safe environment overrides.
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


def run_signup():
    from sqlalchemy import select
    from app.config import get_settings
    from app.main import create_app
    from app.core.inbound import handle_inbound
    from app.core.signup_responder import compose_signup_reply
    from app.core.send_gate import SendGate
    from app.agents.fill_agent import FillContext
    from app.llm.parser import parse_inbound
    from app.db import models as m

    private_settings = get_settings()
    if not private_settings.gloo_api_key:
        raise SystemExit('Set GLOO_API_KEY privately before this optional live-model check.')
    settings = replace(private_settings, database_url='sqlite://', demo_mode=False,
        automation_enabled=False, allow_text_signup=True, gloo_signup_replies=True,
        sms_provider='mock', live_sms=False, mac_bridge_enabled=False,
        mac_bridge_token='', mac_demo_phones='', mac_test_sessions='',
        backend_bridge_key='', supabase_url='', supabase_publishable_key='')
    app=create_app(settings)
    steps=[]
    try:
        with app.state.session_factory() as session:
            session.add(m.Policy(key='full_text_onboarding',value={'value':True}))
            for name in ('Greeter','Usher','Production'):
                session.add(m.Role(name=name,ministry='Sunday service',required_qualifications=[],
                    criticality='standard',fill_policy='auto'))
            session.commit()
            welcome=compose_signup_reply(session,app.state.clock,app.state.gloo,
                'Welcome to Text Monkey! What is your first and last name? Reply STOP to stop or HELP for help.',
                ('first and last name','STOP','HELP'),phone='+15555550187',signup_conversation=True)
            SendGate(session,app.state.clock,app.state.provider).send(
                phone='+15555550187',body=welcome,purpose='signup_reply',kind='agent')
            session.commit()
            steps.append({'stage':'initial welcome','simulated_reply':welcome,'real_delivery':False})
            for body,expected in [('Jordan Demo','signup_consent_pending'),('YES','onboarding_interests'),
                ('greeter','onboarding_availability'),('Sundays at 9am, twice a month','onboarding_complete')]:
                before=len(app.state.provider.sent)
                outcome=handle_inbound(session,app.state.clock,app.state.provider,'+15555550187',body,
                    partial(parse_inbound,app.state.gloo),
                    ctx=FillContext(session,app.state.clock,app.state.provider,app.state.gloo),allow_signup=True)
                session.commit()
                row={'fictional_input':body,'route':outcome.routed_to,'expected_route':expected,
                    'simulated_replies':[x.body for x in app.state.provider.sent[before:]],'real_delivery':False}
                steps.append(row)
                print(json.dumps(row),flush=True)
                if outcome.routed_to!=expected:
                    raise RuntimeError('Signup did not advance to its expected stage')
            volunteer=session.scalar(select(m.Volunteer))
            assert volunteer.sms_opt_in and volunteer.preferences.get('onboarding_stage')=='complete'
            assert volunteer.preferences.get('max_per_month')==2
            assert volunteer.preferences.get('interested_roles')==['Greeter']
            profile={'name':volunteer.name,'sms_opt_in':volunteer.sms_opt_in,'preferences':volunteer.preferences}
            audits=[{'agent':x.agent,'outcome':x.outcome,'model':x.model,
                'input_tokens':x.input_tokens,'output_tokens':x.output_tokens} for x in session.scalars(select(m.AgentRun))]
        result={'label':'SYNTHETIC DEMO: real Gloo interpretation/composition, simulated delivery',
            'passed':True,'steps':steps,'fictional_profile':profile,'gloo_audit':audits,
            'real_gloo_usage':app.state.gloo.total_usage(),'real_messages_sent':0}
    except Exception as error:
        result={'label':'SYNTHETIC DEMO: real Gloo interpretation/composition, simulated delivery',
            'passed':False,'steps':steps,'error_type':type(error).__name__,
            'real_gloo_usage':app.state.gloo.total_usage(),'real_messages_sent':0}
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
