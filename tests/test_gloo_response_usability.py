"""Provider resource failures are system holds, not sender clarification."""
import copy
import json
from types import SimpleNamespace

import httpx
import pytest
from sqlalchemy import select

from app.config import Settings
from app.db import models as m
from app.llm.agent_loop import RunLogger, run_agent
from app.llm.gloo_client import GlooClient, GlooUnavailableError
from app.llm.tools import ToolDef


@pytest.fixture(autouse=True)
def no_network(monkeypatch):
    def forbidden(*args, **kwargs):
        pytest.fail("Response usability checks must remain offline")
    monkeypatch.setattr(httpx.HTTPTransport, "handle_request", forbidden)


def response(text='{"understood":true}', **changes):
    return SimpleNamespace(status="completed", output_text=text, output=[], usage=SimpleNamespace(
        input_tokens=100, output_tokens=1024), **changes)


def client_for(*responses):
    calls, sleeps = [], []
    pending = iter(responses)
    def create(**kwargs):
        calls.append(kwargs)
        return next(pending)
    sdk = SimpleNamespace(responses=SimpleNamespace(create=create))
    return GlooClient(Settings(gloo_api_key="unused-synthetic", sms_provider="mock",
        automation_enabled=False), client=sdk, sleeper=sleeps.append), calls, sleeps


@pytest.mark.parametrize("status", ["incomplete", "failed", "queued", "in_progress", "cancelled", "unknown"])
def test_noncompleted_status_holds_even_with_valid_text_and_records_usage(status):
    reply = response()
    reply.status = status
    client, calls, sleeps = client_for(reply)
    with pytest.raises(GlooUnavailableError):
        client.create_response(model="fixture", input="fictional")
    assert len(calls) == 1 and sleeps == []
    assert client.total_usage() == {"calls":1, "input_tokens":100, "output_tokens":1024}


@pytest.mark.parametrize("field", ["error", "incomplete_details"])
def test_failure_metadata_overrides_completed_text(field):
    reply = response()
    setattr(reply, field, SimpleNamespace(reason="synthetic resource failure"))
    client, calls, sleeps = client_for(reply)
    with pytest.raises(GlooUnavailableError):
        client.create_response(model="fixture", input="fictional")
    assert len(calls) == 1 and not sleeps and client.total_usage()["calls"] == 1


@pytest.mark.parametrize("text", ["", " \t\n", None])
def test_empty_or_reasoning_only_response_is_not_usable(text):
    reply = response(text)
    reply.output = [SimpleNamespace(type="reasoning")]
    client, calls, sleeps = client_for(reply)
    with pytest.raises(GlooUnavailableError):
        client.create_response(model="fixture", input="fictional")
    assert len(calls) == 1 and not sleeps and client.total_usage()["output_tokens"] == 1024


def function_call(**changes):
    fields = dict(type="function_call", name="inspect", call_id="fixture-call",
        status="completed", arguments='{"value":1}')
    return SimpleNamespace(**{**fields, **changes})


@pytest.mark.parametrize("changes", [{"name":"unrequested"}, {"name":[]}, {"arguments":"bad JSON"},
    {"arguments":"[]"}, {"arguments":None}, {"call_id":""}, {"status":"in_progress"}])
def test_invalid_function_call_does_not_exempt_empty_response(changes):
    reply = response("")
    reply.output = [function_call(**changes)]
    client, calls, sleeps = client_for(reply)
    with pytest.raises(GlooUnavailableError):
        client.create_response(model="fixture", input="fictional",
            tools=[{"type":"function", "name":"inspect"}])
    assert len(calls) == 1 and not sleeps


def test_no_requested_tool_does_not_exempt_empty_response():
    reply = response("")
    reply.output = [function_call()]
    client, _, _ = client_for(reply)
    with pytest.raises(GlooUnavailableError):
        client.create_response(model="fixture", input="fictional")


@pytest.mark.parametrize("text", ["", "A completed message"])
@pytest.mark.parametrize("bad", [{"status":"in_progress"}, {"arguments":"bad JSON"}, {"name":"unrequested"}])
def test_mixed_function_output_never_exempts_an_invalid_call(text, bad):
    reply = response(text)
    reply.output = [function_call(), function_call(**bad)]
    client, calls, sleeps = client_for(reply)
    with pytest.raises(GlooUnavailableError):
        client.create_response(model="fixture", input="fictional",
            tools=[{"type":"function", "name":"inspect"}])
    assert len(calls)==1 and not sleeps and client.total_usage()["calls"]==1


def test_all_valid_requested_function_batch_is_usable():
    reply = response("")
    reply.output = [function_call(), function_call(call_id="second", arguments='{"value":2}')]
    client, _, _ = client_for(reply)
    assert client.create_response(model="fixture", input="fictional",
        tools=[{"type":"function", "name":"inspect"}]) is reply


def test_completed_requested_function_call_runs_actual_agent_loop(session, clock, tmp_path):
    tool_reply = response("")
    tool_reply.output = [SimpleNamespace(type="reasoning"), function_call()]
    final = response("Inspection complete")
    client, calls, sleeps = client_for(tool_reply, final)
    executions = []
    tool = ToolDef("inspect", "Read fictional fixture", {"type":"object"},
        lambda args: executions.append(args) or {"ok":True})
    log = RunLogger(session, clock, agent="fixture", trigger="Synthetic tool boundary", log_dir=tmp_path)
    outcome = run_agent(client, log, model="fixture", instructions="Read only", user_input="fictional",
        tools={"inspect":tool}, max_steps=2)
    assert outcome == {"outcome":"completed", "final_text":"Inspection complete"}
    assert executions == [{"value":1}] and len(calls) == 2 and not sleeps
    assert client.total_usage()["calls"] == 2
    assert all("max_output_tokens" not in call and "reasoning" not in call for call in calls)


def test_structured_not_understood_and_legacy_missing_status_remain_usable():
    reply = response('{"understood":false}')
    del reply.status
    client, calls, _ = client_for(reply)
    assert client.create_response(model="fixture", input="fictional") is reply
    assert len(calls) == 1


VALID_AVAILABILITY = dict(understood=True, availability_known=True, frequency_known=True,
    weekdays=[6], all_day=False, preferred_services=[], max_per_month=2,
    available_dates=[], unavailable_dates=[], recurring_windows=[dict(weekday=6,
        role_ids=[1], role_label="Greeter", any_role=False, time_mode="clock",
        start_time="09:00", end_time="10:00", all_day=False, event_context=None)])


@pytest.mark.parametrize("failure", ["empty", "whitespace", "incomplete", "failed", "in_progress"])
def test_actual_inbound_provider_failure_preserves_profile_then_valid_reply_completes_quietly(
        session, clock, provider, failure):
    from tests.test_concise_signup import PHONE, route
    from tests.test_exact_signup_copy import ExactGloo
    session.add(m.Policy(key="signup_exact_copy:"+PHONE, value={"value":True}))
    session.flush()
    setup = ExactGloo()
    for body in ("Hello", "Alex Example", "Anything"):
        route(session, clock, provider, body, setup)
    person = session.scalar(select(m.Volunteer))
    person.preferences = {**person.preferences, "onboarding_clarifications":2}
    session.flush()
    before = copy.deepcopy(person.preferences)
    permissions = (person.sms_opt_in, person.status, person.is_coordinator, person.is_pastor)
    sent_before = len(provider.sent)
    outgoing_before = list(session.scalars(select(m.Message.id).where(m.Message.direction=="out")))
    bad = response(json.dumps(VALID_AVAILABILITY))
    if failure in {"empty", "whitespace"}:
        bad.output_text = "" if failure=="empty" else " \n"
        bad.output = [SimpleNamespace(type="reasoning")]
    else:
        bad.status = failure
    good = response(json.dumps(VALID_AVAILABILITY))
    client, calls, sleeps = client_for(bad, good)
    assert route(session, clock, provider, "Sundays 9-10am, twice a month", client).routed_to=="onboarding_review"
    session.flush()
    assert person.preferences == before
    assert (person.sms_opt_in, person.status, person.is_coordinator, person.is_pastor)==permissions
    assert len(provider.sent)==sent_before
    assert list(session.scalars(select(m.Message.id).where(m.Message.direction=="out")))==outgoing_before
    error = session.scalars(select(m.Escalation).where(m.Escalation.category=="system_error")).one()
    assert error.related_ids=={"volunteer_id":person.id, "transport":"mock_or_twilio"} and error.status=="open"
    assert session.scalars(select(m.AgentRun.outcome).where(m.AgentRun.agent=="onboarding")).all()[-1]=="gloo_unavailable"
    assert len(calls)==1 and not sleeps and client.total_usage()["calls"]==1
    assert route(session, clock, provider, "Sundays 9-10am, twice a month", client).routed_to=="onboarding_complete"
    assert person.preferences["onboarding_stage"]=="complete"
    assert person.preferences["max_per_month"]==2
    assert person.preferences["recurring_windows"][0]["start_time"]=="09:00"
    assert len(provider.sent)==sent_before and len(calls)==2 and not sleeps
    assert session.scalar(select(m.Assignment)) is None and session.scalar(select(m.Qualification)) is None
