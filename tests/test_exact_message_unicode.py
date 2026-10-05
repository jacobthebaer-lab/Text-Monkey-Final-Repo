"""Unicode must reach the model as characters; exact output stays mandatory."""
import hashlib
import json
import re
import unicodedata
from types import SimpleNamespace

import pytest
from sqlalchemy import select

from app.config import Settings
from app.core import cloud_composition, reminders
from app.core.signup_responder import compose_signup_reply
from app.db import models as m
from app.llm.gloo_client import GlooUnavailableError

PHONE = '+12025550190'
BODY = 'Hi José 李, thanks for helping 🐒'


def compose(kind,session,clock,gloo,body,tmp_path):
    if kind=='signup':
        return compose_signup_reply(session,clock,gloo,body,phone=PHONE,
            signup_conversation=True,require_gloo=True,exact_copy=True)
    if kind=='reminder':
        return reminders.compose_exact_reminder(SimpleNamespace(session=session,
            clock=clock,gloo=gloo,log_dir=tmp_path),body)
    cloud_composition.require_composition(session,clock,gloo,PHONE,body,None)
    # The proof digest deliberately keeps its established serialization.
    expected=hashlib.sha256(json.dumps([PHONE,body,None],separators=(',',':')).encode()).hexdigest()
    assert cloud_composition.fingerprint(PHONE,body,None)==expected
    assert expected in session.info['google_voice_compositions'][1]
    return body


@pytest.mark.parametrize('kind',['signup','reminder','cloud'])
@pytest.mark.parametrize('body',[BODY,BODY+' Reply "Flexible"; keep C:\\notes unchanged.'])
def test_request_contains_real_unicode_and_json_roundtrip_is_exact(session,clock,tmp_path,kind,body):
    calls=[]
    def respond(**kwargs):
        calls.append(kwargs)
        raw=kwargs['input']
        assert 'José' in raw and '李' in raw and '🐒' in raw
        assert '\\ud83d' not in raw.lower() and '\\u00e9' not in raw.lower()
        facts=json.loads(raw)
        assert facts['approved_message']==body and facts['exact_copy'] is True
        return SimpleNamespace(output_text=facts['approved_message'])
    gloo=SimpleNamespace(settings=Settings(gloo_signup_replies=True),create_response=respond)
    assert compose(kind,session,clock,gloo,body,tmp_path)==body
    assert len(calls)==1
    assert session.scalar(select(m.Message)) is None and session.scalar(select(m.Approval)) is None


@pytest.mark.parametrize('kind',['signup','reminder','cloud'])
def test_literal_json_token_echo_does_not_turn_unicode_into_escape_text(session,clock,tmp_path,kind):
    def token_echo(**kwargs):
        # Reproduce a model echoing literal JSON escapes into plain text.
        token=re.search(r'"approved_message":\s*"([^"\n]*)"',kwargs['input']).group(1)
        return SimpleNamespace(output_text=token)
    gloo=SimpleNamespace(settings=Settings(gloo_signup_replies=True),create_response=token_echo)
    assert compose(kind,session,clock,gloo,BODY,tmp_path)==BODY


@pytest.mark.parametrize('kind',['signup','reminder','cloud'])
@pytest.mark.parametrize('output',[
    json.dumps(BODY,ensure_ascii=True)[1:-1],
    unicodedata.normalize('NFD',BODY),
    BODY+' \u2014 extra words',
])
def test_escaped_normalized_or_em_dash_output_is_still_held(session,clock,tmp_path,kind,output):
    assert output!=BODY
    gloo=SimpleNamespace(settings=Settings(gloo_signup_replies=True),
        create_response=lambda **kwargs:SimpleNamespace(output_text=output))
    with pytest.raises(GlooUnavailableError):
        compose(kind,session,clock,gloo,BODY,tmp_path)
    assert session.scalar(select(m.Message)) is None and session.scalar(select(m.Approval)) is None
    assert 'google_voice_compositions' not in session.info
