"""Fill agent end to end on the fake clock, with a scripted fake Gloo.

Covers the phase's done-when: the demo-critical scenario (cancel -> T1 no
reply -> T2 yes -> filled, others thanked), sensitive cancellation escalating
with no auto-reply, and the kids role waiting for coordinator YES.
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
    """Writes one ask per tranche member, schedules the timer, then finishes."""

    def create_response(self, *, model, input, instructions=None, tools=None, **kwargs):
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
                        f"on {payload['shift']['starts_at'][:10]}? No worries if not — reply YES or NO.",
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


def test_demo_critical_scenario(session, clock, provider, make_volunteer, make_shift, assign, coordinator, pastor, ctx_factory):
    """cancel -> T1 asks -> no replies -> T2 asks -> yes -> filled, others thanked."""
    ctx = ctx_factory()
    shift = make_shift("greeter")  # Sunday Oct 4, 9:00 — 71h out (>48h tier)
    zoe = make_volunteer("Zoe Adams")
    assign(zoe, shift, status="approved")
    candidates = [make_volunteer(f"Candidate {c}") for c in "ABCDEFGHIJ"]

    result = handle_inbound(
        session, clock, provider, zoe.phone, "cant make it sunday sorry!!",
        parser_returning(intent="cancel", confidence=0.9), ctx=ctx,
    )
    assert result.routed_to == "fill_agent"

    fill = session.scalar(select(m.FillRequest))
    assignment = session.scalar(select(m.Assignment).where(m.Assignment.volunteer_id == zoe.id))
    assert assignment.status == "cancelled"
    assert "off the schedule" in provider.sent_to(zoe.phone)[0].body  # kind ack
    assert fill.state == "in_progress" and fill.urgency == "normal" and fill.current_tranche == 1
    tranche1 = outreach_rows(session, tranche=1)
    assert len(tranche1) == 1 and all(o.message_id for o in tranche1)
    assert fill.next_action_at == NOW + timedelta(hours=2)

    # Nobody replies; the timer fires; tranche 2 goes to the next 5.
    clock.advance(timedelta(hours=2))
    outcomes = jobs.process_due_fill_requests(ctx)
    assert [o.action for o in outcomes] == ["tranche_sent"]
    assert fill.current_tranche == 2
    tranche2 = outreach_rows(session, tranche=2)
    assert len(tranche2) == 1 and all(o.message_id for o in tranche2)

    # A tranche-2 member says yes.
    winner = session.get(m.Volunteer, tranche2[0].volunteer_id)
    handle_inbound(session, clock, provider, winner.phone, "yes!!",
                   parser_returning(intent="accept", confidence=0.97), ctx=ctx)

    assert fill.state == "filled" and fill.next_action_at is None
    new_assignment = session.scalar(
        select(m.Assignment).where(m.Assignment.volunteer_id == winner.id, m.Assignment.shift_id == shift.id)
    )
    assert new_assignment.status == "confirmed" and new_assignment.source == "fill"
    assert any("confirmed" in s.body for s in provider.sent_to(winner.phone))
    # Everyone else who was asked and hadn't replied gets the thank-you.
    others = [o for o in outreach_rows(session) if o.volunteer_id != winner.id]
    assert len(others) == 1
    assert others[0].response == "expired"
    for o in others:
        vol = session.get(m.Volunteer, o.volunteer_id)
        assert not any("filled" in s.body for s in provider.sent_to(vol.phone))
    assert any("covered" in s.body for s in provider.sent_to(coordinator.phone))

    # A second yes after it's filled gets thanks, no double-assign.
    second = session.get(m.Volunteer, tranche1[0].volunteer_id)
    handle_inbound(session, clock, provider, second.phone, "Y",
                   parser_returning(intent="accept", confidence=0.97), ctx=ctx)
    assert session.scalar(
        select(m.Assignment).where(m.Assignment.volunteer_id == second.id, m.Assignment.shift_id == shift.id)
    ) is None

    # The session log has runs with steps and token usage.
    runs = session.scalars(select(m.AgentRun)).all()
    assert runs and any(r.input_tokens > 0 for r in runs)
    steps = session.scalars(select(m.AgentStep)).all()
    assert any(s.type == "tool_call" and s.tool_name == "request_send_text" for s in steps)


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


def test_kids_role_waits_for_coordinator_yes(
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
    assert fill.state == "waiting_approval"
    assert fill.urgency == "critical"  # below minimum on a critical role
    for helper in helpers[:3]:
        assert provider.sent_to(helper.phone) == []  # nothing until YES
    assert provider.sent_to(coordinator.phone) == []
    clock.advance(timedelta(minutes=5))
    jobs.process_due_fill_requests(ctx)
    ask = [s for s in provider.sent_to(coordinator.phone) if "Reply YES A" in s.body]
    assert len(ask) == 1

    handle_inbound(session, clock, provider, coordinator.phone, "YES", parser_returning(), ctx=ctx)

    assert fill.state == "in_progress" and fill.next_action_at is not None
    sent_count = sum(1 for h in helpers if provider.sent_to(h.phone))
    assert sent_count == 1
    approvals = session.scalars(select(m.Approval)).all()
    assert all(a.status == "approved" for a in approvals)
    assert all(o.message_id for o in outreach_rows(session, tranche=1))


def test_ambiguous_shift_asks_numbered_question(
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
    question = provider.sent_to(vol.phone)[0].body
    assert "which one" in question and "1)" in question and "2)" in question
    assert session.scalar(select(m.FillRequest)) is None  # nothing cancelled yet

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


def test_ineligible_yes_gets_thanks_not_assignment(
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
    # Eager gets double-booked before replying yes.
    conflict = make_shift("greeter")
    assign(eager, conflict, status="confirmed")

    handle_inbound(session, clock, provider, eager.phone, "yes!!",
                   parser_returning(intent="accept", confidence=0.95), ctx=ctx)

    assert session.scalar(
        select(m.Assignment).where(m.Assignment.volunteer_id == eager.id, m.Assignment.shift_id == shift.id)
    ) is None
    assert any("willing" in s.body for s in provider.sent_to(eager.phone))
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

    for o in tranche1:  # everyone in T1 says no — T2 opens without waiting 4h
        member = session.get(m.Volunteer, o.volunteer_id)
        handle_inbound(session, clock, provider, member.phone, "no sorry",
                       parser_returning(intent="decline", confidence=0.95), ctx=ctx)

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
    assert provider.sent_to(other.phone) and not provider.sent_to(top.phone)
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
