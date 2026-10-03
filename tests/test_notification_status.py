"""Fictional status projections; no model, network or device delivery."""
from datetime import timedelta
from dataclasses import replace
from types import SimpleNamespace as NS
import httpx
import pytest
from fastapi.testclient import TestClient
from sqlalchemy import event, select
from app.agents.fill_agent import FillContext
from app.clock import FakeClock
from app.config import Settings
from app.core import confirmations, reminders
from app.db import models as m
from app.integrations.test_sessions import TestSession as RecipientSession
from app.main import create_app
from app.web import notification_status as status, texty
from tests.conftest import NOW
from tests.test_exact_day_before_reminder import ExactGloo
from tests.test_planning_composition import human_change, reviewed


def state(clock,provider,**settings):
    return NS(clock=clock,provider=provider,settings=Settings(automation_enabled=True,demo_mode=False,
        gloo_api_key="synthetic-key-not-used",**settings))


def item(session,current,notice="day_before"):
    return next(row for row in status.snapshot(session,current)["notifications"] if row["notice"]==notice)


def staged(session,clock,provider,make_volunteer,make_shift,assign,tmp_path):
    row=assign(make_volunteer("Fictional Recipient"),make_shift("greeter",starts=clock.now()+timedelta(days=1)))
    session.info[confirmations.MODE_KEY]=True
    ctx=FillContext(session,clock,provider,ExactGloo(),log_dir=tmp_path)
    reminders.process(ctx)
    approval=session.scalar(select(m.Approval).where(m.Approval.kind=="confirm_text"))
    return row,ctx,approval


class QueueProvider:
    def __init__(self): self.sent=[]
    def send(self,phone,body):
        self.sent.append((phone,body));return "MACsynthetic-only"


def test_audits_and_proposed_offers_never_create_notification_items(session,clock,provider,make_volunteer,make_shift,assign):
    row=assign(make_volunteer(),make_shift(),status="proposed")
    session.add(m.Notification(key="audit-only",purpose="conversation_source",body="",state="recorded",
        due_at=clock.now(),created_at=clock.now(),detail={"assignment_id":row.id,"notice":"scheduled"}));session.flush()
    assert status.snapshot(session,state(clock,provider))["notifications"]==[]


def test_due_dates_saved_names_timezone_and_pagination(session,clock,provider,make_volunteer,make_shift,assign):
    first=assign(make_volunteer("First Fictional"),make_shift("greeter",starts=NOW+timedelta(days=3)))
    assign(make_volunteer(),make_shift(starts=NOW+timedelta(days=4)))
    page=status.snapshot(session,state(clock,provider),limit=1)
    assert len(page["notifications"])==2 and page["next_offset"]==1
    reminder=next(r for r in page["notifications"] if r["notice"]=="day_before")
    assert reminder["recipient_name"]=="First Fictional" and reminder["assignment_id"]==first.id
    assert reminder["due_at"]=="2026-10-03T00:00:00-06:00" and reminder["due_basis"]=="local_day_before_window"
    assert reminder["state"]=="scheduled" and page["runtime"]["scheduler_running"]=="not_checked"
    assert status.snapshot(session,state(clock,provider),limit=1,offset=1)["next_offset"] is None


def test_paused_configuration_and_nonplanner_notice_are_honest_holds(session,clock,provider,make_volunteer,make_shift,assign):
    row=assign(make_volunteer(),make_shift())
    current=state(clock,provider);current.settings=replace(current.settings,automation_enabled=False)
    assert "paused" in item(session,current)["reason"]
    row.source="admin";session.flush()
    assert "no automatic" in item(session,current,"scheduled")["reason"]


def test_pending_exact_review_and_changed_assignment(session,clock,provider,make_volunteer,make_shift,assign,tmp_path):
    row,ctx,review=staged(session,clock,provider,make_volunteer,make_shift,assign,tmp_path)
    pending=item(session,state(clock,provider))
    assert pending["state"]=="awaiting-review" and pending["approval_id"]==review.id and pending["message_id"] is None
    assert not provider.sent
    human_change(session,lambda:setattr(row.shift.event,"title","Updated saved event"))
    assert item(session,state(clock,provider))["state"]=="held"
    assert "differ" in item(session,state(clock,provider))["reason"]


def test_pending_review_phone_drift_is_a_hold(session,clock,provider,make_volunteer,make_shift,assign,tmp_path):
    row,ctx,review=staged(session,clock,provider,make_volunteer,make_shift,assign,tmp_path)
    human_change(session,lambda:setattr(row.volunteer,"phone","+12025550199"))
    result=item(session,state(clock,provider))
    assert result["state"]=="held" and "recipient differs" in result["reason"]


def test_pending_review_quiet_hours_and_expiry_remain_read_only(session,clock,provider,make_volunteer,make_shift,assign,tmp_path):
    row,ctx,review=staged(session,clock,provider,make_volunteer,make_shift,assign,tmp_path)
    session.add(m.Policy(key="quiet_hours",value={"value":{"start":"09:00","end":"11:00"}}));session.flush()
    assert "Quiet hours" in item(session,state(clock,provider))["reason"]
    clock.advance(timedelta(hours=2))
    assert "expired" in item(session,state(clock,provider))["reason"]
    assert review.status=="pending" and not provider.sent


@pytest.mark.parametrize("change,word",[("consent","consent"),("qualification","eligible"),("care","personal-care")])
def test_current_source_holds_hide_private_detail(session,clock,provider,make_volunteer,make_shift,assign,change,word):
    row=assign(make_volunteer(),make_shift())
    if change=="consent":row.volunteer.sms_opt_in=False
    if change=="qualification":row.shift.role.required_qualifications=["sound_training"]
    if change=="care":session.add(m.Escalation(category="sensitive",severity="normal",summary="PRIVATE detail",
        related_ids={"volunteer_id":row.volunteer_id},status="open",created_at=clock.now()))
    session.flush();result=item(session,state(clock,provider))
    assert result["state"]=="held" and word in result["reason"] and "PRIVATE" not in str(result)


@pytest.mark.parametrize("outcome",["queued","dispatching","submitted","sent","delivered","uncertain","blocked_policy"])
def test_queue_and_provider_labels_never_prove_device_delivery(session,clock,make_volunteer,make_shift,assign,tmp_path,outcome):
    provider=QueueProvider()
    row,ctx,review=staged(session,clock,provider,make_volunteer,make_shift,assign,tmp_path)
    reviewed(session,ctx,review)
    message=session.get(m.Message,review.payload["message_id"]);message.status=outcome;session.flush()
    result=item(session,state(clock,provider,sms_provider="mac_messages"))
    assert result["message_id"]==message.id and result["delivery_evidence"]=="not_recorded"
    assert result["state"]==("queued" if outcome in {"queued","dispatching"} else "suppressed" if outcome=="blocked_policy" else "held")
    assert result["state"]!="verified-delivered" and len(provider.sent)==1
    if outcome in {"submitted","sent","delivered"}:assert "awaiting verification" in result["reason"]


def test_mock_result_is_explicitly_mock_only(session,clock,provider,make_volunteer,make_shift,assign,tmp_path):
    row,ctx,review=staged(session,clock,provider,make_volunteer,make_shift,assign,tmp_path)
    reviewed(session,ctx,review);result=item(session,state(clock,provider))
    assert result["state"]=="held" and result["delivery_evidence"]=="mock_only" and "Mock" in result["reason"]


def test_quiet_hours_and_expired_session_hold_existing_queue(session,clock,make_volunteer,make_shift,assign,tmp_path):
    provider=QueueProvider();row,ctx,review=staged(session,clock,provider,make_volunteer,make_shift,assign,tmp_path)
    reviewed(session,ctx,review)
    session.add(m.Policy(key="quiet_hours",value={"value":{"start":"09:00","end":"11:00"}}));session.flush()
    assert "Quiet hours" in item(session,state(clock,provider))["reason"]
    provider.allows=lambda phone:True
    provider.test_sessions={row.volunteer.phone:RecipientSession("a"*32,NOW-timedelta(hours=2),NOW)}
    result=item(session,state(clock,provider))
    assert result["state"]=="held" and result["recipient_session"]=="expired"


def test_terminal_policy_and_gloo_holds_not_fake_schedules(session,clock,provider,make_volunteer,make_shift,assign):
    row=assign(make_volunteer(),make_shift(starts=NOW+timedelta(days=1)))
    receipt=m.Policy(key=f"job:reminder:{row.id}",value={"state":"blocked_policy"});session.add(receipt);session.flush()
    assert item(session,state(clock,provider))["state"]=="suppressed"
    reason="This signup question or assignment notification was already requested"
    receipt.value={"state":"blocked_policy","policy_reason":reason};session.flush()
    assert item(session,state(clock,provider))["reason"]==reason
    receipt.value={"state":"blocked_policy","policy_reason":"PRIVATE +12025550101"};session.flush()
    assert "PRIVATE" not in str(item(session,state(clock,provider)))
    receipt.value={"state":"gloo_blocked"};session.flush()
    assert "Gloo" in item(session,state(clock,provider))["reason"]


@pytest.fixture
def client_app(tmp_path):
    app=create_app(Settings(database_url=f"sqlite:///{tmp_path}/status.db",automation_enabled=False,demo_mode=False))
    app.state.clock=FakeClock(NOW)
    app.dependency_overrides[texty.admin]=lambda:{"id":"11111111-1111-4111-8111-111111111111"}
    with TestClient(app) as client:yield client,app


def test_registered_route_is_sql_read_only_and_rejects_mutations(client_app):
    client,app=client_app
    with app.state.session_factory() as session:
        session.info["record_authorized"]=True
        volunteer=m.Volunteer(name="Fictional API",phone="+12025550101",status="active",sms_opt_in=True,preferences={},created_at=NOW)
        role=m.Role(name="greeter",ministry="test",required_qualifications=[],criticality="standard",fill_policy="auto")
        event_row=m.Event(title="Saved API event",starts_at=NOW+timedelta(days=2),ends_at=NOW+timedelta(days=2,hours=1),status="scheduled")
        session.add_all([volunteer,role,event_row]);session.flush()
        shift=m.Shift(event_id=event_row.id,role_id=role.id,slot_index=0);session.add(shift);session.flush()
        session.add(m.Assignment(shift_id=shift.id,volunteer_id=volunteer.id,status="approved",source="planner",created_at=NOW,updated_at=NOW));session.commit()
    def no_writes(conn,cursor,statement,params,context,executemany):
        assert statement.lstrip().split()[0].upper() not in {"INSERT","UPDATE","DELETE","CREATE","DROP","ALTER"}
    event.listen(app.state.engine,"before_cursor_execute",no_writes)
    try:
        first=client.get("/api/notification-status");assert first.status_code==200
        assert client.get("/api/notification-status").json()==first.json()
        assert len(first.json()["notifications"])==2 and first.json()["read_only"] is True
        assert all(key not in str(first.json()) for key in ("+12025550101","body","synthetic-key"))
        assert client.post("/api/notification-status",json={}).status_code==405
        assert client.get("/api/notification-status?limit=101").status_code==422
        assert client.get("/api/notification-status?offset=-1").status_code==422
        assert not app.state.provider.sent and not app.state.settings.automation_enabled
    finally:event.remove(app.state.engine,"before_cursor_execute",no_writes)


@pytest.mark.parametrize("email,verified,expected",[("other@example.test","2026-10-01",403),
    ("allowed@example.test",None,403),("allowed@example.test","2026-10-01",200)])
def test_admin_dependency_rejects_missing_token_and_wrong_verified_account(client_app,monkeypatch,email,verified,expected):
    client,app=client_app;app.dependency_overrides.clear()
    app.state.settings=replace(app.state.settings,supabase_url="https://fictional-auth.example.test",
        supabase_publishable_key="synthetic-public",admin_email_allowlist="allowed@example.test")
    assert client.get("/api/notification-status").status_code==401
    transport=httpx.MockTransport(lambda request:httpx.Response(200,json={"id":"11111111-1111-4111-8111-111111111111",
        "email":email,"email_confirmed_at":verified}))
    original=httpx.AsyncClient
    monkeypatch.setattr(httpx,"AsyncClient",lambda **kwargs:original(transport=transport,**kwargs))
    assert client.get("/api/notification-status",headers={"Authorization":"Bearer synthetic"}).status_code==expected
