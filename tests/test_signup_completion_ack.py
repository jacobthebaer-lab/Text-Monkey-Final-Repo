"""Synthetic signup inputs, Gloo responses and transports; no live calls."""
from copy import deepcopy
from datetime import timedelta
import json
from types import SimpleNamespace

import pytest
from sqlalchemy import select

from app.agents.fill_agent import FillContext
from app.config import Settings
from app.core import confirmations, onboarding
from app.core.inbound import handle_inbound
from app.core.notifications import flush_due
from app.core.ordinary_reply import reply
from app.core.send_gate import SendGate
from app.db import models as m
from app.llm.gloo_client import GlooUnavailableError
from app.llm.parser import ParsedMessage
from tests.test_mac_messages import mac_app, PHONE, post  # noqa: F401

BODY = 'Sundays from 8 AM to noon, up to twice a month. I cannot help on October 18.'


class CompletionGloo:
    settings = Settings(gloo_signup_replies=False)

    def __init__(self, role_id):
        self.role_id = role_id
        self.calls = []
        self.fail_reply = False
        self.invalid_reply = False

    def create_response(self, **kwargs):
        facts = json.loads(kwargs['input'])
        self.calls.append(facts)
        if 'approved_message' in facts:
            if self.fail_reply:
                raise GlooUnavailableError('Synthetic composition outage')
            text = facts['approved_message']
            if self.invalid_reply:
                text += ' \u2014 invalid typography'
            return SimpleNamespace(output_text=text)
        return SimpleNamespace(output_text=json.dumps({'understood':True, 'availability_known':True,
            'frequency_known':True, 'weekdays':[6], 'all_day':False, 'preferred_services':[],
            'max_per_month':2, 'available_dates':[], 'unavailable_dates':['2026-10-18'],
            'recurring_windows':[{'weekday':6, 'role_ids':[self.role_id], 'role_label':'Greeter',
                'any_role':False, 'time_mode':'clock', 'start_time':'08:00', 'end_time':'12:00',
                'all_day':False, 'event_context':None}], 'pending_constraints':[]}))


def receive(session, clock, provider, person, ctx, body=BODY):
    return handle_inbound(session,clock,provider,person.phone,body,
        lambda _:ParsedMessage(intent='availability', confidence=.95, dates=['Sundays','2026-10-18'],
            partial_window='08:00-12:00'),ctx=ctx)


@pytest.fixture
def completion_case(session, clock, provider, make_volunteer, make_shift):
    shift = make_shift('Greeter')
    person = make_volunteer('Synthetic Harper', prefs={'onboarding_stage':'availability',
        'signup_minimal_texts':True, 'interested_roles':['Greeter']})
    gloo = CompletionGloo(shift.role_id)
    return person, gloo, FillContext(session,clock,provider,gloo)


@pytest.mark.parametrize('prior_hold', [False, True])
@pytest.mark.parametrize('mode', [False, True])
def test_minimal_saved_preferences_get_one_source_bound_gloo_completion(
    session, clock, provider, completion_case, mode, prior_hold
):
    person,gloo,ctx = completion_case
    session.info[confirmations.MODE_KEY] = mode
    hold = None
    if prior_hold:
        result = handle_inbound(session,clock,provider,person.phone,'Cancel my booking',
            lambda _:ParsedMessage(intent='cancel',confidence=.99),ctx=ctx)
        assert result.routed_to=='cancellation_review'
        hold = session.get(m.Notification,f'cancellation-scope:{person.id}')
        before = deepcopy(hold.detail)
    gloo.calls.clear()
    sent_before = len(provider.sent)
    result = receive(session,clock,provider,person,ctx)
    assert result.routed_to=='onboarding_complete'
    assert person.preferences['onboarding_stage']=='complete' and person.preferences['max_per_month']==2
    assert person.preferences['recurring_windows'][0]['start_time']=='08:00'
    assert person.preferences['recurring_windows'][0]['end_time']=='12:00'
    assert session.scalar(select(m.Availability)).unavailable_dates==['2026-10-18']
    incoming = session.get(m.Message,ctx.reply_to_message_id)
    notice = session.get(m.Notification,f'ordinary-reply:{incoming.id}')
    assert notice.detail['signup_completion']['step_id']
    assert notice.detail['conversation_meta']['binding']['signup_completion'] == notice.detail['signup_completion']
    assert notice.detail['conversation_meta']['binding']['thanks'] is True
    assert len(gloo.calls)==2 and 'approved_message' in gloo.calls[-1]
    assert not session.scalar(select(m.Assignment)) and not session.scalar(select(m.FillRequest))
    if mode:
        assert notice.state=='awaiting_approval' and len(provider.sent)==sent_before
        assert session.get(m.Approval,notice.detail['approval_id']).payload['body']==notice.body
    else:
        assert len(provider.sent)==sent_before+1
        assert 'preferences are saved' in provider.sent[-1].body
        assert "hasn't changed any bookings" in provider.sent[-1].body
        assert '\u2014' not in provider.sent[-1].body
    if hold:
        assert hold.state=='pending' and hold.detail==before
    # Reuse the actual original input's authority without creating another turn.
    gate=SendGate(session,clock,provider,reply_to_message_id=incoming.id)
    gate.gloo=gloo
    assert onboarding.handle(session,clock,gate,person,BODY,gloo) is None
    assert reply(session,clock,gate,person,signup_completion=notice.detail['signup_completion']) is notice
    assert len(gloo.calls)==2 and len(provider.sent)==sent_before+(not mode)


def test_outage_keeps_saved_facts_and_retries_one_completion_after_restart(
    session, clock, provider, completion_case
):
    person,gloo,ctx=completion_case
    gloo.fail_reply=True
    result=receive(session,clock,provider,person,ctx)
    assert result.routed_to=='onboarding_complete'
    notice=session.get(m.Notification,f'ordinary-reply:{ctx.reply_to_message_id}')
    assert notice.state=='pending' and notice.detail['gloo_attempts']==1
    assert person.preferences['max_per_month']==2 and not provider.sent
    assert len(gloo.calls)==2  # Interpretation succeeded, composition was held.
    session.commit();session.expire_all()
    gloo.fail_reply=False;clock.set_time(notice.due_at)
    flush_due(ctx)
    assert notice.state=='sent' and len(provider.sent)==1 and len(gloo.calls)==3
    flush_due(ctx)
    assert len(provider.sent)==1 and len(gloo.calls)==3


@pytest.mark.parametrize('change', ['phone','opt_out','source','preferences','availability','step','care'])
def test_held_completion_never_uses_changed_authority(
    session, clock, provider, completion_case, change
):
    person,gloo,ctx=completion_case
    gloo.fail_reply=True
    receive(session,clock,provider,person,ctx)
    notice=session.get(m.Notification,f'ordinary-reply:{ctx.reply_to_message_id}')
    if change=='phone':person.phone='+15555550999'
    elif change=='opt_out':person.sms_opt_in=False
    elif change=='source':session.get(m.Message,ctx.reply_to_message_id).body='Changed input'
    elif change=='preferences':person.preferences={**person.preferences,'max_per_month':1}
    elif change=='availability':session.scalar(select(m.Availability)).unavailable_dates=[]
    elif change=='step':
        step=session.get(m.AgentStep,notice.detail['signup_completion']['step_id'])
        step.result={**step.result,'incoming_message_id':999}
    else:
        session.add(m.Escalation(category='sensitive',severity='urgent',status='open',
            summary='Synthetic private care hold',related_ids={'volunteer_id':person.id},created_at=clock.now()))
    gloo.fail_reply=False;clock.set_time(notice.due_at)
    flush_due(ctx)
    assert notice.state.startswith('blocked') and not provider.sent and len(gloo.calls)==2


def test_em_dash_from_completion_composer_holds_without_template_fallback(
    session, clock, provider, completion_case
):
    person,gloo,ctx=completion_case
    gloo.invalid_reply=True
    result=receive(session,clock,provider,person,ctx)
    notice=session.get(m.Notification,f'ordinary-reply:{ctx.reply_to_message_id}')
    assert result.routed_to=='onboarding_complete'
    assert notice.state=='pending' and notice.detail['gloo_attempts']==1
    assert person.preferences['onboarding_stage']=='complete' and not provider.sent


def test_completed_ack_does_not_make_the_next_question_a_repeated_clarification(
    session, clock, provider, completion_case
):
    person,gloo,ctx=completion_case
    receive(session,clock,provider,person,ctx)
    result=handle_inbound(session,clock,provider,person.phone,'Where do I go?',
        lambda _:ParsedMessage(intent='question',confidence=.99),ctx=ctx)
    assert result.routed_to=='clarify'
    assert not session.scalar(select(m.Escalation))


def test_unrecorded_direct_call_cannot_create_completion_authority(
    session, clock, provider, completion_case
):
    person,gloo,_=completion_case
    gate=SendGate(session,clock,provider)
    assert onboarding.handle(session,clock,gate,person,BODY,gloo)=='onboarding_review'
    assert person.preferences['onboarding_stage']=='complete'
    assert not provider.sent and not session.scalar(select(m.Notification))


@pytest.mark.parametrize('change', ['unchanged', 'preferences', 'source', 'session'])
def test_native_completion_claim_retains_current_saved_facts_and_session(mac_app, change):
    from fastapi.testclient import TestClient
    from app.integrations.mac_models import MacDeliveryClaim
    clock=mac_app.state.clock
    with mac_app.state.session_factory() as session:
        person=session.scalar(select(m.Volunteer))
        person.preferences={'onboarding_stage':'availability','signup_minimal_texts':True,'interested_roles':['Greeter']}
        role=m.Role(name='Greeter',ministry='Welcome',required_qualifications=[],
            criticality='standard',fill_policy='auto')
        session.add(role);session.flush()
        gloo=CompletionGloo(role.id)
        selected=mac_app.state.provider.test_sessions[PHONE]
        session.info['mac_test_session']=selected
        ctx=FillContext(session,clock,mac_app.state.provider,gloo)
        assert receive(session,clock,mac_app.state.provider,person,ctx).routed_to=='onboarding_complete'
        outgoing=session.scalar(select(m.Message).where(m.Message.direction=='out'))
        assert outgoing.status=='queued' and len(gloo.calls)==2
        ident=outgoing.id
        if change=='preferences':person.preferences={**person.preferences,'max_per_month':1}
        elif change=='source':session.get(m.Message,ctx.reply_to_message_id).body='Changed after composition'
        elif change=='session':clock.set_time(selected.expires_at)
        session.commit()
    with TestClient(mac_app) as client:
        batch=post(client,'/mac/outbound/pull').json()['messages']
        assert bool(batch)==(change=='unchanged')
        if batch:
            assert batch[0]['id']==ident
            assert post(client,f'/mac/outbound/{ident}/verify',{'token':batch[0]['token']}).status_code==200
    with mac_app.state.session_factory() as session:
        if change!='unchanged':assert session.get(MacDeliveryClaim,ident) is None


def test_interpretation_cannot_acknowledge_a_changed_input_body(
    session, clock, provider, completion_case
):
    person,gloo,ctx=completion_case
    original=gloo.create_response
    def change_source(**kwargs):
        response=original(**kwargs)
        if 'stage' in json.loads(kwargs['input']):
            session.get(m.Message,ctx.reply_to_message_id).body='Changed while interpreting'
        return response
    gloo.create_response=change_source
    result=receive(session,clock,provider,person,ctx)
    assert result.routed_to=='onboarding_review'
    assert not provider.sent and len(gloo.calls)==1
    assert not session.scalar(select(m.Notification))
