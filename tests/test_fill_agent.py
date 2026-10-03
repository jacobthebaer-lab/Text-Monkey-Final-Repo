"""Fill agent end to end on the fake clock, with a scripted fake Gloo.

Covers silent cancellations, source-backed historical replies, one-slot
acceptance, private care tasks and restricted-role outreach suppression.
"""

import json
from datetime import timedelta
from types import SimpleNamespace

import pytest
from sqlalchemy import select

from app import jobs
from app.agents import fill_agent
from app.agents.fill_agent import FillContext, compute_urgency, tranche_plan
from app.core.inbound import handle_inbound
from app.db import models as m
from app.llm.gloo_client import GlooUnavailableError
from app.llm.parser import ParsedMessage
from tests.conftest import NOW


def usage(i=50, o=25):
    return SimpleNamespace(input_tokens=i, output_tokens=o)


class ScriptedAgentGloo:
    """Composes approved notification facts or runs the scripted fill tool loop."""

    def create_response(self, *, model, input, instructions=None, tools=None, **kwargs):
        if isinstance(input, str):
            facts = json.loads(input)
            return SimpleNamespace(
                output=[SimpleNamespace(type="message")],
                output_text=facts["approved_message"],
                usage=usage(20, 5),
            )
        answered = any(
            isinstance(i, dict) and i.get("type") == "function_call_output"
            for i in input
        )
        if answered:
            return SimpleNamespace(
                output=[SimpleNamespace(type="message")],
                output_text="Asks sent.",
                usage=usage(20, 5),
            )
        payload = json.loads(input[0]["content"])
        calls = [
            SimpleNamespace(
                type="function_call",
                call_id=f"c{i}",
                name="request_send_text",
                arguments=json.dumps(
                    {
                        "volunteer_id": member["volunteer_id"],
                        "body": f"Hi {member['name'].split()[0]}! Could you cover {payload['shift']['role']} "
                        f"on {payload['shift']['starts_at'][:10]}? No worries if not, reply YES or NO.",
                    }
                ),
            )
            for i, member in enumerate(
                payload["candidates"][: payload["max_candidates"]]
            )
        ]
        calls.insert(
            0,
            SimpleNamespace(
                type="function_call",
                call_id="choose",
                name="choose_replacements",
                arguments=json.dumps(
                    {
                        "volunteer_ids": [
                            c["volunteer_id"]
                            for c in payload["candidates"][: payload["max_candidates"]]
                        ],
                        "reason": "These volunteers match the available shift.",
                    }
                ),
            ),
        )
        calls.append(
            SimpleNamespace(
                type="function_call",
                call_id="sched",
                name="schedule_next_tranche",
                arguments="{}",
            )
        )
        return SimpleNamespace(output=calls, output_text=None, usage=usage())


class FailingGloo:
    def create_response(self, **kwargs):
        raise GlooUnavailableError("gloo is down")


class LoopingGloo:
    def create_response(self, *, input, **kwargs):
        call = SimpleNamespace(type="function_call", call_id=f"x{len(input)}", name="get_shift_context", arguments='{"shift_id": 1}')
        return SimpleNamespace(output=[call], output_text=None, usage=usage(5, 5))


def parser_returning(**kwargs):
    defaults = dict(intent="other", confidence=0.95)
    defaults.update(kwargs)
    return lambda text: ParsedMessage(**defaults)


@pytest.fixture
def coordinator(make_volunteer):
    return make_volunteer("Coordinator Kim", coordinator=True)


@pytest.fixture
def pastor(make_volunteer):
    return make_volunteer("Pastor Lee", pastor=True)


@pytest.fixture
def ctx_factory(session, clock, provider, tmp_path):
    def _make(gloo=None):
        return FillContext(session, clock, provider, gloo or ScriptedAgentGloo(), log_dir=tmp_path)

    return _make


def outreach_rows(session, tranche=None):
    query = select(m.Outreach)
    if tranche is not None:
        query = query.where(m.Outreach.tranche == tranche)
    return session.scalars(query).all()


def historical_invitation(session, clock, volunteer, fill, *, provider=None):
    """Seed synthetic evidence of a pre-policy delivered offer, never a new send.

    Preparation and dispatch still enforce source snapshots and reply deadlines.
    Native fixtures carry the actual selected test-session prefix and submission.
    """
    from app.core import offer_windows as offers
    fill.state = "in_progress"
    fill.current_tranche = max(1, fill.current_tranche)
    row = m.Outreach(fill_request_id=fill.id, volunteer_id=volunteer.id, tranche=fill.current_tranche)
    session.add(row); session.flush()
    meta = offers.prepare(session, row, "Historical synthetic invitation.", clock.now())
    assert meta is not None
    selected = getattr(provider, 'test_sessions', {}).get(volunteer.phone)
    message = m.Message(direction="out", volunteer_id=volunteer.id, phone=volunteer.phone,
        body=meta.body, purpose="outreach", kind="ai", status="submitted" if selected else "sent",
        provider_sid=selected.outbound_prefix+f"historical-{row.id}" if selected else "MOCK-HISTORY",
        created_at=clock.now())
    session.add(message); session.flush(); row.message_id = message.id
    assert offers.dispatch(session, row, message, clock.now()) is None
    session.flush()
    return row


def historical_consent(session, clock, volunteer):
    """A delivered disclosure followed by this sender's actual name response."""
    from app.core.signup_copy import WELCOME
    when = clock.now()-timedelta(minutes=2)
    session.add(m.Message(direction="out", volunteer_id=volunteer.id, phone=volunteer.phone,
                         body=WELCOME, purpose="signup_reply", kind="ai", status="sent", created_at=when))
    session.flush()
    session.add(m.Message(direction="in", volunteer_id=volunteer.id, phone=volunteer.phone,
                         body=volunteer.name, kind="inbound", status="received", created_at=when+timedelta(seconds=1)))
    volunteer.preferences = {**volunteer.preferences, 'consent_source': 'sms_name_reply_to_exact_invitation',
                             'consent_at': (when+timedelta(seconds=1)).isoformat()}
    session.flush()


def test_demo_critical_scenario(session, clock, provider, make_volunteer, make_shift, assign, coordinator, pastor, ctx_factory):
    """Silent cancellation, historical expiry and acceptance keep one winner."""
    ctx = ctx_factory()
    shift = make_shift("greeter")
    zoe = make_volunteer("Zoe Adams")
    assignment = assign(zoe, shift, status="approved")
    candidates = [make_volunteer(f"Candidate {c}") for c in "ABCDEFGHIJ"]
    result = handle_inbound(session, clock, provider, zoe.phone, "cant make it sunday sorry!!",
                            parser_returning(intent="cancel", confidence=0.9), ctx=ctx)
    assert result.routed_to == "fill_agent" and assignment.status == "cancelled"
    fill = session.scalar(select(m.FillRequest))
    assert fill.state == "in_progress" and fill.urgency == "normal" and fill.current_tranche == 1
    assert not provider.sent_to(zoe.phone)
    assert all(o.message_id is None for o in outreach_rows(session))
    assert not session.scalars(select(m.Approval)).all()
    first = historical_invitation(session, clock, candidates[0], fill)
    clock.set_time(fill.next_action_at)
    outcomes = jobs.process_due_fill_requests(ctx)
    assert [o.action for o in outcomes] == ["offer_blocked"]
    assert first.response == "expired" and fill.current_tranche == 2
    winner = candidates[1]
    historical_invitation(session, clock, winner, fill)
    handle_inbound(session, clock, provider, winner.phone, "yes!!",
                   parser_returning(intent="accept", confidence=0.97), ctx=ctx)
    assert fill.state == "filled" and fill.next_action_at is None
    active = session.scalars(select(m.Assignment).where(m.Assignment.shift_id == shift.id,
                                  m.Assignment.status == "confirmed")).all()
    assert len(active) == 1 and active[0].volunteer_id == winner.id and active[0].source == "fill"
    assert len(provider.sent_to(winner.phone)) == 1
    assert "confirmed" in provider.sent_to(winner.phone)[0].body
    assert not provider.sent_to(candidates[0].phone)
    clock.advance(timedelta(minutes=5)); jobs.process_due_fill_requests(ctx)
    assert any("covered" in text.body for text in provider.sent_to(coordinator.phone))
    handle_inbound(session, clock, provider, candidates[0].phone, "Y", parser_returning(intent="accept"), ctx=ctx)
    assert session.scalar(select(m.Assignment).where(m.Assignment.volunteer_id == candidates[0].id)) is None
    assert not provider.sent_to(candidates[0].phone)
    runs = session.scalars(select(m.AgentRun)).all()
    assert runs and any(run.input_tokens > 0 for run in runs)
    assert session.scalar(select(m.AgentStep).where(m.AgentStep.type == "tool_call",
                                                   m.AgentStep.tool_name == "request_send_text")) is not None


def test_sensitive_cancellation_escalates_and_fill_proceeds_silently(
    session, clock, provider, make_volunteer, make_shift, assign, coordinator, pastor, ctx_factory
):
    ctx = ctx_factory()
    shift = make_shift("usher")
    sam = make_volunteer("Sam Field")
    assign(sam, shift, status="approved")
    [make_volunteer(f"Helper {c}") for c in "ABC"]

    parser = parser_returning(intent="cancel", confidence=0.9, sensitive=True, severity="urgent")
    handle_inbound(session, clock, provider, sam.phone, "my dad was taken to the ER, can't come", parser, ctx=ctx)

    escalation = session.scalar(select(m.Escalation).where(m.Escalation.category == "sensitive"))
    assert escalation.severity == "urgent" and escalation.assigned_to == pastor.id
    assert provider.sent_to(sam.phone) == []  # no ack, no anything
    fill = session.scalar(select(m.FillRequest))
    assert fill.state == "in_progress"  # the shift still gets filled
    assert len(outreach_rows(session, tranche=1)) == 1


def test_kids_role_does_not_invent_outreach_approval(
    session, clock, provider, make_volunteer, make_shift, assign, coordinator, pastor, ctx_factory
):
    ctx = ctx_factory()
    quals = [("background_check", "verified", None), ("child_safety_training", "verified", None)]
    shift = make_shift("nursery", required=("background_check", "child_safety_training"),
                       criticality="critical", fill_policy="needs_approval")
    cara = make_volunteer("Cara Jones", quals=quals)
    assign(cara, shift, status="approved")
    helpers = [make_volunteer(f"Kids Helper {c}", quals=quals) for c in "ABCD"]

    handle_inbound(session, clock, provider, cara.phone, "cant do sunday",
                   parser_returning(intent="cancel", confidence=0.9), ctx=ctx)

    fill = session.scalar(select(m.FillRequest))
    assert fill.state == "in_progress" and fill.urgency == "critical"
    assert not session.scalars(select(m.Approval)).all()
    assert all(o.message_id is None for o in outreach_rows(session))
    assert all(not provider.sent_to(helper.phone) for helper in helpers)
    # A coordinator YES cannot create authority where no review was staged.
    result = handle_inbound(session, clock, provider, coordinator.phone, "YES", parser_returning(), ctx=ctx)
    assert result.routed_to == "admin_agent"
    assert not session.scalars(select(m.Assignment).where(m.Assignment.status == "confirmed")).all()
    assert all(not provider.sent_to(helper.phone) for helper in helpers)
    clock.advance(timedelta(minutes=5)); jobs.process_due_fill_requests(ctx)
    assert provider.sent_to(coordinator.phone)  # internal staffing status remains supported


def test_ambiguous_shift_requires_delivered_source_for_numbered_reply(
    session, clock, provider, make_volunteer, make_shift, assign, coordinator, ctx_factory
):
    ctx = ctx_factory()
    vol = make_volunteer("Busy Bee")
    first = make_shift("usher")
    second = make_shift("greeter", starts=NOW + timedelta(days=10))
    assign(vol, first, status="approved")
    assign(vol, second, status="approved")
    make_volunteer("Cover Person")

    handle_inbound(session, clock, provider, vol.phone, "I can't make it",
                   parser_returning(intent="cancel", confidence=0.9, shift_hint=None), ctx=ctx)
    assert not provider.sent_to(vol.phone)
    assert session.scalar(select(m.FillRequest)) is None  # nothing cancelled yet

    handle_inbound(session, clock, provider, vol.phone, "2", parser_returning(), ctx=ctx)
    assert all(a.status == "approved" for a in session.scalars(select(m.Assignment)))
    # A genuinely delivered historical clarification can scope a later number.
    session.add(m.Message(direction="out", volunteer_id=vol.id, phone=vol.phone,
        body="Which shift: 1) usher, 2) greeter?", purpose="clarify_shift", kind="ai", status="sent", created_at=clock.now()))
    session.flush()
    handle_inbound(session, clock, provider, vol.phone, "2", parser_returning(), ctx=ctx)
    assignments = session.scalars(select(m.Assignment).where(m.Assignment.volunteer_id == vol.id)).all()
    by_shift = {a.shift_id: a.status for a in assignments}
    assert by_shift[second.id] == "cancelled"
    assert by_shift[first.id] == "approved"
    assert session.scalar(select(m.FillRequest)).shift_id == second.id


def test_no_candidates_escalates_to_coordinator(
    session, clock, provider, make_volunteer, make_shift, assign, coordinator, ctx_factory
):
    ctx = ctx_factory()
    shift = make_shift("usher")
    only = make_volunteer("Only One")
    assign(only, shift, status="approved")

    handle_inbound(session, clock, provider, only.phone, "cant come",
                   parser_returning(intent="cancel", confidence=0.9), ctx=ctx)

    fill = session.scalar(select(m.FillRequest))
    assert fill.state == "escalated"
    escalation = session.scalar(select(m.Escalation).where(m.Escalation.category == "unfillable"))
    assert escalation is not None
    clock.advance(timedelta(minutes=5))
    jobs.process_due_fill_requests(ctx)
    assert any("Still needs cover" in s.body and "need your help" in s.body for s in provider.sent_to(coordinator.phone))


def test_gloo_failure_escalates_never_guesses(
    session, clock, provider, make_volunteer, make_shift, assign, coordinator, ctx_factory
):
    ctx = ctx_factory(gloo=FailingGloo())
    shift = make_shift("usher")
    vol = make_volunteer("Volunteer V")
    assign(vol, shift, status="approved")
    helper = make_volunteer("Helper H")

    handle_inbound(session, clock, provider, vol.phone, "cant come",
                   parser_returning(intent="cancel", confidence=0.9), ctx=ctx)

    fill = session.scalar(select(m.FillRequest))
    assert fill.state == "escalated"
    assert session.scalar(select(m.Escalation).where(m.Escalation.category == "system_error")) is not None
    assert provider.sent_to(helper.phone) == []  # nothing guessed, nothing sent


def test_max_steps_escalates(session, clock, provider, make_volunteer, make_shift, assign, coordinator, ctx_factory):
    ctx = ctx_factory(gloo=LoopingGloo())
    shift = make_shift("usher")
    vol = make_volunteer("Volunteer W")
    assign(vol, shift, status="approved")
    make_volunteer("Helper I")

    fill_agent.handle_cancellation(ctx, vol)
    assert session.scalar(select(m.FillRequest)).state == "escalated"


def test_ineligible_historical_yes_is_recorded_silently_without_assignment(
    session, clock, provider, make_volunteer, make_shift, assign, coordinator, ctx_factory
):
    ctx = ctx_factory()
    shift = make_shift("usher")
    vol = make_volunteer("Cancelling C")
    assign(vol, shift, status="approved")
    eager = make_volunteer("Eager E")
    make_volunteer("Other O")

    handle_inbound(session, clock, provider, vol.phone, "cant come",
                   parser_returning(intent="cancel", confidence=0.9), ctx=ctx)
    fill = session.scalar(select(m.FillRequest))
    offer = historical_invitation(session, clock, eager, fill)
    # Eager gets double-booked before replying yes.
    conflict = make_shift("greeter")
    assign(eager, conflict, status="confirmed")

    handle_inbound(session, clock, provider, eager.phone, "yes!!",
                   parser_returning(intent="accept", confidence=0.95), ctx=ctx)

    assert session.scalar(
        select(m.Assignment).where(m.Assignment.volunteer_id == eager.id, m.Assignment.shift_id == shift.id)
    ) is None
    assert offer.response == "ineligible" and not provider.sent_to(eager.phone)
    assert session.scalar(select(m.FillRequest)).state == "in_progress"  # still looking


def test_tranches_exhaust_then_escalate(
    session, clock, provider, make_volunteer, make_shift, assign, coordinator, ctx_factory
):
    ctx = ctx_factory()
    shift = make_shift("usher", starts=NOW + timedelta(hours=8))  # 2-12h tier: 20m waits
    vol = make_volunteer("Short Notice")
    assign(vol, shift, status="approved")
    [make_volunteer(f"Sub {c}") for c in "ABCD"]  # 4 candidates: T1=3, T2=1, T3 empty

    handle_inbound(session, clock, provider, vol.phone, "cant come today",
                   parser_returning(intent="cancel", confidence=0.9), ctx=ctx)
    fill = session.scalar(select(m.FillRequest))
    assert fill.urgency == "high" and fill.current_tranche == 1

    clock.set_time(fill.next_action_at)
    jobs.process_due_fill_requests(ctx)
    assert fill.current_tranche == 2 and len(outreach_rows(session, tranche=2)) == 1

    for _ in range(3):
        clock.set_time(fill.next_action_at)
        jobs.process_due_fill_requests(ctx)
    assert fill.state == "escalated"


def test_declines_advance_early(session, clock, provider, make_volunteer, make_shift, assign, coordinator, ctx_factory):
    ctx = ctx_factory()
    shift = make_shift("usher")
    vol = make_volunteer("Cancelling D")
    assign(vol, shift, status="approved")
    subs = [make_volunteer(f"Decliner {c}") for c in "ABCDEF"]

    handle_inbound(session, clock, provider, vol.phone, "cant come",
                   parser_returning(intent="cancel", confidence=0.9), ctx=ctx)
    fill = session.scalar(select(m.FillRequest))
    tranche1 = outreach_rows(session, tranche=1)
    assert len(tranche1) == 1

    historic = historical_invitation(session, clock, session.get(m.Volunteer, tranche1[0].volunteer_id), fill)
    member = session.get(m.Volunteer, historic.volunteer_id)
    handle_inbound(session, clock, provider, member.phone, "no sorry",
                   parser_returning(intent="decline", confidence=0.95), ctx=ctx)
    assert historic.response == "no" and historic.responded_at == clock.now()

    assert fill.current_tranche == 2
    assert len(outreach_rows(session, tranche=2)) == 1  # the remaining subs


def test_compute_urgency_and_tranche_plan(session, make_volunteer, make_shift, assign):
    critical = make_shift("sound", criticality="critical", starts=NOW + timedelta(days=5))
    assert compute_urgency(session, critical, NOW) == "critical"  # 0 assigned < min 1
    soon = make_shift("usher", starts=NOW + timedelta(hours=10))
    assert compute_urgency(session, soon, NOW) == "high"
    later = make_shift("parking", starts=NOW + timedelta(days=5))
    assert compute_urgency(session, later, NOW) == "normal"
    covered = make_shift("coffee", criticality="optional")
    helper = make_volunteer()
    assign(helper, covered, status="approved")
    assert compute_urgency(session, covered, NOW) == "skip"

    assert all(tranche_plan(hours).sizes == (1,) for hours in (72, 24, 5, 1))



def test_gloo_can_choose_lower_scored_replacement(
    session,
    clock,
    provider,
    make_volunteer,
    make_shift,
    assign,
    coordinator,
    ctx_factory,
):
    class ChoosingGloo(ScriptedAgentGloo):
        def create_response(self, *, input, **kwargs):
            if isinstance(input, str):
                return super().create_response(input=input, **kwargs)
            payload = json.loads(input[0]["content"])
            # This scripted model deliberately prefers the last candidate,
            # proving fixed scores and top-three slices no longer decide.
            payload["candidates"] = payload["candidates"][-1:]
            changed = [{**input[0], "content": json.dumps(payload)}, *input[1:]]
            return super().create_response(input=changed, **kwargs)

    shift = make_shift("usher")
    cancelled = make_volunteer("Cancel Person")
    assign(cancelled, shift)
    top = make_volunteer("Fixed Score Favorite", prefs={"interested_roles": ["usher"]})
    other = make_volunteer("Model Choice")
    fill_agent.handle_cancellation(ctx_factory(ChoosingGloo()), cancelled)
    rows = outreach_rows(session)
    assert [o.volunteer_id for o in rows] == [other.id]
    assert not provider.sent_to(other.phone) and not provider.sent_to(top.phone)
    assert rows[0].message_id is None and rows[0].response == "blocked"
    assert session.scalar(
        select(m.AgentStep).where(
            m.AgentStep.tool_name == "choose_replacements",
            m.AgentStep.type == "tool_result",
        )
    ).result["reason"]


def test_gloo_selection_rejects_unqualified_duplicates_and_oversized_batches(
    session, clock, gate, make_volunteer, make_shift
):
    from app.llm.tools import fill_agent_tools

    shift = make_shift("sound", required=("sound_training",))
    invalid = make_volunteer("Unqualified")
    qualified = make_volunteer("Trained", quals=[("sound_training", "verified", None)])
    other = make_volunteer("Also Trained", quals=[("sound_training", "verified", None)])
    fill = m.FillRequest(
        shift_id=shift.id,
        urgency="normal",
        state="in_progress",
        current_tranche=1,
        created_at=clock.now(),
    )
    session.add(fill)
    session.flush()
    tools = fill_agent_tools(session, clock, gate, fill, max_candidates=1)
    choose = tools["choose_replacements"].handler
    for ids in ([invalid.id], [qualified.id, qualified.id], [qualified.id, other.id]):
        assert "error" in choose({"volunteer_ids": ids, "reason": "A model choice"})
        assert not outreach_rows(session)
    assert (
        choose({"volunteer_ids": [qualified.id], "reason": "Current sound training"})[
            "status"
        ]
        == "chosen"
    )
    assert "error" in choose(
        {"volunteer_ids": [other.id], "reason": "Try another batch"}
    )
    assert "assign_volunteer" not in tools and "cancel_assignment" not in tools


def test_model_finishing_without_selection_escalates(
    session, make_volunteer, make_shift, assign, coordinator, ctx_factory
):
    class NoChoiceGloo:
        def create_response(self, **kwargs):
            return SimpleNamespace(output=[], output_text="Done", usage=usage())

    shift = make_shift("usher")
    vol = make_volunteer("Cancel Person")
    assign(vol, shift)
    make_volunteer("Possible Helper")
    outcome = fill_agent.handle_cancellation(ctx_factory(NoChoiceGloo()), vol)
    assert outcome.action == "escalated_system"
    assert not outreach_rows(session)


def test_model_selection_without_asks_cannot_report_sent(
    session, make_volunteer, make_shift, assign, coordinator, ctx_factory
):
    class SelectionOnlyGloo:
        def create_response(self, *, input, **kwargs):
            if len(input) > 1:
                return SimpleNamespace(output=[], output_text="Done", usage=usage())
            payload = json.loads(input[0]["content"])
            call = SimpleNamespace(
                type="function_call",
                call_id="choose",
                name="choose_replacements",
                arguments=json.dumps(
                    {
                        "volunteer_ids": [payload["candidates"][0]["volunteer_id"]],
                        "reason": "Available",
                    }
                ),
            )
            return SimpleNamespace(output=[call], output_text=None, usage=usage())

    shift = make_shift("usher")
    vol = make_volunteer("Cancel Person")
    assign(vol, shift)
    make_volunteer("Possible Helper")
    outcome = fill_agent.handle_cancellation(ctx_factory(SelectionOnlyGloo()), vol)
    assert outcome.action == "escalated_system"
    assert session.scalar(select(m.FillRequest)).next_action_at is None
