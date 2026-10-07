"""Empty-pool recovery with synthetic volunteers and mocked Gloo/transport."""
from datetime import timedelta

import pytest
from sqlalchemy import select

from app.agents.fill_agent import FillContext, handle_cancellation
from app.core import algorithm_outreach, offer_windows, paired_planning, confirmations
from app.db import models as m
from app.jobs import process_due_fill_requests
from tests.conftest import DENVER
from tests.test_fill_agent import ScriptedAgentGloo, FailingGloo


@pytest.fixture
def empty_fill(session, clock, provider, make_volunteer, make_shift, assign):
    ctx = FillContext(session, clock, provider, ScriptedAgentGloo())
    session.add(m.Policy(key="algorithm_outreach_enabled", value={"value": True}))
    shift = make_shift("Greeter")
    sender = make_volunteer("Fictional cancelling volunteer")
    assign(sender, shift)
    outcome = handle_cancellation(ctx, sender)
    fill = session.get(m.FillRequest, outcome.fill_request_id)
    assert fill.state == "escalated"
    return ctx, fill, shift, sender


def test_empty_pool_persists_reasons_and_rechecks_without_outreach(empty_fill, session, clock):
    ctx, fill, _, sender = empty_fill
    row = session.get(m.Notification, f"replacement-pool:{fill.id}")
    assert row.detail["excluded"] == [{"volunteer_id": sender.id, "reasons": ["cancelled this assignment"]}]
    clock.set_time(row.due_at)
    assert process_due_fill_requests(ctx) == []
    assert fill.state == "escalated" and row.due_at > clock.now()
    assert session.scalar(select(m.Outreach)) is None
    assert len(session.scalars(select(m.Escalation)).all()) == 1


def test_new_candidates_resume_exact_clyde_batch_and_dispatch_timer(empty_fill, session, clock, make_volunteer):
    ctx, fill, _, _ = empty_fill
    helpers = [make_volunteer(f"Fictional helper {i}") for i in range(4)]
    row = session.get(m.Notification, f"replacement-pool:{fill.id}")
    clock.set_time(row.due_at)
    score, urgency = algorithm_outreach.config(session)
    from app.core import clyde_algorithm
    expected = clyde_algorithm.plan_initial_batch(
        [algorithm_outreach.profile(session, p, clock.now()) for p in helpers],
        1, clock.now(), score,
        deadline=offer_windows.cutoff(session, empty_fill[2].starts_at), urgency=urgency)
    assert process_due_fill_requests(ctx)[0].action == "tranche_sent"
    batch = algorithm_outreach.receipt(session, fill)
    # The receipt captures pre-dispatch scoring, so use its recorded order and
    # acceptance target, independent of newly created successful send history.
    assert len(batch.detail["volunteer_ids"]) == len(expected.volunteers) == 2
    assert batch.detail["volunteer_ids"] == [int(p.volunteer_id) for p in expected.volunteers]
    asks = session.scalars(select(m.Outreach)).all()
    assert all(session.get(m.Message, o.message_id).status == "sent" for o in asks)
    assert fill.next_action_at == min(offer_windows.metadata(session, o).expires_at for o in asks)
    assert process_due_fill_requests(ctx) == []
    assert len(session.scalars(select(m.Outreach)).all()) == 2


@pytest.mark.parametrize("stop", ["closed_event", "changed_event", "deadline", "human_closed", "system_hold"])
def test_pool_retry_does_not_reopen_terminal_or_human_holds(empty_fill, session, clock, make_volunteer, stop):
    ctx, fill, shift, _ = empty_fill
    make_volunteer("Fictional available helper")
    row = session.get(m.Notification, f"replacement-pool:{fill.id}")
    if stop == "closed_event":
        shift.event.status = "cancelled"
    elif stop == "changed_event":
        shift.event.starts_at += timedelta(hours=1)
    elif stop == "human_closed":
        session.scalar(select(m.Escalation)).status = "resolved"
    elif stop == "system_hold":
        session.add(m.Escalation(category="system_error", severity="normal", summary="Fictional held workflow",
            related_ids={"fill_request_id": fill.id}, status="open", created_at=clock.now()))
    clock.set_time(row.expires_at if stop == "deadline" else row.due_at)
    assert process_due_fill_requests(ctx) == []
    assert row.state == "closed" and fill.state == "escalated"
    assert session.scalar(select(m.Outreach)) is None


def test_pair_rule_stays_held_until_partner_is_assigned(empty_fill, session, clock, make_volunteer, make_shift, assign):
    ctx, fill, shift, _ = empty_fill
    partner = make_shift("Production", starts=shift.starts_at+timedelta(hours=2))
    person = make_volunteer("Fictional paired helper")
    review = paired_planning.stage_rules(session, clock.now(), person,
        pairs=[{"role_ids": [shift.role_id, partner.role_id]}])
    assert confirmations.decide(session, ctx.gate, review, approve=True, actor="test",
        expected=review.payload["content_hash"], now=clock.now(), ctx=ctx)
    row = session.get(m.Notification, f"replacement-pool:{fill.id}")
    clock.set_time(row.due_at)
    assert process_due_fill_requests(ctx) == []
    assert "partner role" in next(x["reasons"][0] for x in row.detail["excluded"] if x["volunteer_id"] == person.id)
    assert session.scalar(select(m.Outreach)) is None
    assign(person, partner)
    clock.set_time(row.due_at)
    assert process_due_fill_requests(ctx)[0].action == "tranche_sent"
    assert session.scalar(select(m.Outreach)).volunteer_id == person.id


def test_consent_and_quiet_hours_remain_enforced(empty_fill, session, clock, make_volunteer):
    ctx, fill, _, _ = empty_fill
    helper = make_volunteer("Fictional helper", opt_in=False)
    row = session.get(m.Notification, f"replacement-pool:{fill.id}")
    clock.set_time(row.due_at)
    assert process_due_fill_requests(ctx) == []
    assert any(x["volunteer_id"] == helper.id and x["reasons"] == ["texting consent is disabled"] for x in row.detail["excluded"])
    helper.sms_opt_in = True
    clock.set_time(clock.now().astimezone(DENVER).replace(hour=22))
    assert process_due_fill_requests(ctx)[0].action == "waiting_quiet"
    assert fill.state == "waiting_quiet" and session.scalar(select(m.Outreach)) is None


def test_resume_holds_when_gloo_is_unavailable(empty_fill, session, clock, make_volunteer, provider):
    ctx, fill, _, _ = empty_fill
    helper = make_volunteer("Fictional helper")
    ctx.gloo = FailingGloo()
    clock.set_time(session.get(m.Notification, f"replacement-pool:{fill.id}").due_at)
    assert process_due_fill_requests(ctx)[0].action == "escalated_system"
    assert fill.state == "escalated" and not provider.sent_to(helper.phone)
    clock.advance(timedelta(minutes=2))
    assert process_due_fill_requests(ctx) == []
