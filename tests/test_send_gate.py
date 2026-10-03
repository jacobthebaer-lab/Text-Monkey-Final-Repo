"""Quiet conversation policy preserves consent, safety, review and transport guards."""

from datetime import datetime, timedelta

import pytest
from sqlalchemy import select
from types import SimpleNamespace

from app.core.send_gate import SendGate, SendStatus, handle_stop_start
from app.db import models as m
from tests.conftest import DENVER, NOW


def test_recorded_schedule_notice_logs_message(gate, provider, session, make_volunteer, make_shift, assign):
    vol = make_volunteer()
    assignment = assign(vol, make_shift())
    outcome = gate.send(body="You are scheduled.", purpose="confirmation", volunteer=vol,
                        conversation={"assignment_id": assignment.id, "notice": "scheduled"})
    assert outcome.status is SendStatus.SENT
    message = session.get(m.Message, outcome.message_id)
    assert message.direction == "out"
    assert message.purpose == "confirmation"
    assert message.status == "sent"
    assert provider.sent_to(vol.phone)[0].body == "You are scheduled."


def test_opted_out_blocked(gate, provider, make_volunteer):
    vol = make_volunteer(opt_in=False)
    outcome = gate.send(body="Hi", purpose="manual", volunteer=vol)
    assert outcome.status is SendStatus.BLOCKED_OPT_OUT
    assert provider.sent == []


def test_unknown_purpose_fails_closed(gate, make_volunteer):
    with pytest.raises(ValueError):
        gate.send(body="Hi", purpose="marketing_blast", volunteer=make_volunteer())


def test_sensitive_escalation_blocks_eligible_schedule_notice(gate, provider, session, make_volunteer, make_shift, assign):
    vol = make_volunteer()
    session.add(
        m.Escalation(
            category="sensitive",
            severity="urgent",
            summary="family emergency",
            related_ids={"volunteer_id": vol.id},
            status="open",
            created_at=NOW,
        )
    )
    session.flush()
    assignment = assign(vol, make_shift())
    outcome = gate.send(body="Scheduled.", purpose="confirmation", volunteer=vol,
                        conversation={"assignment_id": assignment.id, "notice": "scheduled"})
    assert outcome.status is SendStatus.BLOCKED_SENSITIVE
    assert provider.sent == []


def test_resolved_sensitive_escalation_unblocks(gate, session, make_volunteer, make_shift, assign):
    vol = make_volunteer()
    session.add(
        m.Escalation(
            category="sensitive",
            summary="past issue",
            related_ids={"volunteer_id": vol.id},
            status="resolved",
            created_at=NOW - timedelta(days=30),
        )
    )
    session.flush()
    assignment = assign(vol, make_shift())
    assert gate.send(body="Scheduled.", purpose="confirmation", volunteer=vol,
                     conversation={"assignment_id": assignment.id, "notice": "scheduled"}).sent


def test_quiet_hours_hold(session, clock, provider, make_volunteer):
    clock.set_time(datetime(2026, 10, 1, 22, 0, tzinfo=DENVER))
    gate = SendGate(session, clock, provider)
    outcome = gate.send(body="Hi", purpose="coordinator_notify", volunteer=make_volunteer(coordinator=True))
    assert outcome.status is SendStatus.HELD_QUIET_HOURS
    assert outcome.retry_at == datetime(2026, 10, 2, 7, 0, tzinfo=DENVER)
    assert provider.sent == []


def test_urgent_window_cannot_enable_prohibited_outreach(session, clock, provider, make_volunteer, make_shift):
    role = make_shift("usher").role
    vol = make_volunteer()
    gate = SendGate(session, clock, provider)
    gate.gloo = SimpleNamespace(create_response=lambda **kw: pytest.fail("No model for suppressed offers"))
    for hour, minute in [(6, 45), (22, 0)]:
        clock.set_time(datetime(2026, 10, 1, hour, minute, tzinfo=DENVER))
        outcome = gate.send(body="Can you cover?", purpose="outreach", volunteer=vol, role=role, urgent=True)
        assert outcome.status is SendStatus.BLOCKED_POLICY
    assert not provider.sent and session.scalar(select(m.Message)) is None
    assert session.scalar(select(m.Approval)) is None
    admin = make_volunteer(coordinator=True)
    clock.set_time(datetime(2026, 10, 1, 6, 45, tzinfo=DENVER))
    assert gate.send(body="Coverage gap.", purpose="coordinator_notify", volunteer=admin).status is SendStatus.HELD_QUIET_HOURS
    assert gate.send(body="Urgent coverage gap.", purpose="coordinator_notify", volunteer=admin, urgent=True).sent
    clock.set_time(datetime(2026, 10, 1, 22, 0, tzinfo=DENVER))
    held = gate.send(body="Urgent coverage gap.", purpose="coordinator_notify", volunteer=admin, urgent=True)
    assert held.status is SendStatus.HELD_QUIET_HOURS
    assert held.retry_at == datetime(2026, 10, 2, 6, 30, tzinfo=DENVER)


def test_monthly_ask_budget(gate, provider, session, make_volunteer, make_shift, assign):
    vol = make_volunteer()
    other = make_volunteer()
    for _ in range(4):
        session.add(m.Message(phone=vol.phone, volunteer_id=vol.id, direction="out", body="Historical ask",
                              purpose="outreach", status="sent", kind="template", created_at=NOW))
    assignment = assign(vol, make_shift())
    assert gate._asks_this_month(vol.id, NOW) == 4
    assert gate._asks_this_month(other.id, NOW) == 0
    for recipient in (vol, other):
        assert gate.send(body="Can you serve?", purpose="outreach", volunteer=recipient).status is SendStatus.BLOCKED_POLICY
    assert gate.send(body="You are scheduled.", purpose="confirmation", volunteer=vol,
                     conversation={"assignment_id": assignment.id, "notice": "scheduled"}).sent
    assert len(provider.sent) == 1 and gate._asks_this_month(vol.id, NOW) == 4


def test_needs_approval_role_cannot_stage_prohibited_outreach(gate, provider, session, make_volunteer, make_shift):
    shift = make_shift("nursery", required=("background_check",), fill_policy="needs_approval")
    vol = make_volunteer(quals=[("background_check", "verified", None)])
    outcome = gate.send(body="Could you cover nursery Sunday?", purpose="outreach", volunteer=vol, role=shift.role)
    assert outcome.status is SendStatus.BLOCKED_POLICY
    assert not provider.sent and session.scalar(select(m.Approval)) is None
    assert session.scalar(select(m.Message)) is None


def test_send_approved_still_respects_safety_checks(gate, session, provider, make_volunteer, make_shift):
    shift = make_shift("nursery", fill_policy="needs_approval")
    vol = make_volunteer()
    approval = m.Approval(kind="send_outreach", status="approved", requested_at=NOW,
                          payload={"volunteer_id": vol.id, "role_id": shift.role_id, "body": "Cover Sunday?",
                                   "purpose": "outreach", "phone": vol.phone})
    session.add(approval); session.flush()
    # An old coordinator approval cannot restore prohibited automatic outreach.
    assert gate.send_approved(approval).status is SendStatus.BLOCKED_POLICY
    session.add(m.Policy(key="sms_opt_out:" + vol.phone, value={"value": True})); session.flush()
    assert gate.send_approved(approval).status is SendStatus.BLOCKED_OPT_OUT
    vol.sms_opt_in = False
    assert gate.send(body="Exact human draft", purpose="manual", volunteer=vol).status is SendStatus.BLOCKED_OPT_OUT
    assert not provider.sent and session.scalar(select(m.Message)) is None


def test_auto_role_cannot_enable_prohibited_outreach(gate, session, provider, make_volunteer, make_shift):
    shift = make_shift("greeter", fill_policy="auto")
    assert gate.send(body="Can you greet Sunday?", purpose="outreach", volunteer=make_volunteer(),
                     role=shift.role).status is SendStatus.BLOCKED_POLICY
    assert not provider.sent and session.scalar(select(m.Approval)) is None


def test_unknown_number_does_not_receive_unsolicited_reply(gate, provider):
    outcome = gate.send(body="This number is for volunteers.", purpose="unknown_number", phone="+15559990000")
    assert outcome.status is SendStatus.BLOCKED_POLICY
    assert not provider.sent_to("+15559990000")


def test_stop_opts_out_confirms_once_then_silence(session, clock, provider, gate, make_volunteer):
    vol = make_volunteer()
    assert handle_stop_start(session, clock, provider, vol, " stop ") == "stop"
    assert vol.sms_opt_in is False
    assert len(provider.sent_to(vol.phone)) == 1  # single mandated confirmation

    assert handle_stop_start(session, clock, provider, vol, "STOP") == "stop"
    assert len(provider.sent_to(vol.phone)) == 1  # no second confirmation

    assert gate.send(body="Hi", purpose="manual", volunteer=vol).status is SendStatus.BLOCKED_OPT_OUT
    assert len(provider.sent_to(vol.phone)) == 1  # never texted again


def test_start_opts_back_in(session, clock, provider, gate, make_volunteer):
    vol = make_volunteer(opt_in=False)
    assert handle_stop_start(session, clock, provider, vol, "START") == "start"
    assert vol.sms_opt_in is True
    assert gate.send(body="Hi", purpose="reminder", volunteer=vol).status is SendStatus.BLOCKED_POLICY
    assert len(provider.sent_to(vol.phone)) == 1


def test_non_keyword_is_ignored(session, clock, provider, make_volunteer):
    vol = make_volunteer()
    assert handle_stop_start(session, clock, provider, vol, "can't make it sunday") is None
    assert vol.sms_opt_in is True
    assert provider.sent == []


def test_all_sent_messages_are_logged(gate, session, provider, make_volunteer):
    vol = make_volunteer(coordinator=True)
    assert gate.send(body="A", purpose="coordinator_notify", volunteer=vol).sent
    assert gate.send(body="B", purpose="admin_reply", volunteer=vol).sent
    logged = session.scalars(select(m.Message).where(m.Message.direction == "out")).all()
    assert len(logged) == len(provider.sent) == 2
