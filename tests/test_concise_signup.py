"""Latest user copy and fewer texts; fictional sender, Gloo stub and mock transport."""
import json
import re
from types import SimpleNamespace
import pytest
from sqlalchemy import select

from app.agents.fill_agent import FillContext
from app.config import Settings
from app.core.inbound import handle_inbound
from app.core.signup_responder import compose_signup_reply, EMOJI_PATTERN, LIGHT_EMOJIS
from app.core.signup import finish_signup
from app.db import models as m
from app.llm.gloo_client import GlooUnavailableError
from app.llm.parser import ParsedMessage

PHONE = '+12025550190'


class ConciseGloo:
    settings = Settings(gloo_signup_replies=True)
    def __init__(self):
        self.calls = []
    def create_response(self, **kwargs):
        facts = json.loads(kwargs['input']); self.calls.append(facts)
        if isinstance(facts, dict) and 'approved_message' in facts:
            emoji = '🐵' if facts['approved_message'].startswith('Welcome') else '🙌'
            suffix = ' '+emoji if emoji in facts['allowed_emojis'] else ''
            return SimpleNamespace(output_text=facts['approved_message']+suffix)
        if isinstance(facts, list):
            body = facts[-1]['body']
            return SimpleNamespace(output_text=json.dumps({'signup':body!='Hello',
                'first_name':'Alex','last_name':'Example'}))
        if facts['stage']=='interests':
            data={'understood':True,'any_role':True,'role_ids':[]}
        else:
            data={'understood':True,'availability_known':True,'frequency_known':False,
                'weekdays':[6,2],'all_day':True,'preferred_services':[], 'max_per_month':None,
                'available_dates':[], 'unavailable_dates':['2026-10-18']}
        return SimpleNamespace(output_text=json.dumps(data))


def route(session, clock, provider, body, gloo):
    return handle_inbound(session, clock, provider, PHONE, body,
        lambda _: ParsedMessage(intent='question',confidence=1),
        ctx=FillContext(session,clock,provider,gloo),allow_signup=True)


@pytest.mark.parametrize('identity_reply',['Alex Example YES','YES Alex Example','JOIN Alex Example, YES'])
def test_complete_signup_uses_four_outgoing_texts_without_inventing_frequency(session,clock,provider,identity_reply):
    session.add(m.Policy(key='full_text_onboarding',value={'value':True}))
    gloo=ConciseGloo()
    assert route(session,clock,provider,'Hello',gloo).routed_to=='signup_invitation'
    assert route(session,clock,provider,identity_reply,gloo).routed_to=='onboarding_interests'
    assert route(session,clock,provider,'Anything',gloo).routed_to=='onboarding_availability'
    assert route(session,clock,provider,'Sundays and Wednesdays all day, unavailable October 18',gloo).routed_to=='onboarding_complete'
    assert len(provider.sent)==4
    assert 'YES' in provider.sent[0].body and 'Message frequency varies' in provider.sent[0].body
    assert 'message/data rates may apply' in provider.sent[0].body
    assert 'STOP' in provider.sent[0].body and 'HELP' in provider.sent[0].body
    assert all('STOP' not in message.body and 'HELP' not in message.body for message in provider.sent[1:])
    assert not any('Reply YES to receive' in message.body for message in provider.sent[1:])
    assert sum(len(EMOJI_PATTERN.findall(message.body)) for message in provider.sent)==2
    assert all(len(EMOJI_PATTERN.findall(message.body))<=1 for message in provider.sent)
    person=session.scalar(select(m.Volunteer))
    assert person.sms_opt_in and person.preferences['consent_source']=='sms_name_and_yes'
    assert person.preferences['availability_weekdays']==[6,2] and person.preferences['availability_all_day']
    assert person.preferences['availability_frequency_known'] is False
    assert 'max_per_month' not in person.preferences
    assert person.preferences['onboarding_stage']=='complete'
    assert session.scalar(select(m.Assignment)) is None
    assert session.scalar(select(m.Qualification)) is None
    assert session.scalar(select(m.Availability)).unavailable_dates==['2026-10-18']


@pytest.mark.parametrize('body',['Alex Example','JOIN Alex Example','My name is Alex Example','Alex Example said YES yesterday'])
def test_a_name_or_incidental_yes_never_becomes_affirmative_consent(session,clock,provider,body):
    gloo=ConciseGloo()
    assert route(session,clock,provider,body,gloo).routed_to=='signup_consent_pending'
    person=session.scalar(select(m.Volunteer))
    assert not person.sms_opt_in and person.status=='inactive'
    assert 'Reply YES' in provider.sent[-1].body


def test_stop_still_blocks_combined_signup_and_more_messages(session,clock,provider):
    gloo=ConciseGloo()
    route(session,clock,provider,'STOP',gloo)
    assert route(session,clock,provider,'Alex Example YES',gloo).routed_to=='stop'
    assert session.scalar(select(m.Volunteer)) is None and not provider.sent


def test_gloo_outage_never_replaces_the_consolidated_intro_with_a_template(session,clock,provider):
    gloo=ConciseGloo()
    original=gloo.create_response
    def unavailable(**kwargs):
        facts=json.loads(kwargs['input'])
        if isinstance(facts,dict) and 'approved_message' in facts: raise GlooUnavailableError('Synthetic outage')
        return original(**kwargs)
    gloo.create_response=unavailable
    with pytest.raises(GlooUnavailableError):route(session,clock,provider,'Hello',gloo)
    assert not provider.sent


@pytest.mark.parametrize('emoji',LIGHT_EMOJIS)
def test_light_emoji_is_optional_and_only_one_is_kept(session,clock,emoji):
    gloo=SimpleNamespace(settings=Settings(gloo_signup_replies=True),
        create_response=lambda **kwargs:SimpleNamespace(output_text=f'Thanks! {emoji} {emoji}'))
    reply=compose_signup_reply(session,clock,gloo,'Thanks!',phone=PHONE,signup_conversation=True)
    assert reply==f'Thanks! {emoji}'


def test_other_emoji_also_suppresses_decoration_in_the_next_two_texts(session,clock):
    session.add(m.Message(phone=PHONE,direction='out',body='Thanks! 🙌',kind='ai',status='sent',created_at=clock.now()))
    session.flush()
    gloo=SimpleNamespace(settings=Settings(gloo_signup_replies=True),
        create_response=lambda **kwargs:SimpleNamespace(output_text='Welcome! 🐵'))
    assert compose_signup_reply(session,clock,gloo,'Welcome!',phone=PHONE,signup_conversation=True)=='Welcome!'


@pytest.mark.parametrize('body',['YES','HELP','not sure'])
@pytest.mark.parametrize('failure',['missing','disabled_setting'])
def test_legacy_finish_and_consent_followups_also_require_gloo(session,clock,gate,provider,make_volunteer,body,failure):
    person=make_volunteer(opt_in=False,status='inactive',prefs={'consent_pending':True})
    def unavailable(**kwargs):raise GlooUnavailableError('Synthetic outage')
    gloo=None if failure=='missing' else SimpleNamespace(settings=Settings(gloo_signup_replies=False),create_response=unavailable)
    with pytest.raises(GlooUnavailableError):finish_signup(session,clock,gate,person,body,gloo=gloo)
    assert not provider.sent and session.scalar(select(m.Message)) is None
