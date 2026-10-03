"""Real Gloo regression demo with fictional input and simulated delivery only."""
import argparse
import json
import os
import sys
from datetime import datetime, timezone
from functools import partial
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))


def run_case(name, answers, expected_days, frequency, settings):
    from sqlalchemy import select
    from app.agents.fill_agent import FillContext
    from app.core.inbound import handle_inbound
    from app.core.send_gate import SendGate
    from app.core.signup_responder import compose_signup_reply
    from app.db import models as m
    from app.llm.parser import parse_inbound
    from app.main import create_app
    from app.sms.mock_provider import MockSMSProvider

    app = create_app(settings)
    assert isinstance(app.state.provider, MockSMSProvider)
    case = {'name': name, 'label': 'fictional input; real Gloo; simulated delivery', 'steps': []}
    phone = '+15555550122'
    try:
        with app.state.session_factory() as session:
            session.add(m.Policy(key='full_text_onboarding', value={'value': True}))
            session.add(m.Role(name='Greeter', ministry='Fictional demo church',
                required_qualifications=['background_check'], criticality='standard', fill_policy='needs_approval'))
            session.commit()
            welcome = compose_signup_reply(session, app.state.clock, app.state.gloo,
                'Welcome to Text Monkey! What is your first and last name? Reply STOP to stop or HELP for help.',
                ('first and last name', 'STOP', 'HELP'), phone=phone, signup_conversation=True, require_gloo=True)
            SendGate(session, app.state.clock, app.state.provider).send(phone=phone, body=welcome,
                purpose='signup_reply', kind='agent')
            session.commit()
            for body, expected_route in [('Rowan Example', 'signup_consent_pending'),
                ('YES', 'onboarding_interests'), ('greeter', 'onboarding_availability'), *answers]:
                before = len(app.state.provider.sent)
                result = handle_inbound(session, app.state.clock, app.state.provider, phone, body,
                    partial(parse_inbound, app.state.gloo),
                    ctx=FillContext(session, app.state.clock, app.state.provider, app.state.gloo), allow_signup=True)
                session.commit()
                v = session.scalar(select(m.Volunteer).where(m.Volunteer.phone == phone))
                step = {'fictional_input': body, 'route': result.routed_to, 'expected_route': expected_route,
                    'simulated_replies': [row.body for row in app.state.provider.sent[before:]],
                    'saved_preferences': dict(v.preferences) if v else {}}
                case['steps'].append(step)
                assert result.routed_to == expected_route
                if body == 'Rowan Example':
                    assert v is not None and not v.sms_opt_in and v.preferences['consent_pending']
                if result.routed_to == 'onboarding_clarify':
                    draft = v.preferences['onboarding_availability_draft']
                    if draft['availability_known']:
                        assert not draft['frequency_known']
                        assert all('flexible' not in text.lower() and 'which days' not in text.lower()
                            for text in step['simulated_replies'])
            assert v.sms_opt_in and v.preferences['onboarding_stage'] == 'complete'
            assert set(v.preferences['availability_weekdays']) == set(expected_days)
            assert v.preferences['availability_all_day'] and v.preferences['preferred_services'] == []
            assert v.preferences['max_per_month'] == frequency
            assert not v.qualifications and not v.is_coordinator and not v.is_pastor
            assert session.scalar(select(m.Assignment)) is None
            assert all(row.sid.startswith('MOCK') for row in app.state.provider.sent)
            case['final_preferences'] = dict(v.preferences)
            case['gloo_audit'] = [{'agent': row.agent, 'model': row.model, 'outcome': row.outcome,
                'input_tokens': row.input_tokens, 'output_tokens': row.output_tokens}
                for row in session.scalars(select(m.AgentRun).order_by(m.AgentRun.id))]
        case['passed'] = True
    except Exception as error:
        case.update(passed=False, failure_type=type(error).__name__)
    case['real_gloo_usage'] = app.state.gloo.total_usage()
    case['real_messages_sent'] = 0
    return case


def main():
    from dotenv import dotenv_values
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--env-file', type=Path, help='Existing private credentials; never copied into evidence')
    parser.add_argument('--output', type=Path, default=ROOT/'evals/reports/natural-availability-real-gloo.json')
    args = parser.parse_args()
    values = dotenv_values(args.env_file) if args.env_file else {}
    key = os.environ.get('GLOO_API_KEY') or values.get('GLOO_API_KEY')
    if not key:
        raise SystemExit('Real Gloo credentials are required; no fallback was run.')
    from app.config import Settings
    settings = Settings(database_url='sqlite://', demo_mode=False, automation_enabled=False,
        sms_provider='mock', allow_text_signup=True, gloo_signup_replies=True,
        gloo_api_key=key, gloo_endpoint=os.environ.get('GLOO_ENDPOINT') or values.get('GLOO_ENDPOINT') or 'guarded',
        parser_model=os.environ.get('PARSER_MODEL') or values.get('PARSER_MODEL') or 'gloo-openai-gpt-5-mini')
    scenarios = [
        ('multiple days, all day, correction, frequency followup', [
            ('Sundays and Wednesdays all day', 'onboarding_clarify'),
            ('Actually Thursdays instead of Wednesdays', 'onboarding_clarify'),
            ('Three times a month', 'onboarding_complete')], [6, 3], 3),
        ('frequency supplied in the same answer', [
            ('Sundays and Wednesdays all day, twice a month', 'onboarding_complete')], [6, 2], 2),
        ('frequency first, then normal multiple weekdays', [
            ('Once a month', 'onboarding_clarify'),
            ('Sundays and Wednesdays all day', 'onboarding_complete')], [6, 2], 1),
    ]
    results = []
    for name, answers, days, frequency in scenarios:
        result = run_case(name, answers, days, frequency, settings)
        results.append(result)
        print(json.dumps({'case': name, 'passed': result['passed'],
            'gloo_calls': result['real_gloo_usage']['calls']}), flush=True)
    report = {'checked_at': datetime.now(timezone.utc).isoformat(), 'real_messages_sent': 0,
        'label': 'Real Gloo interpretation/composition with fictional profiles and simulated transport',
        'cases': results, 'passed': all(case['passed'] for case in results)}
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(report, indent=2)+'\n')
    return 0 if report['passed'] else 1


if __name__ == '__main__':
    raise SystemExit(main())
