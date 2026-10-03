"""Central quiet policy runs before Gloo; all identities and transports are synthetic."""
from datetime import timedelta
from zoneinfo import ZoneInfo
import pytest
from sqlalchemy import select
from app.core import availability_review, reminders
from app.db import models as m
from tests.test_planning_workflows_api import planning_client, requested, decision, PREFIX
from tests.test_planning_composition import collection, context, CopyGloo


def test_collection_reports_policy_and_persists_terminal_receipts_without_gloo(planning_client):
    client, app, _ = planning_client
    parent = requested(client)
    approved = decision(client, parent).json()["collection"]
    assert approved["composition_status"] == "blocked_policy"
    assert approved["suppressed_recipient_count"] == 2
    assert approved["remaining_recipient_count"] == 0
    with app.state.session_factory() as session:
        # Snapshot is read-only: explicit preparation owns the durable job receipts.
        assert not session.scalar(select(m.Policy).where(m.Policy.key.startswith("job:availability:")))
    for _ in range(2):
        response = decision(client, parent, "retry")
        assert response.status_code == 200
        view = response.json()["collection"]
        assert view["composition_status"] == "blocked_policy"
        assert view["hold_reason"] == "Routine volunteer acknowledgments, progress and offer prompts are suppressed"
        assert view["remaining_recipient_count"] == 0 and view["suppressed_recipient_count"] == 2
        assert not view["text_review_ids"] and view["retry_at"] is None
    assert client.get(f"{PREFIX}/{parent['id']}").json()["collection"] == view
    assert app.state.gloo.calls == 0 and not app.state.provider.sent
    with app.state.session_factory() as session:
        receipts = session.scalars(select(m.Policy).where(m.Policy.key.startswith("job:availability:"))).all()
        assert len(receipts) == 2
        assert all(r.value["state"] == "blocked_policy" and r.value["policy_reason"] == view["hold_reason"] for r in receipts)
        assert all("body" not in r.value and "gloo_attempts" not in r.value for r in receipts)
        assert not session.scalar(select(m.Message))
        assert not session.scalar(select(m.Approval).where(m.Approval.kind == "confirm_text"))


@pytest.mark.parametrize("state", ["pending", "gloo_blocked"])
def test_precheck_expires_old_pending_collection_review_without_recomposition(session, clock, provider, make_volunteer, tmp_path, state):
    volunteer = make_volunteer()
    child = collection(session, clock)
    source = {"type": "availability", "collection_id": child.id, "month": "2026-11", "reminder": False}
    key = f"availability:{child.id}:{volunteer.id}:0"
    old = m.Approval(kind="confirm_text", status="pending", payload={}, requested_at=clock.now())
    session.add(old); session.flush()
    receipt = m.Policy(key="job:" + key, value={"approval_id": old.id, "state": state, "body": "Old question"})
    session.add(receipt); session.flush()
    ctx = context(session, clock, provider, tmp_path)
    assert not reminders.once(ctx, key, volunteer, "Availability facts", "availability_ask", source=source)
    assert old.status == "expired" and receipt.value["state"] == "blocked_policy"
    assert "approval_id" not in receipt.value and "body" not in receipt.value
    assert ctx.gloo.calls == 0 and not provider.sent
    parent = session.get(m.Approval, child.payload["parent_review_id"])
    view = availability_review.snapshot(session, parent, clock.now())
    assert view["composition_status"] == "blocked_policy" and not view["text_review_ids"]


def test_uncertain_delivery_stays_held_for_reconciliation(session, clock, provider, make_volunteer, tmp_path):
    volunteer = make_volunteer()
    child = collection(session, clock)
    key = f"availability:{child.id}:{volunteer.id}:0"
    receipt = m.Policy(key="job:" + key, value={"state": "uncertain"})
    session.add(receipt); session.flush()
    ctx = context(session, clock, provider, tmp_path)
    source = {"type": "availability", "collection_id": child.id, "month": "2026-11", "reminder": False}
    assert not reminders.once(ctx, key, volunteer, "Old availability question", "availability_ask", source=source)
    assert receipt.value == {"state": "uncertain"} and ctx.gloo.calls == 0
    view = availability_review.snapshot(session, session.get(m.Approval, child.payload["parent_review_id"]), clock.now())
    assert view["composition_status"] == "held" and "operator review" in view["hold_reason"]


@pytest.mark.parametrize("purpose", ["confirmation", "reminder"])
def test_real_assignment_notifications_still_compose_and_stage_exact_review(session, clock, provider, make_volunteer, make_shift, assign, tmp_path, purpose):
    volunteer = make_volunteer()
    days = 1 if purpose == "reminder" else 3
    assignment = assign(volunteer, make_shift(starts=clock.now() + timedelta(days=days)))
    ctx = context(session, clock, provider, tmp_path)
    source = reminders.assignment_source(assignment, purpose)
    body = reminders.day_before_copy(assignment, ZoneInfo("America/Denver")) if purpose == "reminder" else "A verified placement"
    assert not reminders.once(ctx, purpose, volunteer, body, purpose, source=source, exact_copy=purpose == "reminder")
    receipt = session.get(m.Policy, "job:" + purpose)
    review = session.get(m.Approval, receipt.value["approval_id"])
    assert review.status == "pending" and review.payload["purpose"] == purpose
    assert review.payload["conversation"]["assignment_id"] == assignment.id
    assert review.payload["conversation"]["notice"] == ("day_before" if purpose == "reminder" else "scheduled")
    assert ctx.gloo.calls == 1 and not provider.sent


def test_allowed_admin_summary_still_composes_and_stages(session, clock, provider, make_volunteer, tmp_path):
    coordinator = make_volunteer(coordinator=True)
    ctx = context(session, clock, provider, tmp_path, CopyGloo())
    source = reminders.summary_source(session, (clock.now().date() + timedelta(days=1)).isoformat(), clock.now(), ZoneInfo("America/Denver"))
    assert not reminders.once(ctx, "summary:test", coordinator, "Current internal coverage", "coordinator_notify", source=source)
    receipt = session.get(m.Policy, "job:summary:test")
    review = session.get(m.Approval, receipt.value["approval_id"])
    assert review.status == "pending" and review.payload["purpose"] == "coordinator_notify"
    assert ctx.gloo.calls == 1 and not provider.sent
