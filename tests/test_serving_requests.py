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
    assert not session.scalar(select(m.Message))
    assert calls == []
    suppressed = session.scalars(select(m.Notification).where(
        m.Notification.purpose == "conversation_suppression")).all()
    assert len(suppressed) == 1 and suppressed[0].state == "blocked_policy"
    assert "essential missing signup facts" in suppressed[0].detail["reason"]
    assert not session.scalars(select(m.Assignment)).all()
    assert volunteer.is_coordinator is False
    assert volunteer.qualifications == []


def test_recorded_assignment_notice_still_composes_through_gloo(session, clock, provider, make_volunteer, make_shift, assign):
    import json
    from app.agents.fill_agent import FillContext
    from app.core.notifications import deliver
    volunteer = make_volunteer()
    assignment = assign(volunteer, make_shift())
    calls = []
    def generate(**kwargs):
        calls.append(kwargs)
        return SimpleNamespace(output_text=json.loads(kwargs["input"])["approved_message"])
    gloo = SimpleNamespace(settings=Settings(gloo_signup_replies=True), create_response=generate)
    ctx = FillContext(session, clock, provider, gloo)
    notice = deliver(ctx, key=f"scheduled-test:{assignment.id}", body="Your recorded shift is scheduled.",
        purpose="confirmation", volunteer=volunteer,
        conversation={"assignment_id":assignment.id,"notice":"scheduled"})
    assert notice.state == "sent" and len(calls) == 1 and len(provider.sent) == 1
    assert provider.sent[0].body == "Your recorded shift is scheduled."
