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
