from types import SimpleNamespace

import pytest

from app.config import Settings
from app.core.signup_responder import compose_signup_reply
from app.db import models as m
from app.llm.gloo_client import GlooUnavailableError
from sqlalchemy import select


def test_live_signup_text_comes_from_gloo_and_usage_is_saved(session, clock):
    calls = []
    class Gloo:
        settings = Settings(gloo_signup_replies=True)
        def create_response(self, **kwargs):
            calls.append(kwargs)
            return SimpleNamespace(output_text="Glad you're here! Reply YES to continue. STOP to stop.", usage=SimpleNamespace(input_tokens=20, output_tokens=10))
    reply = compose_signup_reply(session, clock, Gloo(), "Reply YES to continue. STOP to stop.", ("Reply YES", "STOP"))
    assert reply.startswith("Glad you're here!")
    assert len(calls) == 1
    run = session.scalar(select(m.AgentRun))
    assert run.agent == "signup_reply" and run.input_tokens == 20 and run.output_tokens == 10


@pytest.mark.parametrize("reply", ["", "See https://example.com", "You're enrolled without consent"])
def test_invalid_gloo_reply_never_becomes_a_canned_response(session, clock, reply):
    gloo = SimpleNamespace(settings=Settings(gloo_signup_replies=True), create_response=lambda **kwargs: SimpleNamespace(output_text=reply))
    with pytest.raises(GlooUnavailableError):
        compose_signup_reply(session, clock, gloo, "Reply YES", ("Reply YES",))

@pytest.mark.parametrize('approved', [
    'Welcome to Text Monkey! What is your first and last name? Reply STOP to stop or HELP for help.',
    'Thanks, Alex! Reply YES to receive volunteer scheduling texts from Text Monkey. Message frequency varies; message/data rates may apply.',
    'What would you like to help with? Reply with names or numbers, or ANY.',
    'When can you serve, and how often?',
    'Your preferences are saved. We will send matching shift details.',
])
def test_signup_does_not_automatically_append_an_emoji(session, clock, approved):
    gloo = SimpleNamespace(settings=Settings(gloo_signup_replies=True),
        create_response=lambda **kw: SimpleNamespace(output_text=approved))
    assert compose_signup_reply(session, clock, gloo, approved, signup_conversation=True) == approved
    assert compose_signup_reply(session, clock, None, approved, signup_conversation=True) == approved


def test_plain_signup_text_can_use_the_full_length_limit(session, clock):
    calls = []
    class Gloo:
        settings = Settings(gloo_signup_replies=True)
        def create_response(self, **kwargs):
            calls.append(kwargs)
            return SimpleNamespace(output_text='A'*600)
    assert compose_signup_reply(session, clock, Gloo(), 'Approved words', signup_conversation=True) == 'A'*600
    import json
    facts=json.loads(calls[0]['input'])
    assert facts['signup_conversation'] is True and facts['product_name'] == 'Text Monkey'
    with pytest.raises(GlooUnavailableError):
        compose_signup_reply(session, clock, None, 'A'*601, signup_conversation=True)


@pytest.mark.parametrize('emoji',['🐒','🐵','🙈','🙉','🙊'])
def test_gloo_can_choose_an_occasional_monkey_without_a_forced_signoff(session,clock,emoji):
    gloo=SimpleNamespace(settings=Settings(gloo_signup_replies=True),
        create_response=lambda **kw:SimpleNamespace(output_text=f'{emoji} Welcome aboard!'))
    assert compose_signup_reply(session,clock,gloo,'Welcome aboard!',phone='+15555550101',
        signup_conversation=True)==f'{emoji} Welcome aboard!'


def test_recent_emoji_prevents_repeated_signoffs_and_next_choice_varies(session,clock):
    import json
    phone='+15555550101'
    calls=[]
    gloo=SimpleNamespace(settings=Settings(gloo_signup_replies=True),
        create_response=lambda **kw: calls.append(kw) or SimpleNamespace(output_text='Welcome! 🐵'))
    session.add(m.Message(phone=phone,direction='out',body='Hello! 🐒',status='sent',
        kind='agent',created_at=clock.now()))
    session.flush()
    assert compose_signup_reply(session,clock,gloo,'Welcome!',phone=phone,signup_conversation=True)=='Welcome!'
    assert json.loads(calls[-1]['input'])['allowed_monkey_emojis']==[]
    for body in ['Thanks!','Your preferences are saved.']:
        session.add(m.Message(phone=phone,direction='out',body=body,status='sent',
            kind='agent',created_at=clock.now()))
    session.flush()
    assert compose_signup_reply(session,clock,gloo,'Welcome!',phone=phone,signup_conversation=True)=='Welcome! 🐵'
    choices=json.loads(calls[-1]['input'])['allowed_monkey_emojis']
    assert '🐒' not in choices and '🐵' in choices


@pytest.mark.parametrize('approved', ['Your shift is cancelled.', 'A coordinator will follow up about your concern.', 'You have opted out.', 'Unable to save that change.'])
def test_shared_cancellation_care_privacy_and_error_copy_has_no_monkey_emoji(session, clock, approved):
    gloo = SimpleNamespace(settings=Settings(gloo_signup_replies=True),
        create_response=lambda **kw: SimpleNamespace(output_text=approved+' 🐵 🐒'))
    assert compose_signup_reply(session, clock, gloo, approved) == approved

@pytest.mark.parametrize('reply', ['Your shift is cancelled. 🎉', 'A coordinator will follow up about your concern. 😢'])
def test_model_emoji_does_not_leak_into_cancellation_or_care(session, clock, reply):
    gloo = SimpleNamespace(settings=Settings(gloo_signup_replies=True),
        create_response=lambda **kw: SimpleNamespace(output_text=reply))
    with pytest.raises(GlooUnavailableError):
        compose_signup_reply(session, clock, gloo, reply.rsplit(' ',1)[0])
