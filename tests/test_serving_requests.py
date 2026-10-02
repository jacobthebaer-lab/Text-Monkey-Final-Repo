from sqlalchemy import select
from types import SimpleNamespace

from app.config import Settings
from app.core.serving_requests import save_serving_request
from app.db import models as m
from app.llm.parser import ParsedMessage


def test_request_is_saved_once_for_review_without_role_or_assignment(session, clock, gate, make_volunteer):
    volunteer = make_volunteer()
    calls = []
    def generate(**kwargs):
        calls.append(kwargs)
        return SimpleNamespace(output_text="Your request is saved for coordinator review. You are not assigned yet. Reply STOP or HELP.")
    gloo = SimpleNamespace(settings=Settings(gloo_signup_replies=True), create_response=generate)
    args = (session, clock, gate, gloo, volunteer, "I want to lead Sunday service",
            ParsedMessage(intent="availability", dates=["2026-10-04"]), 123)
    assert save_serving_request(*args)
    assert not save_serving_request(*args)
    assert volunteer.preferences["serving_requests"][0]["message_id"] == 123
    assert len(session.scalars(select(m.Escalation)).all()) == 1
    assert len(session.scalars(select(m.Message)).all()) == 1
    assert "not assigned" in session.scalar(select(m.Message)).body
    assert len(calls) == 1
    assert not session.scalars(select(m.Assignment)).all()
    assert volunteer.is_coordinator is False
    assert volunteer.qualifications == []
