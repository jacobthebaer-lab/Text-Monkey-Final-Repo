"""User-reported conversation regressions, synthetic data and mocked Gloo only."""
import json
import sqlite3
from datetime import date, timedelta
from types import SimpleNamespace
import pytest
from sqlalchemy import select
from app.agents.fill_agent import FillContext
from app.config import Settings
from app.core.inbound import handle_inbound
from app.core import eligibility
from app.core.signup_responder import compose_signup_reply
from app.db import models as m
from app.integrations.mac_messages import MessagesReader
from app.llm.gloo_client import GlooUnavailableError
from app.llm.parser import ParsedMessage



class EchoGloo:
    settings = Settings(gloo_signup_replies=True)
    def __init__(self, extraction=None):
        self.calls = []
        self.extraction = extraction
    def create_response(self, **kwargs):
        self.calls.append(kwargs)
        data = json.loads(kwargs["input"])
        if isinstance(data, dict) and "approved_message" in data:
            return SimpleNamespace(output_text=data["approved_message"])
        if isinstance(data, dict) and "stage" in data:
            return SimpleNamespace(output_text=json.dumps(self.extraction or {
                "understood": True, "any_role": True, "role_ids": [], "weekdays": [6],
                "preferred_services": [], "max_per_month": 2, "available_dates": [], "unavailable_dates": [],
            }))
        named = any("Synthetic Newcomer" in item["body"] for item in data)
        return SimpleNamespace(output_text=json.dumps({"signup": True,
            "first_name": "Synthetic" if named else None, "last_name": "Newcomer" if named else None}))


def route(session, clock, provider, person, body, gloo=None):
    return handle_inbound(session, clock, provider, person.phone, body,
        lambda _: ParsedMessage(intent="question", confidence=1),
        ctx=FillContext(session, clock, provider, gloo) if gloo else None)


def test_initial_disclosures_then_no_footers_or_premature_rsvp(session, clock, provider):
    session.add(m.Policy(key="full_text_onboarding", value={"value": True}))
    gloo = EchoGloo()
    ctx = FillContext(session, clock, provider, gloo)
    phone = "+12025550190"
    for text in ["JOIN", "Synthetic Newcomer"]:
        handle_inbound(session, clock, provider, phone, text,
            lambda _: ParsedMessage(), ctx=ctx, allow_signup=True)
    initial = provider.sent_to(phone)
    assert "STOP" in initial[0].body and "HELP" in initial[0].body
    assert "Message frequency varies" in initial[-1].body
    assert "message/data rates may apply" in initial[-1].body
    assert "STOP" not in initial[-1].body and "HELP" not in initial[-1].body
    for text in ["YES", "ANY", "Sundays all day"]:
        handle_inbound(session, clock, provider, phone, text,
            lambda _: ParsedMessage(), ctx=ctx, allow_signup=True)
        assert "STOP" not in provider.sent_to(phone)[-1].body
        assert "HELP" not in provider.sent_to(phone)[-1].body
    completion = provider.sent_to(phone)[-1].body
    assert completion == "You’re all set, Synthetic! We’ve saved your preferences. When a shift matches, we’ll text you the details and ask if you can take it."
    assert "YES" not in completion and "NO" not in completion
    person = session.scalar(select(m.Volunteer).where(m.Volunteer.phone == phone))
    assert person.sms_opt_in and person.preferences["onboarding_stage"] == "complete"
    assert not session.scalars(select(m.Assignment)).all()


def test_stop_still_suppresses_and_start_and_help_still_work(session, clock, provider, make_volunteer):
    person = make_volunteer("Synthetic Consent", prefs={"onboarding_stage": "complete"})
    assert route(session, clock, provider, person, "STOP").routed_to == "stop"
    count = len(provider.sent)
    assert not person.sms_opt_in
    route(session, clock, provider, person, "STOP")
    route(session, clock, provider, person, "Am I booked for anything now?")
    assert len(provider.sent) == count
    assert route(session, clock, provider, person, "START").routed_to == "start"
    assert person.sms_opt_in
    assert route(session, clock, provider, person, "HELP", EchoGloo()).routed_to == "help"
    assert "coordinator" in provider.sent[-1].body and "STOP" not in provider.sent[-1].body


@pytest.mark.parametrize("stage", ["interests", "availability", "complete"])
def test_clear_repeated_booking_question_never_becomes_generic_clarification(session, clock, provider, make_volunteer, stage):
    person = make_volunteer("Synthetic Noah", prefs={"onboarding_stage": stage})
    for _ in range(2):
        result = route(session, clock, provider, person, "Am I booked for anything now?", EchoGloo())
        assert result.routed_to == "booking_status"
        assert "not booked for any shifts" in provider.sent[-1].body
        assert "could you say" not in provider.sent[-1].body
    assert person.preferences["onboarding_stage"] == stage
    assert not session.scalars(select(m.Escalation)).all()


def test_two_senders_and_followup_use_their_own_current_records(session, clock, provider, make_volunteer, make_shift, assign):
    a, b = make_volunteer("Alpha Synthetic"), make_volunteer("Beta Synthetic")
    booking = assign(a, make_shift("Greeter"), "confirmed")
    for person in [a, b]:
        assert route(session, clock, provider, person, "Am I booked for anything now?", EchoGloo()).routed_to == "booking_status"
    assert provider.sent_to(a.phone)[-1].body.startswith("Hi Alpha!") and "Greeter" in provider.sent_to(a.phone)[-1].body and "Beta" not in provider.sent_to(a.phone)[-1].body
    assert provider.sent_to(b.phone)[-1].body.startswith("Hi Beta!") and "not booked" in provider.sent_to(b.phone)[-1].body and "Greeter" not in provider.sent_to(b.phone)[-1].body
    booking.status = "cancelled"
    session.flush()
    assert route(session, clock, provider, a, "What about now?", EchoGloo()).routed_to == "booking_status"
    assert "not booked" in provider.sent_to(a.phone)[-1].body
    stranger = make_volunteer("Synthetic Gamma")
    assert route(session, clock, provider, stranger, "What about now?").routed_to == "clarify"


@pytest.mark.parametrize("state", ["in_progress", "filled"])
def test_pending_offer_is_distinct_from_booking_and_closed_offer_is_hidden(session, clock, provider, make_volunteer, make_shift, state):
    person = make_volunteer("Synthetic Offer")
    shift = make_shift("Greeter")
    fill = m.FillRequest(shift_id=shift.id, urgency="normal", state=state, created_at=clock.now())
    session.add(fill)
    msg = m.Message(phone=person.phone, volunteer_id=person.id, body="Synthetic offer", direction="out",
                    kind="template", purpose="outreach", status="sent", created_at=clock.now())
    session.add(msg)
    session.flush()
    offer = m.Outreach(fill_request_id=fill.id, volunteer_id=person.id, tranche=1, message_id=msg.id, response="none")
    session.add(offer)
    session.flush()
    assert route(session, clock, provider, person, "Am I booked for anything now?", EchoGloo()).routed_to == "booking_status"
    body = provider.sent[-1].body
    assert "not booked" in body
    if state == "in_progress":
        assert "pending offer(s), not bookings" in body and f"YES R{offer.id}" in body
    else:
        assert "YES" not in body and "pending offer" not in body
    assert not session.scalars(select(m.Assignment)).all()


def test_proposals_and_closed_events_are_not_reported_as_confirmed_bookings(session, clock, provider, make_volunteer, make_shift, assign):
    person = make_volunteer("Synthetic Proposed")
    assign(person, make_shift("Greeter"), "proposed")
    closed = make_shift("Usher")
    assign(person, closed, "confirmed")
    closed.event.status = "cancelled"
    session.flush()
    route(session, clock, provider, person, "Am I booked for anything now?", EchoGloo())
    assert "not booked" in provider.sent[-1].body and "await coordinator approval" in provider.sent[-1].body
    assert "Usher" not in provider.sent[-1].body


def test_gloo_reply_context_contains_only_exact_sender_history(session, clock, make_volunteer):
    a, b = make_volunteer("Synthetic Alpha"), make_volunteer("Synthetic Beta")
    for vol, phone, body, at in [
        (a, a.phone, "alpha-current", clock.now()),
        (b, b.phone, "beta-private", clock.now()),
        (a, b.phone, "wrong-phone", clock.now()),
        (b, a.phone, "wrong-profile", clock.now()),
        (a, a.phone, "alpha-old", clock.now()-timedelta(days=2)),
    ]:
        session.add(m.Message(volunteer_id=vol.id, phone=phone, body=body, direction="in", kind="inbound", status="received", created_at=at))
    session.flush()
    gloo = EchoGloo()
    compose_signup_reply(session, clock, gloo, "Synthetic approved facts", volunteer=a)
    data = json.loads(gloo.calls[-1]["input"])
    assert data["sender"] == {"name": a.name, "volunteer_id": a.id}
    assert [msg["body"] for msg in data["recent_messages"]] == ["alpha-current"]


def test_signup_context_does_not_mix_two_unknown_senders(session, clock, provider):
    gloo = EchoGloo()
    ctx = FillContext(session, clock, provider, gloo)
    for phone, text in [("+12025550188", "JOIN"), ("+12025550189", "JOIN"), ("+12025550188", "Synthetic Newcomer")]:
        handle_inbound(session, clock, provider, phone, text, lambda _: ParsedMessage(), ctx=ctx, allow_signup=True)
    conversations = [json.loads(call["input"]) for call in gloo.calls if isinstance(json.loads(call["input"]), list)]
    assert "Synthetic Newcomer" not in json.dumps(conversations[1])
    people = session.scalars(select(m.Volunteer)).all()
    assert len(people) == 1 and people[0].phone == "+12025550188"


@pytest.mark.parametrize("extra", ["STOP to stop or HELP for help.", "Reply YES or NO."])
def test_model_cannot_reintroduce_completion_footer_or_premature_rsvp(session, clock, make_volunteer, extra):
    person = make_volunteer("Synthetic Completion")
    approved = "Your preferences are saved. We will send matching shift details."
    gloo = SimpleNamespace(settings=Settings(gloo_signup_replies=True),
        create_response=lambda **_: SimpleNamespace(output_text=approved+" "+extra))
    if extra.startswith("STOP"):
        assert compose_signup_reply(session, clock, gloo, approved, volunteer=person) == approved
    else:
        with pytest.raises(GlooUnavailableError):
            compose_signup_reply(session, clock, gloo, approved, volunteer=person)


def test_screenshot_availability_stores_every_exclusion_and_checks_eligibility(session, clock, provider, make_volunteer, make_shift):
    person = make_volunteer("Noah Synthetic", prefs={"onboarding_stage": "availability"})
    january = [date(2027, 1, d).isoformat() for d in range(1, 32)]
    data = {"understood": True, "weekdays": [6, 2, 3], "preferred_services": [], "max_per_month": 2,
            "available_dates": [], "unavailable_dates": ["2026-10-04", *january]}
    gloo = EchoGloo(data)
    text = "Sundays I’m free all day except next Sunday, free on Wednesdays and Thursdays as well. Not available in January"
    result = route(session, clock, provider, person, text, gloo)
    assert result.routed_to == "onboarding_complete"
    assert provider.sent[-1].body == "You’re all set, Noah! We’ve saved your preferences. When a shift matches, we’ll text you the details and ask if you can take it."
    assert person.preferences["availability_weekdays"] == [6, 2, 3]
    assert person.preferences["preferred_services"] == []
    rows = session.scalars(select(m.Availability).where(m.Availability.volunteer_id == person.id)).all()
    assert set(d for row in rows for d in row.unavailable_dates) == {"2026-10-04", *january}
    for days, eligible in [(3, False), (6, True), (7, True), (8, False), (10, True), (101, False)]:
        shift = make_shift(starts=clock.now()+timedelta(days=days))
        assert eligibility.check(session, person, shift).eligible is eligible
    extraction = next(json.loads(c["input"]) for c in gloo.calls if isinstance(json.loads(c["input"]), dict) and "stage" in json.loads(c["input"]))
    assert extraction["today"] == "2026-10-01" and extraction["body"] == text


def test_mac_reader_cannot_distinguish_personal_inbound_on_same_test_line(tmp_path):
    path = tmp_path / "synthetic-privacy.db"
    with sqlite3.connect(path) as db:
        db.executescript("""
            CREATE TABLE message(guid TEXT, handle_id INTEGER, text TEXT, attributedBody BLOB, is_from_me INTEGER, service TEXT, destination_caller_id TEXT);
            CREATE TABLE handle(id TEXT);
            CREATE TABLE chat(service_name TEXT, guid TEXT, last_addressed_handle TEXT);
            CREATE TABLE chat_message_join(message_id INTEGER, chat_id INTEGER);
            CREATE TABLE chat_handle_join(chat_id INTEGER, handle_id INTEGER);
            INSERT INTO handle VALUES ('+12025550190'), ('+12025550191');
            INSERT INTO chat VALUES ('iMessage','test-chat','+12025550192'), ('iMessage','other-line','+12025550193'), ('iMessage','unrelated','+12025550192');
            INSERT INTO chat_handle_join VALUES (1,1),(2,1),(3,2);
            INSERT INTO message VALUES
              ('app-test',1,'Synthetic app reply',NULL,0,'iMessage','+12025550192'),
              ('personal-same-line',1,'Synthetic personal reply',NULL,0,'iMessage','+12025550192'),
              ('personal-outbound',1,'Synthetic operator text',NULL,1,'iMessage','+12025550192'),
              ('personal-other-line',1,'Synthetic excluded line',NULL,0,'iMessage','+12025550193'),
              ('unrelated',2,'Synthetic unrelated sender',NULL,0,'iMessage','+12025550192');
            INSERT INTO chat_message_join VALUES (1,1),(2,1),(3,1),(4,2),(5,3);
        """)
    reader = MessagesReader(path, {"+12025550190"}, tmp_path / "unused", "+12025550192")
    try:
        # Characterize the privacy blocker honestly; this is not an isolation pass.
        assert [r["guid"] for r in reader.new_messages(0)] == ["app-test", "personal-same-line"]
    finally:
        reader.connection.close()
