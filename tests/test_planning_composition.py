"""Synthetic recipients, fake model responses, no native transport."""
import json
from datetime import timedelta
from types import SimpleNamespace as NS
import pytest
from sqlalchemy import select, update
from app.agents.fill_agent import FillContext
from app.agents.planning_agent import request_collection, collect, plan_month, publish
from app.config import Settings
from app.core import confirmations, reminders, scheduler
from app.db import models as m
from app.llm.gloo_client import GlooUnavailableError, NullGloo


class CopyGloo:
    settings = Settings(gloo_signup_replies=False)
    def __init__(self, invalid=False):
        self.calls = 0
        self.invalid = invalid
    def create_response(self, *, input, **kwargs):
        self.calls += 1
        if isinstance(input, str):
            facts = json.loads(input)
            body = ("" if facts.get("exact_copy") else "Quick update: ") + facts["approved_message"]
            return NS(output_text="Invented reply without dates" if self.invalid else body, usage=None)
        return NS(output=[NS(type="message")], output_text="Reviewed schedule; leave any unresolved gap for the coordinator.", usage=None)


class ConnectedDouble:
    """Queue-only fake, deliberately not a MockSMSProvider subclass."""
    def __init__(self): self.sent = []
    def send(self, phone, body):
        self.sent.append((phone, body))
        return f"SYNTHETIC{len(self.sent)}"


def context(session, clock, provider, tmp_path, gloo=None, exact=True):
    if exact:
        session.info[confirmations.MODE_KEY] = True
        session.info["confirmation_now"] = clock.now()
    return FillContext(session, clock, provider, gloo or CopyGloo(), log_dir=tmp_path)


def reviewed(session, ctx, approval, approve=True):
    return confirmations.decide(session, ctx.gate, approval, approve=approve, actor="synthetic-admin",
        expected=approval.payload["content_hash"], now=ctx.clock.now(), ctx=ctx)


def human_change(session, function):
    session.info["record_authorized"] = True
    try:
        function(); session.flush()
    finally:
        session.info["record_authorized"] = False


def collection(session, clock):
    from app.core import availability_review
    owner="11111111-1111-4111-8111-111111111111"
    session.info[confirmations.MODE_KEY]=True
    parent=availability_review.request_review(session,owner,"2026-11",clock.now())
    availability_review.decide(session,parent,owner,clock.now(),parent.payload["content_hash"],True)
    return session.get(m.Approval,parent.payload["collection_id"])


def historical_collection_review(session, ctx, volunteer, parent):
    """Stored pre-policy review, to exercise its retained source/revocation guards."""
    source = {"type":"availability", "collection_id":parent.id, "month":"2026-11", "reminder":False}
    key = f"job:availability:{parent.id}:{volunteer.id}:0"
    body = "Historical approved availability question for 2026-11."
    transport = "mac_messages" if hasattr(ctx.provider, "allows") else "mock_or_twilio"
    approval = confirmations.stage_text(ctx.gate, {"transport":transport,
        "phone":volunteer.phone, "volunteer_id":volunteer.id, "body":body,
        "purpose":"availability_ask", "kind":"ai", "workflow_job_key":key,
        "workflow_source_hash":"historical-fixture"})
    session.add(m.Policy(key=key, value={"approval_id":approval.id, "source":source,
        "source_hash":"historical-fixture", "phone":volunteer.phone, "body":body,
        "volunteer_id":volunteer.id}))
    session.flush()
    return approval


def test_connected_jobs_stage_reviewed_reminders_and_hold_parent_actions(session, clock, make_volunteer, make_shift, assign, tmp_path, monkeypatch):
    from app import jobs
    clock.set_time(clock.now().replace(day=15))
    volunteer = make_volunteer()
    assign(volunteer, make_shift(starts=clock.now() + timedelta(days=1)))
    provider = ConnectedDouble()
    ctx = context(session, clock, provider, tmp_path)
    parent = request_collection(ctx, '2026-11')
    monkeypatch.setattr(jobs, 'process_due_fill_requests', lambda current: ['reviewed fill jobs'])
    result = jobs.process_jobs(ctx)
    assert result['fills'] == ['reviewed fill jobs']
    assert result['collection_and_planning'] == 'held_for_authorized_parent_approval'
    assert result['legacy_controls'] == 'held_for_connected_review'
    reviews = session.scalars(select(m.Approval).where(m.Approval.payload['purpose'].as_string() == 'reminder')).all()
    assert len(reviews) == 1 and confirmations.valid(reviews[0], clock.now())
    confirmation = session.scalar(select(m.Approval).where(m.Approval.payload['purpose'].as_string() == 'confirmation'))
    assert confirmation is None  # The day-before message replaces a duplicate confirm request.
    assert ctx.gloo.calls == 1 and not provider.sent
    assert parent.status == 'pending'
    jobs.process_jobs(ctx)
    assert ctx.gloo.calls == 1 and not provider.sent
    assert not session.scalar(select(m.Policy).where(m.Policy.key.startswith('job:plan:')))


def test_plan_job_outage_does_not_prevent_recovery(session, clock, make_volunteer, make_shift, tmp_path, monkeypatch):
    from app import jobs
    from app.sms.mock_provider import MockSMSProvider
    make_volunteer()
    make_shift(starts=clock.now().replace(month=11, day=1))
    ctx = context(session, clock, MockSMSProvider(), tmp_path, NullGloo())
    parent = collection(session, clock)
    parent.decided_at = clock.now() - timedelta(days=3)
    monkeypatch.setattr(jobs, 'process_due_fill_requests', lambda current: [])
    assert jobs.process_jobs(ctx)['plan']['state'] == 'held_for_review'
    assert session.get(m.Policy, f'job:plan:{parent.id}') is None
    assert not session.scalar(select(m.Assignment)) and not ctx.provider.sent
    ctx.gloo = CopyGloo()
    clock.advance(timedelta(minutes=2))
    assert jobs.process_jobs(ctx)['plan']['state'] == 'pending_exact_review'
    assert session.get(m.Policy, f'job:plan:{parent.id}').value['done']
    assert not session.scalar(select(m.Assignment)) and not ctx.provider.sent


def test_connected_collection_is_suppressed_once_without_composition(session, clock, make_volunteer, tmp_path):
    v = make_volunteer(); a = collection(session, clock); provider = ConnectedDouble()
    ctx = context(session, clock, provider, tmp_path)
    assert collect(ctx,a) == {"sent":[], "reviews":[]}
    receipt = session.get(m.Policy,f"job:availability:{a.id}:{v.id}:0")
    assert receipt.value["state"] == "blocked_policy" and receipt.value["policy_reason"]
    original = dict(receipt.value)
    assert collect(ctx,a) == {"sent":[], "reviews":[]}
    assert receipt.value == original and ctx.gloo.calls == 0 and not provider.sent
    assert not session.scalar(select(m.Approval.id).where(m.Approval.kind=="confirm_text"))


@pytest.mark.parametrize("failure", ["outage", "invalid"])
def test_gloo_failure_never_stages_or_sends_seed_copy(session, clock, make_volunteer, make_shift, assign, tmp_path, failure):
    v = make_volunteer(); assign(v,make_shift(starts=clock.now()+timedelta(days=1)))
    ctx = context(session,clock,ConnectedDouble(),tmp_path,NullGloo() if failure=="outage" else CopyGloo(invalid=True))
    for _ in range(3):
        assert reminders.process(ctx)["reminders"] == 0
        clock.advance(timedelta(minutes=2))
    assert not ctx.provider.sent
    assert not session.scalar(select(m.Approval.id).where(m.Approval.kind=="confirm_text"))
    receipt = session.scalar(select(m.Policy).where(m.Policy.key.startswith("job:reminder:")))
    assert receipt.value["state"] == "gloo_blocked"
    assert session.scalar(select(m.Escalation.id))


def test_rejected_review_stays_rejected_without_recomposition(session, clock, make_volunteer, make_shift, assign, tmp_path):
    v=make_volunteer();assign(v,make_shift(starts=clock.now()+timedelta(days=1)))
    ctx=context(session,clock,ConnectedDouble(),tmp_path);reminders.process(ctx)
    review=session.scalar(select(m.Approval).where(m.Approval.kind=="confirm_text"))
    reviewed(session,ctx,review,False)
    reminders.process(ctx)
    assert review.status=="rejected" and ctx.gloo.calls==1 and not ctx.provider.sent


@pytest.mark.parametrize("change", ["consent", "collection", "availability", "phone"])
def test_collection_exact_review_rechecks_mutable_authority(session, clock, make_volunteer, tmp_path, change):
    v=make_volunteer();a=collection(session,clock);ctx=context(session,clock,ConnectedDouble(),tmp_path)
    review=historical_collection_review(session,ctx,v,a)
    assert reminders.delivery_problem(session,review,clock.now()) is None
    def mutate():
        if change=="consent": v.sms_opt_in=False
        if change=="collection": a.status="rejected"
        if change=="availability": session.add(m.Availability(volunteer_id=v.id,month="2026-11",available_dates=["2026-11-01"]))
        if change=="phone": v.phone="+12025550198"
    human_change(session,mutate)
    assert reminders.delivery_problem(session,review,clock.now())
    reviewed(session,ctx,review)
    assert review.status=="expired" and not ctx.provider.sent and ctx.gloo.calls==0


@pytest.mark.parametrize("delivery_status", ["submitted", "uncertain", "queued"])
def test_availability_reminder_requires_delivered_initial_ask_and_wait(session, clock, make_volunteer, tmp_path, delivery_status):
    v=make_volunteer();a=collection(session,clock);ctx=context(session,clock,ConnectedDouble(),tmp_path)
    source={"type":"availability","collection_id":a.id,"month":"2026-11","reminder":True}
    assert "initial ask" in reminders.source_problem(session,v,source,clock.now())
    historical=m.Message(direction="out",volunteer_id=v.id,phone=v.phone,body="Historical ask",purpose="availability_ask",kind="ai",status=delivery_status,created_at=clock.now())
    session.add(historical);session.flush()
    session.add(m.Policy(key=f"job:availability:{a.id}:{v.id}:0",value={"message_id":historical.id}));session.flush()
    assert "three-day wait" in reminders.source_problem(session,v,source,clock.now())
    clock.advance(timedelta(days=3))
    if delivery_status != "submitted":
        assert "initial ask" in reminders.source_problem(session,v,source,clock.now())
        assert collect(ctx,a,reminder=True)=={"sent":[],"reviews":[]}
        assert session.get(m.Policy,f"job:availability:{a.id}:{v.id}:1") is None
        assert ctx.gloo.calls==0 and not ctx.provider.sent
        return
    assert reminders.source_problem(session,v,source,clock.now()) is None
    assert collect(ctx,a,reminder=True)=={"sent":[],"reviews":[]}
    assert session.get(m.Policy,f"job:availability:{a.id}:{v.id}:1").value["state"]=="blocked_policy"
    assert ctx.gloo.calls==0 and not ctx.provider.sent


def test_quiet_hours_do_not_compose_or_send_then_require_review(session, clock, make_volunteer, make_shift, assign, tmp_path):
    v=make_volunteer();assign(v,make_shift(starts=clock.now()+timedelta(days=1)))
    ctx=context(session,clock,ConnectedDouble(),tmp_path)
    clock.set_time(clock.now().replace(hour=23))
    assert reminders.process(ctx)["reminders"]==0 and ctx.gloo.calls==0
    clock.advance(timedelta(hours=9))
    assert reminders.process(ctx)["reminders"]==0
    reviews=session.scalars(select(m.Approval).where(m.Approval.kind=="confirm_text")).all()
    assert len(reviews)==1 and ctx.gloo.calls==1 and not ctx.provider.sent


def test_reminder_review_has_gloo_copy_and_checks_cancellation_at_preflight(session, clock, make_volunteer, make_shift, assign, tmp_path):
    v=make_volunteer(); row=assign(v,make_shift(starts=clock.now()+timedelta(days=1)))
    ctx=context(session,clock,ConnectedDouble(),tmp_path)
    assert reminders.process(ctx)=={"reminders":0,"confirmations":0,"summaries":0}
    reviews=session.scalars(select(m.Approval).where(m.Approval.kind=="confirm_text")).all()
    assert len(reviews)==1 and ctx.gloo.calls==1
    reminders.process(ctx); assert ctx.gloo.calls==1
    review=next(a for a in reviews if a.payload["purpose"]=="reminder")
    reviewed(session,ctx,review); assert len(ctx.provider.sent)==1
    human_change(session,lambda:setattr(row,"status","cancelled"))
    message=session.get(m.Message,review.payload["message_id"])
    assert "no longer current" in confirmations.delivery_problem(session,ctx.provider,review,clock.now(),message)


@pytest.mark.parametrize("change", ["event_time", "qualification", "care", "overlap"])
def test_assignment_review_rechecks_schedule_and_eligibility(session, clock, make_volunteer, make_shift, assign, tmp_path, change):
    v=make_volunteer(quals=[("training","verified",None)])
    shift=make_shift(required=["training"],starts=clock.now()+timedelta(days=1)); row=assign(v,shift)
    ctx=context(session,clock,ConnectedDouble(),tmp_path); reminders.process(ctx)
    review=session.scalar(select(m.Approval).where(m.Approval.payload["purpose"].as_string()=="reminder"))
    def mutate():
        if change=="event_time": shift.event.starts_at+=timedelta(hours=1)
        if change=="qualification": v.qualifications[0].status="pending"
        if change=="care": session.add(m.Escalation(category="sensitive",severity="normal",summary="Synthetic care hold",related_ids={"volunteer_id":v.id},status="open",created_at=clock.now()))
        if change=="overlap": assign(v,make_shift("another",starts=shift.event.starts_at))
    human_change(session,mutate); reviewed(session,ctx,review)
    assert review.status=="expired" and not ctx.provider.sent


def test_summary_review_invalidates_changed_coverage(session, clock, make_volunteer, make_shift, assign, tmp_path):
    coordinator=make_volunteer(coordinator=True); v=make_volunteer()
    clock.set_time(clock.now()+timedelta(days=2,hours=8))
    shift=make_shift(starts=clock.now()+timedelta(hours=15))
    ctx=context(session,clock,ConnectedDouble(),tmp_path); reminders.process(ctx)
    review=session.scalar(select(m.Approval).where(m.Approval.payload["purpose"].as_string()=="coordinator_notify"))
    assert review and review.payload["volunteer_id"]==coordinator.id
    human_change(session,lambda:assign(v,shift)); reviewed(session,ctx,review)
    assert review.status=="expired" and not ctx.provider.sent


def test_connected_plan_is_in_memory_until_exact_record_approval(session, clock, make_volunteer, make_shift, tmp_path):
    v=make_volunteer(prefs={"max_per_month":1}); make_shift(); make_shift(starts=clock.now()+timedelta(days=10))
    ctx=context(session,clock,ConnectedDouble(),tmp_path)
    report=plan_month(ctx,"2026-10")
    assert report["state"]=="pending_exact_review" and len(report["reviews"])==1 and not report["violations"]
    assert not session.scalar(select(m.Assignment.id)) and not ctx.provider.sent
    review=session.get(m.Approval,report["reviews"][0]); reviewed(session,ctx,review)
    assert session.scalar(select(m.Assignment)).status=="approved" and not ctx.provider.sent
    assert scheduler.validate(session,"2026-10")["violations"]==[]
    # Validation must not create a new record review by mutating row status.
    assert len(session.scalars(select(m.Approval)).all())==1


@pytest.mark.parametrize("change", ["care", "consent", "load", "event"])
def test_planning_record_review_rechecks_hard_rules(session, clock, make_volunteer, make_shift, assign, tmp_path, change):
    v=make_volunteer(prefs={"max_per_month":1}); shift=make_shift()
    ctx=context(session,clock,ConnectedDouble(),tmp_path); review=session.get(m.Approval,plan_month(ctx,"2026-10")["reviews"][0])
    def mutate():
        if change=="care": session.add(m.Escalation(category="sensitive",severity="normal",summary="Synthetic hold",related_ids={"volunteer_id":v.id},status="open",created_at=clock.now()))
        if change=="consent": v.sms_opt_in=False
        if change=="load": assign(v,make_shift(starts=clock.now()+timedelta(days=10)))
        if change=="event": shift.event.starts_at+=timedelta(hours=1)
    human_change(session,mutate)
    with pytest.raises(ValueError): reviewed(session,ctx,review)
    assert not ctx.provider.sent


def test_planning_outage_does_not_stage_record_publication(session, clock, make_volunteer, make_shift, tmp_path):
    make_volunteer(); make_shift(); ctx=context(session,clock,ConnectedDouble(),tmp_path,NullGloo())
    report=plan_month(ctx,"2026-10")
    assert report["state"]=="held_for_review" and report["reviews"]==[]
    assert not session.scalar(select(m.Assignment.id)) and not ctx.provider.sent
    with pytest.raises(ValueError): publish(ctx,NS(status="approved"))


def test_missing_mac_recipient_session_does_not_read_history_or_stage(session, clock, make_volunteer, tmp_path):
    class ScopedDouble(ConnectedDouble):
        test_sessions = {}
        def allows(self, phone): return True
    make_volunteer(); a=collection(session,clock); ctx=context(session,clock,ScopedDouble(),tmp_path)
    assert collect(ctx,a)=={"sent":[],"reviews":[]} and ctx.gloo.calls==0
    assert not ctx.provider.sent


def test_quiet_approval_expires_and_next_day_reuses_gloo_copy(session, clock, make_volunteer, make_shift, assign, tmp_path):
    v=make_volunteer();row=assign(v,make_shift(starts=clock.now()+timedelta(days=5)));row.source="planner"
    ctx=context(session,clock,ConnectedDouble(),tmp_path)
    clock.set_time(clock.now().replace(hour=20,minute=30));reminders.process(ctx)
    review=session.scalar(select(m.Approval).where(m.Approval.payload["purpose"].as_string()=="confirmation"))
    clock.advance(timedelta(minutes=35));reviewed(session,ctx,review)
    assert review.status=="expired" and not ctx.provider.sent
    reminders.process(ctx);assert ctx.gloo.calls==1
    clock.advance(timedelta(hours=11));reminders.process(ctx)
    replacement=session.scalar(select(m.Approval).where(m.Approval.kind=="confirm_text",m.Approval.status=="pending"))
    assert replacement.id!=review.id and replacement.payload["body"]==review.payload["body"]
    assert ctx.gloo.calls==1 and not ctx.provider.sent


def test_uncertain_assignment_notice_is_never_recomposed_or_requeued(session, clock, make_volunteer, make_shift, assign, tmp_path):
    v=make_volunteer();assign(v,make_shift(starts=clock.now()+timedelta(days=1)))
    ctx=context(session,clock,ConnectedDouble(),tmp_path);reminders.process(ctx)
    review=session.scalar(select(m.Approval).where(m.Approval.kind=="confirm_text"));reviewed(session,ctx,review)
    session.get(m.Message,review.payload["message_id"]).status="uncertain";session.flush()
    reminders.process(ctx);clock.advance(timedelta(minutes=3));reminders.process(ctx)
    assert ctx.gloo.calls==1 and len(ctx.provider.sent)==1
    assert len(session.scalars(select(m.Approval).where(m.Approval.kind=="confirm_text")).all())==1


def test_native_preflight_reloads_schedule_instead_of_cached_event(session, clock, make_volunteer, make_shift, assign, tmp_path):
    v=make_volunteer(); shift=make_shift(starts=clock.now()+timedelta(days=1)); assign(v,shift)
    ctx=context(session,clock,ConnectedDouble(),tmp_path); reminders.process(ctx)
    review=session.scalar(select(m.Approval).where(m.Approval.payload["purpose"].as_string()=="reminder"))
    reviewed(session,ctx,review)
    # A write from a different coordinator transaction can leave this ORM cache stale.
    original=shift.event.starts_at
    human_change(session,lambda:session.execute(update(m.Event).where(m.Event.id==shift.event_id)
        .values(starts_at=original+timedelta(hours=1)).execution_options(synchronize_session=False)))
    assert shift.event.starts_at==original
    message=session.get(m.Message,review.payload["message_id"])
    assert "details changed" in confirmations.delivery_problem(session,ctx.provider,review,clock.now(),message)


def test_model_call_finishing_after_event_never_stages_confirmation(session, clock, make_volunteer, make_shift, assign, tmp_path):
    v=make_volunteer(); assign(v,make_shift(starts=clock.now()+timedelta(minutes=1)))
    class SlowCopyGloo(CopyGloo):
        def create_response(self, **kwargs):
            clock.advance(timedelta(minutes=2))
            return super().create_response(**kwargs)
    ctx=context(session,clock,ConnectedDouble(),tmp_path,SlowCopyGloo())
    assert reminders.process(ctx)=={"reminders":0,"confirmations":0,"summaries":0}
    assert not session.scalar(select(m.Approval.id)) and not ctx.provider.sent
