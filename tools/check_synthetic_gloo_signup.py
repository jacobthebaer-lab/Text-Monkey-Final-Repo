"""Real Gloo signup check with fictional data and simulated delivery.

Run from the repository: python tools/check_synthetic_gloo_signup.py
Requires GLOO_API_KEY in your private environment. No Messages connection.
"""
import json
import sys
from functools import partial
from dataclasses import replace
from pathlib import Path

root = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(root))
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
finally:
    output = root / 'evals' / 'reports' / 'synthetic-gloo-signup.json'
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps(result,indent=2)+'\n')
print(json.dumps({'passed':result['passed'],'gloo_usage':result['real_gloo_usage']}),flush=True)
raise SystemExit(0 if result['passed'] else 1)
