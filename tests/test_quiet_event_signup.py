"""Fictional event-following answers complete silently, without invented caps."""
from copy import deepcopy
import json
from datetime import date
from types import SimpleNamespace
import pytest
from sqlalchemy import select
from app.core import onboarding
from app.core.signup_copy import ensure_exact_role_menu
from app.core.send_gate import SendGate
from app.db import models as m
from tests.test_concise_signup import PHONE
from tests.test_adaptive_signup import WINDOWS


def fixture_answer(session,clock,make_volunteer,mapped):
    ensure_exact_role_menu(session)
    event_type=m.EventType(name='Workshop',title_patterns=[])
    if mapped:session.add(event_type);session.flush()
    windows=deepcopy(WINDOWS)
    windows[1]['time_mode']='event'
    windows[1]['event_context']={'label':'Workshop','event_type_ids':[event_type.id] if mapped else []}
    december=[date(2026,12,day).isoformat() for day in range(1,32)]
    previous={'availability_known':True,'frequency_known':True,'weekdays':[6,2],
        'all_day':False,'preferred_services':[],'max_per_month':2,'available_dates':[],
        'unavailable_dates':december,'recurring_windows':deepcopy(WINDOWS)}
    person=make_volunteer('Alex Example',prefs={'onboarding_stage':'availability','signup_minimal_texts':True,
        'interested_roles':['Greeter','Coffee'],'onboarding_availability_draft':previous})
    person.phone=PHONE
    session.add(m.Policy(key='signup_exact_copy:'+PHONE,value={'value':True}));session.flush()
    answer={**previous,'understood':True,'frequency_known':False,'max_per_month':None,
        'recurring_windows':windows,'role_frequency_caps':[{'role_id':1,'role_name':'Greeter','max_per_month':2}]}
    return person,answer

@pytest.mark.parametrize('mapped',[False,True])
def test_event_relative_scoped_frequency_and_december_complete_without_text(session,clock,provider,make_volunteer,mapped):
    person,data=fixture_answer(session,clock,make_volunteer,mapped)
    calls=[]
    def parse_only(**kwargs):
        calls.append(json.loads(kwargs['input']))
        assert 'stage' in calls[-1] and 'approved_message' not in calls[-1]
        assert 'time_mode' in kwargs['instructions'] and 'role_frequency_caps' in kwargs['instructions']
        return SimpleNamespace(output_text=json.dumps(data))
    gloo=SimpleNamespace(create_response=parse_only)
    result=onboarding.handle(session,clock,SendGate(session,clock,provider),person,
        'Synthetic workshop-time coffee, greeting twice monthly, unavailable December',gloo)
    assert result=='onboarding_complete' and not provider.sent and len(calls)==1
    prefs=person.preferences
    assert prefs['onboarding_stage']=='complete' and 'onboarding_availability_draft' not in prefs
    assert 'max_per_month' not in prefs and prefs['availability_frequency_known'] is False
    assert prefs['role_frequency_caps']==[{'role_id':1,'role_name':'Greeter','max_per_month':2}]
    assert prefs['recurring_windows'][0]['start_time']=='08:00' and prefs['recurring_windows'][0]['end_time']=='10:00'
    assert prefs['recurring_windows'][1]['time_mode']=='event' and prefs['recurring_windows'][1]['start_time'] is None
    assert session.scalar(select(m.Availability)).unavailable_dates==data['unavailable_dates']
    holds=session.scalars(select(m.Escalation).where(m.Escalation.category=='unknown_event')).all()
    assert len(holds)==(0 if mapped else 1)
    assert session.scalar(select(m.Event)) is None and session.scalar(select(m.Assignment)) is None
    assert session.scalar(select(m.Qualification)) is None


def test_essential_intake_metadata_and_quiet_recovery_output():
    from app.core.signup_delivery import intake_context
    context=intake_context(None,PHONE,'name',['first_name','last_name'])
    assert context=={'intake_fields':['name']}
    from app.core.signup_recovery import validate_reply
    recovery={'stage':'availability','missing':['window_times']}
    question='What times can you help with Coffee on Wednesday?'
    data={'stage':'availability','missing':['window_times'],'question':question,'acknowledgment':''}
    assert validate_reply(json.dumps(data),recovery,question)==question
    data['acknowledgment']='Thanks, that helps.'
    assert validate_reply(json.dumps(data),recovery,question)==question


def test_repeat_intake_stops_before_another_composition(session,clock,provider,make_volunteer):
    from app.core.signup_delivery import send_intake
    person=make_volunteer('Alex Example',prefs={'onboarding_stage':'interests'})
    gate=SendGate(session,clock,provider)
    calls=[]
    def compose():
        calls.append(True)
        return 'Which volunteer role would you like?'
    args=dict(compose=compose,purpose='signup_reply',volunteer=person,
        conversation={'intake_fields':['interests']})
    first=send_intake(session,clock,gate,**args)
    assert first.sent and len(provider.sent)==1
    for _ in range(2):
        assert send_intake(session,clock,gate,**args).status.value=='blocked_policy'
    assert len(calls)==1 and len(provider.sent)==1
