"""Fictional single-church scope, authenticated owner doubles, mock delivery only."""
import json
from concurrent.futures import ThreadPoolExecutor
from datetime import timedelta
from uuid import uuid4
import httpx
import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select
from app.clock import FakeClock
from app.config import Settings
from app.core import availability_review as review, confirmations
from app.db import models as m
from app.main import create_app
from app.web import planning_workflows, texty
from tests.conftest import NOW
from tests.test_planning_composition import CopyGloo
from tests.test_admin_setup import OWNER_A, OWNER_B

PREFIX="/api/planning/availability-collections"


@pytest.fixture
def planning_client(tmp_path):
    app=create_app(Settings(database_url=f"sqlite:///{tmp_path}/planning.db",sms_provider="mock",
        demo_mode=False,automation_enabled=False,competition_confirmation_required=True))
    app.include_router(planning_workflows.router)
    app.state.clock=FakeClock(NOW)
    app.state.gloo=CopyGloo()
    user={"id":OWNER_A,"email":"admin@example.test","email_confirmed_at":"2026-10-01"}
    app.dependency_overrides[texty.admin]=lambda:user
    with TestClient(app) as client:
        with app.state.session_factory() as session:
            session.info["record_authorized"]=True
            session.add_all([m.Volunteer(name=f"Fictional Volunteer {i}",phone=f"+1202555010{i}",
                status="active",sms_opt_in=True,preferences={},created_at=NOW) for i in (1,2)])
            session.commit()
        yield client,app,user


def requested(client, **extra):
    response=client.post(PREFIX,json={"month":"2026-11",**extra})
    assert response.status_code==200,response.text
    return response.json()["collection"]


def decision(client,parent,action="approve",**extra):
    return client.post(f"{PREFIX}/{parent['id']}/{action}",json={"content_hash":parent["content_hash"],**extra})


def mutate(app,function):
    with app.state.session_factory() as session:
        session.info["record_authorized"]=True
        function(session);session.commit()


def test_parent_preview_is_exact_owner_bound_and_never_enables_delivery(planning_client):
    client,app,_=planning_client; parent=requested(client)
    assert parent["status"]=="pending" and parent["scope"]["recipient_count"]==2
    assert parent["scope"]["recipients"][0]=={"volunteer_id":1,"name":"Fictional Volunteer 1","phone":"+12025550101"}
    assert parent["text_review_ids"]==[] and not app.state.gloo.calls and not app.state.provider.sent
    assert client.get(PREFIX).json()["collections"]==[parent]
    response=decision(client,parent).json()
    assert response["collection"]["status"]=="approved" and response["collection"]["composition_status"]=="blocked_policy"
    assert response["sent"]==0 and not response["delivery_enabled"] and not response["scheduler_activated"]
    assert not app.state.gloo.calls and not app.state.provider.sent and not app.state.settings.automation_enabled
    with app.state.session_factory() as session:
        child=session.get(m.Approval,response["collection"]["collection_id"])
        assert review.approved_collection_problem(session,child,app.state.clock.now()) is None


@pytest.mark.parametrize("action",["read","approve","reject","retry"])
def test_foreign_owner_cannot_find_or_decide_parent(planning_client,action):
    client,app,user=planning_client; parent=requested(client);user["id"]=OWNER_B
    assert client.get(PREFIX).json()=={"collections":[]}
    response=client.get(f"{PREFIX}/{parent['id']}") if action=="read" else decision(client,parent,action)
    assert response.status_code==404 and not app.state.gloo.calls and not app.state.provider.sent


@pytest.mark.parametrize("field",["owner_id","recipient_ids","scope","collection_owner_id"])
def test_client_scope_and_owner_cannot_override_server_preview(planning_client,field):
    client,app,_=planning_client
    assert client.post(PREFIX,json={"month":"2026-11",field:OWNER_B}).status_code==422
    parent=requested(client)
    assert decision(client,parent,**{field:OWNER_B}).status_code==422
    assert not app.state.gloo.calls and not app.state.provider.sent


@pytest.mark.parametrize("month",["2026-00","2026-1","2026-09","2027-11",True,None])
def test_bad_past_or_out_of_horizon_month(planning_client,month):
    assert planning_client[0].post(PREFIX,json={"month":month}).status_code==422


def test_request_retry_is_idempotent_and_uuid_cannot_change_month(planning_client):
    client,app,_=planning_client; key=str(uuid4()); parent=requested(client,request_id=key)
    assert requested(client,request_id=key)==parent
    assert requested(client)["id"]==parent["id"]
    assert client.post(PREFIX,json={"month":"2026-12","request_id":key}).status_code==409
    assert not app.state.gloo.calls and not app.state.provider.sent
    with app.state.session_factory() as session:
        assert all(len(key)<=80 for key in session.scalars(select(m.Policy.key)))


def test_wrong_hash_or_tampered_stored_scope_cannot_authorize(planning_client):
    client,app,_=planning_client; parent=requested(client)
    assert client.post(f"{PREFIX}/{parent['id']}/approve",json={"content_hash":"0"*64}).status_code==409
    def tamper(session):
        p=session.get(m.Approval,parent["id"])
        p.payload={**p.payload,"month":"2026-12"}
    mutate(app,tamper)
    assert decision(client,parent).status_code==409
    assert not app.state.gloo.calls and not app.state.provider.sent


@pytest.mark.parametrize("change",["phone","name","consent","newcomer","availability","timezone","care"])
def test_scope_changes_before_approval_require_new_hash(planning_client,change):
    client,app,_=planning_client; parent=requested(client)
    def update(session):
        v=session.get(m.Volunteer,1)
        if change=="phone":v.phone="+12025550109"
        if change=="name":v.name="Renamed Fictional Volunteer"
        if change=="consent":v.sms_opt_in=False
        if change=="newcomer":session.add(m.Volunteer(name="New Fictional Person",phone="+12025550103",status="active",sms_opt_in=True,preferences={},created_at=NOW))
        if change=="availability":session.add(m.Availability(volunteer_id=1,month="2026-11",available_dates=["2026-11-01"]))
        if change=="timezone":session.add(m.Policy(key="church_timezone",value={"value":"America/New_York"}))
        if change=="care":session.add(m.Escalation(category="sensitive",severity="normal",summary="Fictional hold",status="open",related_ids={"volunteer_id":1},created_at=NOW))
    mutate(app,update)
    assert decision(client,parent).status_code==409 and not app.state.gloo.calls
    fresh=requested(client,request_id=str(uuid4()))
    assert fresh["id"]!=parent["id"] and fresh["content_hash"]!=parent["content_hash"]
    assert not app.state.provider.sent


def test_expired_review_and_month_rollover_hold_without_composition(planning_client):
    client,app,_=planning_client; parent=requested(client)
    app.state.clock.advance(timedelta(hours=2))
    assert decision(client,parent).status_code==409
    assert client.get(f"{PREFIX}/{parent['id']}").json()["collection"]["status"]=="expired"
    parent=requested(client,request_id=str(uuid4()))
    app.state.clock.set_time(NOW.replace(month=12))
    assert decision(client,parent).status_code==409 and not app.state.gloo.calls and not app.state.provider.sent


def test_rejection_and_repeat_decisions_never_prepare_text(planning_client):
    client,app,_=planning_client; parent=requested(client)
    first=decision(client,parent,"reject").json();second=decision(client,parent,"reject").json()
    assert first==second and first["collection"]["status"]=="rejected"
    assert decision(client,parent,"approve").status_code==409
    assert decision(client,parent,"retry").status_code==409
    assert not app.state.gloo.calls and not app.state.provider.sent


def test_explicit_prepare_records_each_suppression_once_without_text_reviews(planning_client):
    client,app,_=planning_client;parent=requested(client)
    approved=decision(client,parent).json()
    assert decision(client,parent).json()==approved and not app.state.gloo.calls
    first=decision(client,parent,"retry").json()
    assert first["text_review_ids"]==[] and first["collection"]["composition_status"]=="blocked_policy"
    assert first["collection"]["suppressed_recipient_count"]==2
    assert decision(client,parent,"retry").json()==first
    with app.state.session_factory() as session:
        child=first["collection"]["collection_id"]
        receipts=[session.get(m.Policy,f"job:availability:{child}:{vid}:0") for vid in (1,2)]
        assert all(row.value["state"]=="blocked_policy" for row in receipts)
        assert {row.value["phone"] for row in receipts}=={"+12025550101","+12025550102"}
        assert not session.scalar(select(m.Approval).where(m.Approval.kind=="confirm_text"))
    assert not app.state.gloo.calls and not app.state.provider.sent and not app.state.settings.automation_enabled


def test_concurrent_prepare_does_not_repeat_the_same_model_or_review(planning_client):
    client,app,_=planning_client;parent=requested(client);decision(client,parent)
    with ThreadPoolExecutor(max_workers=2) as pool:
        results=list(pool.map(lambda _:decision(client,parent,"retry"),range(2)))
    assert all(r.status_code==200 for r in results) and app.state.gloo.calls==0
    result=client.get(f"{PREFIX}/{parent['id']}").json()["collection"]
    assert result["text_review_ids"]==[] and result["suppressed_recipient_count"]==2
    with app.state.session_factory() as session:
        receipts=session.scalars(select(m.Policy).where(m.Policy.key.startswith("job:availability:"))).all()
        assert len(receipts)==2 and {r.value["volunteer_id"] for r in receipts}=={1,2}
    assert not app.state.provider.sent


def test_after_approval_newcomer_or_changed_destination_cannot_expand_scope(planning_client):
    client,app,_=planning_client;parent=requested(client);decision(client,parent)
    def update(session):
        session.get(m.Volunteer,1).phone="+12025550109"
        session.add(m.Volunteer(name="New Person",phone="+12025550103",status="active",sms_opt_in=True,preferences={},created_at=NOW))
    mutate(app,update)
    result=decision(client,parent,"retry").json()
    assert result["text_review_ids"]==[] and result["collection"]["composition_status"]=="held"
    assert "Recipient details changed" in result["collection"]["hold_reason"]
    assert result["collection"]["suppressed_recipient_count"]==1
    with app.state.session_factory() as session:
        child=result["collection"]["collection_id"]
        receipts=session.scalars(select(m.Policy).where(m.Policy.key.startswith(f"job:availability:{child}:"))).all()
        assert len(receipts)==1 and receipts[0].value["phone"]=="+12025550102"
        assert session.get(m.Policy,f"job:availability:{child}:1:0") is None
        assert session.get(m.Policy,f"job:availability:{child}:3:0") is None
    assert app.state.gloo.calls==0 and not app.state.provider.sent


def test_collection_policy_precedes_gloo_outage_and_never_retries_it(planning_client):
    from app.llm.gloo_client import GlooUnavailableError
    class Outage(CopyGloo):
        def create_response(self,**kwargs):
            self.calls+=1;raise GlooUnavailableError("synthetic outage")
    client,app,_=planning_client;app.state.gloo=Outage();parent=requested(client);decision(client,parent)
    for _ in range(3):assert decision(client,parent,"retry").status_code==200
    result=decision(client,parent,"retry").json()
    assert result["collection"]["composition_status"]=="blocked_policy" and result["collection"]["retry_at"] is None
    assert result["collection"]["suppressed_recipient_count"]==2
    assert result["text_review_ids"]==[] and app.state.gloo.calls==0 and not app.state.provider.sent


def test_generic_legacy_child_is_held_in_exact_mode(planning_client):
    from app.agents.fill_agent import FillContext
    from app.agents.planning_agent import collect
    client,app,_=planning_client
    with app.state.session_factory() as session:
        a=m.Approval(kind="collect_availability",status="approved",payload={"month":"2026-11"},requested_at=NOW)
        session.add(a);session.flush()
        result=collect(FillContext(session,app.state.clock,app.state.provider,app.state.gloo),a)
        assert result["reviews"]==[] and "signed-in" in result["held"]
    assert not app.state.gloo.calls and not app.state.provider.sent


@pytest.mark.parametrize("verified,email",[(None,"allowed@example.test"),("2026-10-01","other@example.test")])
def test_actual_auth_dependency_rejects_unverified_or_unallowlisted_account(tmp_path,monkeypatch,verified,email):
    app=create_app(Settings(database_url=f"sqlite:///{tmp_path}/auth.db",automation_enabled=False,
        sms_provider="mock",competition_confirmation_required=True,supabase_url="https://auth.example.test",
        supabase_publishable_key="synthetic-public-key",admin_email_allowlist="allowed@example.test"))
    app.include_router(planning_workflows.router)
    original=httpx.AsyncClient
    transport=httpx.MockTransport(lambda request:httpx.Response(200,json={"id":OWNER_A,"email":email,"email_confirmed_at":verified}))
    monkeypatch.setattr(texty.httpx,"AsyncClient",lambda **kwargs:original(transport=transport,**kwargs))
    with TestClient(app) as client:
        assert client.get(PREFIX).status_code==401
        assert client.post(PREFIX,headers={"Authorization":"Bearer synthetic-token"},json={"month":"2026-11"}).status_code==403
