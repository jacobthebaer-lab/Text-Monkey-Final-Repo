"""The send gate: opt-out, STOP/START, sensitive block, approval hold,
quiet hours, and the monthly ask budget."""

from datetime import datetime, timedelta

import pytest
from sqlalchemy import select

from app.core.send_gate import SendGate, SendStatus, handle_stop_start
from app.db import models as m
from tests.conftest import DENVER, NOW


def test_template_send_logs_message(gate, provider, session, make_volunteer):
    vol = make_volunteer()
    outcome = gate.send(body="Reminder!", purpose="reminder", volunteer=vol)
    assert outcome.status is SendStatus.SENT
    message = session.get(m.Message, outcome.message_id)
    assert message.direction == "out"
    assert message.purpose == "reminder"
    assert message.status == "sent"
    assert provider.sent_to(vol.phone)[0].body == "Reminder!"


def test_opted_out_blocked(gate, provider, make_volunteer):
    vol = make_volunteer(opt_in=False)
    outcome = gate.send(body="Hi", purpose="reminder", volunteer=vol)
    assert outcome.status is SendStatus.BLOCKED_OPT_OUT
    assert provider.sent == []


def test_unknown_purpose_fails_closed(gate, make_volunteer):
    with pytest.raises(ValueError):
        gate.send(body="Hi", purpose="marketing_blast", volunteer=make_volunteer())


def test_sensitive_escalation_blocks_all_automated_texts(gate, provider, session, make_volunteer):
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
    outcome = gate.send(body="Reminder!", purpose="reminder", volunteer=vol)
    assert outcome.status is SendStatus.BLOCKED_SENSITIVE
    assert provider.sent == []


def test_resolved_sensitive_escalation_unblocks(gate, session, make_volunteer):
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
    assert gate.send(body="Reminder!", purpose="reminder", volunteer=vol).sent


def test_quiet_hours_hold(session, clock, provider, make_volunteer):
    clock.set_time(datetime(2026, 10, 1, 22, 0, tzinfo=DENVER))
    gate = SendGate(session, clock, provider)
    outcome = gate.send(body="Hi", purpose="reminder", volunteer=make_volunteer())
    assert outcome.status is SendStatus.HELD_QUIET_HOURS
    assert outcome.retry_at == datetime(2026, 10, 2, 7, 0, tzinfo=DENVER)
    assert provider.sent == []


def test_urgent_fill_respects_wider_window(session, clock, provider, make_volunteer, make_shift):
    role = make_shift("usher").role
    vol = make_volunteer()

    clock.set_time(datetime(2026, 10, 1, 6, 45, tzinfo=DENVER))
    gate = SendGate(session, clock, provider)
    # 6:45 is inside normal quiet hours but inside the urgent window.
    assert (
        gate.send(body="Hi", purpose="reminder", volunteer=vol).status
        is SendStatus.HELD_QUIET_HOURS
    )
    assert gate.send(body="Can you cover today?", purpose="outreach", volunteer=vol, role=role, urgent=True).sent

    # 22:00 is quiet even for urgent fills.
    clock.set_time(datetime(2026, 10, 1, 22, 0, tzinfo=DENVER))
    outcome = gate.send(body="Cover tomorrow?", purpose="outreach", volunteer=vol, role=role, urgent=True)
    assert outcome.status is SendStatus.HELD_QUIET_HOURS
    assert outcome.retry_at == datetime(2026, 10, 2, 6, 30, tzinfo=DENVER)


def test_monthly_ask_budget(gate, provider, make_volunteer, make_shift):
    role = make_shift("usher").role
    vol = make_volunteer()
    for _ in range(4):
        assert gate.send(body="Can you serve?", purpose="outreach", volunteer=vol, role=role).sent
        gate.clock.advance(timedelta(days=1))

    fifth = gate.send(body="One more?", purpose="outreach", volunteer=vol, role=role)
    assert fifth.status is SendStatus.BLOCKED_BUDGET
    # Reminders and confirmations don't count and still go out.
    assert gate.send(body="Reminder!", purpose="reminder", volunteer=vol).sent
    # Another volunteer has their own budget.
    assert gate.send(body="Can you serve?", purpose="outreach", volunteer=make_volunteer(), role=role).sent


def test_needs_approval_role_holds_outreach(gate, provider, session, make_volunteer, make_shift):
    shift = make_shift("nursery", required=("background_check",), fill_policy="needs_approval")
    vol = make_volunteer(quals=[("background_check", "verified", None)])

    outcome = gate.send(
        body="Could you cover nursery Sunday?", purpose="outreach", volunteer=vol, role=shift.role
    )
    assert outcome.status is SendStatus.HELD_FOR_APPROVAL
    assert provider.sent == []
    approval = session.get(m.Approval, outcome.approval_id)
    assert approval.status == "pending"
    assert approval.kind == "send_outreach"
    assert approval.payload["volunteer_id"] == vol.id

    # Coordinator approves; the same payload now goes out.
    approval.status = "approved"
    approval.decided_at = NOW
    approval.decided_by = "Maria Delgado"
    sent = gate.send_approved(approval)
    assert sent.status is SendStatus.SENT
    assert provider.sent_to(vol.phone)[0].body == "Could you cover nursery Sunday?"


def test_send_approved_still_respects_safety_checks(gate, session, provider, make_volunteer, make_shift):
    shift = make_shift("nursery", fill_policy="needs_approval")
    vol = make_volunteer()
    held = gate.send(body="Cover Sunday?", purpose="outreach", volunteer=vol, role=shift.role)
    approval = session.get(m.Approval, held.approval_id)
    approval.status = "approved"

    vol.sms_opt_in = False  # they opted out while approval was pending
    assert gate.send_approved(approval).status is SendStatus.BLOCKED_OPT_OUT
    assert provider.sent == []


def test_auto_role_outreach_sends(gate, make_volunteer, make_shift):
    shift = make_shift("greeter", fill_policy="auto")
    assert gate.send(
        body="Can you greet Sunday?", purpose="outreach", volunteer=make_volunteer(), role=shift.role
    ).sent


def test_unknown_number_reply_by_phone(gate, provider):
    outcome = gate.send(body="This number is for volunteers.", purpose="unknown_number", phone="+15559990000")
    assert outcome.sent
    assert provider.sent_to("+15559990000")


def test_stop_opts_out_confirms_once_then_silence(session, clock, provider, gate, make_volunteer):
    vol = make_volunteer()
    assert handle_stop_start(session, clock, provider, vol, " stop ") == "stop"
    assert vol.sms_opt_in is False
    assert len(provider.sent_to(vol.phone)) == 1  # single mandated confirmation

    assert handle_stop_start(session, clock, provider, vol, "STOP") == "stop"
    assert len(provider.sent_to(vol.phone)) == 1  # no second confirmation

    assert gate.send(body="Hi", purpose="reminder", volunteer=vol).status is SendStatus.BLOCKED_OPT_OUT
    assert len(provider.sent_to(vol.phone)) == 1  # never texted again


def test_start_opts_back_in(session, clock, provider, gate, make_volunteer):
    vol = make_volunteer(opt_in=False)
    assert handle_stop_start(session, clock, provider, vol, "START") == "start"
    assert vol.sms_opt_in is True
    assert gate.send(body="Hi", purpose="reminder", volunteer=vol).sent


def test_non_keyword_is_ignored(session, clock, provider, make_volunteer):
    vol = make_volunteer()
    assert handle_stop_start(session, clock, provider, vol, "can't make it sunday") is None
    assert vol.sms_opt_in is True
    assert provider.sent == []


def test_all_sent_messages_are_logged(gate, session, provider, make_volunteer):
    vol = make_volunteer()
    gate.send(body="A", purpose="reminder", volunteer=vol)
    gate.send(body="B", purpose="confirmation", volunteer=vol)
    logged = session.scalars(select(m.Message).where(m.Message.direction == "out")).all()
    assert len(logged) == len(provider.sent) == 2
